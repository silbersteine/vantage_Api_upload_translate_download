"""HTTP access to the Lokalise Vantage API and its presigned upload targets."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

DEFAULT_BASE_URL = "https://api.lokalise.com/vantage"
USER_AGENT = "vantage-file-client/0.1.0"
JSON_TIMEOUT_SECONDS = 60
TRANSFER_TIMEOUT_SECONDS = 300


class VantageError(Exception):
    """A failed Vantage API call or a journey check that stopped the run."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str | None = None,
        status: int | None = None,
        details: object = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.status = status
        self.details = details


@dataclass
class HttpResult:
    status: int
    headers: dict[str, str]
    body: bytes


@dataclass
class Download:
    status: int
    headers: dict[str, str]
    body: bytes
    url: str


Send = Callable[[str, str, dict[str, str], bytes | None, float], HttpResult]


def header(headers: dict[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


def parse_error_payload(status: int, payload: object) -> VantageError:
    """Read either a single error object or an `errors` array."""
    if isinstance(payload, dict) and isinstance(payload.get("errors"), list):
        parts: list[str] = []
        first_code: str | None = None
        for item in payload["errors"]:
            if not isinstance(item, dict):
                continue
            code = item.get("errorCode")
            message = item.get("message") or "Request failed"
            if first_code is None and isinstance(code, str):
                first_code = code
            parts.append(f"{code}: {message}" if code else str(message))
        text = "; ".join(parts) or "Request failed"
        return VantageError(
            text,
            error_code=first_code or "request_failed",
            status=status,
            details=payload,
        )
    if isinstance(payload, dict) and ("message" in payload or "errorCode" in payload):
        code = payload.get("errorCode")
        message = str(payload.get("message") or "Request failed")
        error_code = str(code) if code else "request_failed"
        return VantageError(
            message,
            error_code=error_code,
            status=status,
            details=payload.get("details"),
        )
    return VantageError(
        f"HTTP {status}",
        error_code="http_error",
        status=status,
        details=payload,
    )


def _escape_disposition(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def encode_multipart(
    fields: list[tuple[str, str]],
    *,
    file_name: str,
    mime_type: str,
    content: bytes,
    boundary: str,
) -> bytes:
    """Encode a multipart body. `fields` are written first and the file is last."""
    chunks: list[bytes] = []
    for name, value in fields:
        chunks.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{_escape_disposition(name)}"\r\n'
                "\r\n"
                f"{value}\r\n"
            ).encode("utf-8")
        )
    chunks.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{_escape_disposition(file_name)}"\r\n'
            f"Content-Type: {mime_type}\r\n"
            "\r\n"
        ).encode("utf-8")
    )
    chunks.append(content)
    chunks.append(f"\r\n--{boundary}--\r\n".encode("ascii"))
    return b"".join(chunks)


def multipart_fields(upload_fields: dict[str, object], mime_type: str) -> list[tuple[str, str]]:
    """Presigned fields, then the file MIME type. The file part is appended separately."""
    return [(str(key), str(value)) for key, value in upload_fields.items()] + [
        ("Content-Type", mime_type)
    ]


def _boundary_for(content: bytes) -> str:
    for _ in range(5):
        boundary = "----vantage" + uuid.uuid4().hex
        if boundary.encode("ascii") not in content:
            return boundary
    raise VantageError(
        "Could not build the upload body",
        error_code="upload_body",
    )


def default_send(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout: float,
) -> HttpResult:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw_headers = {key: value for key, value in response.headers.items()}
            return HttpResult(status=response.status, headers=raw_headers, body=response.read())
    except urllib.error.HTTPError as error:
        raw_headers = {key: value for key, value in error.headers.items()} if error.headers else {}
        return HttpResult(status=error.code, headers=raw_headers, body=error.read())
    except urllib.error.URLError as error:
        host = urllib.parse.urlparse(url).netloc
        raise VantageError(
            f"Could not reach {host}: {error.reason}",
            error_code="connection_error",
        ) from error


class VantageClient:
    """JSON calls send `X-Api-Token`. Presigned upload and download hosts do not."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        send: Send | None = None,
        timeout: float = JSON_TIMEOUT_SECONDS,
        transfer_timeout: float = TRANSFER_TIMEOUT_SECONDS,
    ) -> None:
        if not token:
            raise VantageError("API token is empty", error_code="missing_token")
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.transfer_timeout = transfer_timeout
        self._send = send or default_send

    def get_json(self, path: str, query: dict[str, str] | None = None) -> dict:
        return self._request_json("GET", path, query=query)

    def post_json(self, path: str, body: dict) -> dict:
        return self._request_json("POST", path, body=body)

    def post_file(
        self,
        url: str,
        upload_fields: dict[str, object],
        mime_type: str,
        file_path: Path,
        *,
        boundary: str | None = None,
    ) -> None:
        content = file_path.read_bytes()
        fields = multipart_fields(upload_fields, mime_type)
        chosen_boundary = boundary or _boundary_for(content)
        payload = encode_multipart(
            fields,
            file_name=file_path.name,
            mime_type=mime_type,
            content=content,
            boundary=chosen_boundary,
        )
        headers = {"Content-Type": f"multipart/form-data; boundary={chosen_boundary}"}
        result = self._send("POST", url, headers, payload, self.transfer_timeout)
        if result.status < 200 or result.status >= 300:
            detail = result.body.decode("utf-8", errors="replace")[:500]
            message = f"Upload failed with HTTP {result.status}"
            if detail.strip():
                message = f"{message}: {detail.strip()}"
            raise VantageError(message, error_code="upload_failed", status=result.status)

    def get_download(self, url: str) -> Download:
        result = self._send("GET", url, {}, None, self.transfer_timeout)
        return Download(status=result.status, headers=result.headers, body=result.body, url=url)

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        query: dict[str, str] | None = None,
    ) -> dict:
        headers = {
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "X-Api-Token": self.token,
        }
        payload: bytes | None = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        result = self._send(method, self._url(path, query), headers, payload, self.timeout)
        if result.status >= 400:
            raise self._error_from(result)
        if not result.body or not result.body.strip():
            return {}
        try:
            parsed = json.loads(result.body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise VantageError(
                "Response was not JSON",
                error_code="invalid_response",
                status=result.status,
            ) from exc
        # Confirming an upload returns 202 with a JSON null body.
        if parsed is None:
            return {}
        if not isinstance(parsed, dict):
            raise VantageError(
                "Response was not a JSON object",
                error_code="invalid_response",
                status=result.status,
            )
        return parsed

    def _url(self, path: str, query: dict[str, str] | None) -> str:
        if not path.startswith("/"):
            path = "/" + path
        url = self.base_url + path
        if query:
            url = url + "?" + urllib.parse.urlencode(query)
        return url

    def _error_from(self, result: HttpResult) -> VantageError:
        if result.body:
            try:
                parsed = json.loads(result.body.decode("utf-8"))
            except json.JSONDecodeError:
                parsed = None
            if parsed is not None:
                return parse_error_payload(result.status, parsed)
            text = result.body.decode("utf-8", errors="replace").strip()
            if text:
                return VantageError(text[:500], error_code="http_error", status=result.status)
        return VantageError(
            f"HTTP {result.status}",
            error_code="http_error",
            status=result.status,
        )
