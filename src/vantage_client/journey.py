"""The Vantage file journey: stage, import, wait, and export only this run's files."""

from __future__ import annotations

import hashlib
import io
import time
import zipfile
from email.message import EmailMessage
from pathlib import Path, PurePath, PurePosixPath
from typing import Callable
from urllib.parse import unquote, urlparse

from vantage_client.client import Download, VantageClient, VantageError, header

MAX_FILE_BYTES = 52_428_800
BATCH_SIZE = 100

MIME_TYPES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".html": "text/html",
    ".htm": "text/html",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".idml": "application/vnd.adobe.indesign-idml-package",
}

UPLOADED_STATUSES = {"UPLOADED", "IMPORTING", "IMPORTED"}
FAILED_UPLOAD_STATUSES = {"UPLOAD_FAILED", "IMPORT_FAILED"}

Log = Callable[[str], None]
Clock = Callable[[], float]
Sleeper = Callable[[float], None]


def locale_key(locale: str) -> str:
    """BCP 47 comparison: case-insensitive, and `_` matches `-`."""
    return locale.strip().replace("_", "-").casefold()


def mime_type_for(path: Path) -> str:
    mime = MIME_TYPES.get(path.suffix.casefold())
    if mime is None:
        raise VantageError(
            f"Unsupported file type: {path.name}",
            error_code="unsupported_file_extension",
        )
    return mime


def md5_hex(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def translated_name(file_name: str, locale: str) -> str:
    """`guide.docx` for `de-DE` is `guide.de-DE.docx`."""
    if any(separator in value for value in (file_name, locale) for separator in ("/", "\\")):
        raise VantageError(
            f"Download name is not a single file: {file_name} ({locale})",
            error_code="unexpected_download",
        )
    path = PurePath(file_name)
    return f"{path.stem}.{locale}{path.suffix}"


def expected_download_names(file_names: list[str], locales: list[str]) -> set[str]:
    return {translated_name(file_name, locale) for file_name in file_names for locale in locales}


def language_is_ready(status: str) -> bool:
    """AI output is ready at COMPLETED, or at PENDING_REVIEW when a person still has to review it."""
    return status in {"COMPLETED", "PENDING_REVIEW"}


def resolve_locales(target_languages: list[dict], requested: list[str] | None) -> list[str]:
    """Return project locale strings. The base language is not in `target_languages`."""
    if not isinstance(target_languages, list):
        raise VantageError(
            "Project response did not include target languages",
            error_code="invalid_response",
        )
    canonical: dict[str, str] = {}
    ordered: list[str] = []
    for language in target_languages:
        locale = language.get("locale") if isinstance(language, dict) else None
        if not isinstance(locale, str) or not locale:
            raise VantageError(
                "A target language is missing its locale",
                error_code="invalid_response",
            )
        canonical[locale_key(locale)] = locale
        ordered.append(locale)
    if requested is None:
        if not ordered:
            raise VantageError(
                "This project has no target languages",
                error_code="no_target_languages",
            )
        return ordered
    resolved: list[str] = []
    for raw in requested:
        match = canonical.get(locale_key(raw))
        if match is None:
            raise VantageError(
                f"{raw} is not a target language of this project",
                error_code="unknown_locale",
            )
        if match not in resolved:
            resolved.append(match)
    return resolved


def collect_files(inputs: list[Path], *, max_bytes: int = MAX_FILE_BYTES) -> list[Path]:
    """Files passed directly must be supported. Directories contribute supported files only."""
    found: list[Path] = []
    for raw in inputs:
        path = raw if isinstance(raw, Path) else Path(raw)
        if not path.exists():
            raise VantageError(f"Input not found: {path}", error_code="input_not_found")
        if path.is_file():
            _require_supported(path, max_bytes)
            found.append(path)
            continue
        if path.is_dir():
            for child in path.rglob("*"):
                if child.is_file() and child.suffix.casefold() in MIME_TYPES:
                    _require_size(child, max_bytes)
                    found.append(child)
            continue
        raise VantageError(f"Input is not a file or directory: {path}", error_code="input_not_found")

    unique = sorted(set(found), key=lambda item: str(item).casefold())
    if not unique:
        raise VantageError(
            "No supported files were found",
            error_code="no_files",
        )
    seen: dict[str, Path] = {}
    for path in unique:
        key = path.name.casefold()
        previous = seen.get(key)
        if previous is not None:
            raise VantageError(
                f"File name {path.name} is used by {previous} and {path}. "
                "Vantage identifies an upload by its file name.",
                error_code="duplicate_file_name",
            )
        seen[key] = path
    return unique


def describe_file(path: Path) -> dict[str, object]:
    return {
        "fileName": path.name,
        "mimeType": mime_type_for(path),
        "size": path.stat().st_size,
        "checksum": md5_hex(path),
    }


def data_object(payload: dict, label: str) -> dict:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise VantageError(
            f"{label} response did not include data",
            error_code="invalid_response",
        )
    return data


def entry_error(entry: dict) -> str | None:
    error = entry.get("error")
    if isinstance(error, dict):
        code = error.get("errorCode") or "upload_rejected"
        message = error.get("message") or "rejected"
        return f"{code}: {message}"
    if isinstance(error, str) and error:
        return error
    if entry.get("errorCode") or (entry.get("message") and not entry.get("uploadId")):
        code = entry.get("errorCode") or "upload_rejected"
        message = entry.get("message") or "rejected"
        return f"{code}: {message}"
    return None


def upload_failure(record: dict) -> VantageError | None:
    status = record.get("status")
    if status not in FAILED_UPLOAD_STATUSES:
        return None
    name = record.get("fileName") or record.get("uploadId") or "upload"
    error = record.get("error")
    if isinstance(error, dict):
        code = str(error.get("errorCode") or status)
        message = str(error.get("message") or status)
        return VantageError(f"{name}: {message}", error_code=code)
    if error:
        return VantageError(f"{name}: {error}", error_code=str(status))
    return VantageError(f"{name} is {status}", error_code=str(status))


def download_filename(download: Download) -> str:
    disposition = header(download.headers, "content-disposition")
    filename = None
    if disposition:
        message = EmailMessage()
        message["Content-Disposition"] = disposition
        filename = message.get_filename()
    if not filename:
        filename = unquote(urlparse(download.url).path.rsplit("/", 1)[-1])
    safe = PurePath(filename).name
    if not safe or safe in {".", ".."}:
        raise VantageError(
            "Download did not include a file name",
            error_code="unexpected_download",
        )
    return safe


def safe_member_name(name: str) -> str:
    normalized = name.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if (
        not normalized
        or normalized.endswith("/")
        or pure.is_absolute()
        or ".." in pure.parts
        or len(pure.parts) != 1
    ):
        raise VantageError(
            f"Unexpected path in the download: {name}",
            error_code="unexpected_download",
        )
    return pure.name


def validate_member_names(names: list[str], expected: set[str]) -> set[str]:
    """Return expected names that are absent. Unexpected names fail the download."""
    actual: list[str] = []
    for name in names:
        actual.append(safe_member_name(name))
    actual_set = set(actual)
    if len(actual) != len(actual_set):
        raise VantageError(
            "The download archive contains duplicate file names",
            error_code="unexpected_download",
        )
    extra = sorted(actual_set - expected)
    if extra:
        raise VantageError(
            "Download does not match the files from this run: unexpected " + ", ".join(extra),
            error_code="unexpected_download",
        )
    return expected - actual_set


def _is_zip(body: bytes) -> bool:
    return body.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"))


def save_download(download: Download, output: Path, expected: set[str]) -> list[Path]:
    """Save the translated files. Several files arrive as a ZIP. One file arrives as itself."""
    if not expected:
        raise VantageError("Nothing was selected to download", error_code="unexpected_download")
    output.mkdir(parents=True, exist_ok=True)
    if len(expected) > 1 and _is_zip(download.body):
        return _save_zip(download, output, expected)
    return _save_single_file(download, output, expected)


def _save_single_file(download: Download, output: Path, expected: set[str]) -> list[Path]:
    actual = download_filename(download)
    if actual not in expected:
        raise VantageError(
            f"Downloaded file was {actual}, expected {', '.join(sorted(expected))}",
            error_code="unexpected_download",
        )
    destination = output / actual
    destination.write_bytes(download.body)
    return [destination]


def _save_zip(download: Download, output: Path, expected: set[str]) -> list[Path]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(download.body))
    except zipfile.BadZipFile as exc:
        raise VantageError(
            "Expected a ZIP of translated files",
            error_code="unexpected_download",
        ) from exc
    with archive:
        names = [safe_member_name(name) for name in archive.namelist()]
        validate_member_names(archive.namelist(), expected)
        written: list[Path] = []
        for name in sorted(set(names)):
            destination = output / name
            destination.write_bytes(archive.read(name))
            written.append(destination)
        return written


def _require_size(path: Path, max_bytes: int) -> None:
    size = path.stat().st_size
    if size > max_bytes:
        raise VantageError(
            f"{path.name} is {size} bytes, over the {max_bytes} byte limit",
            error_code="file_too_large",
        )


def _require_supported(path: Path, max_bytes: int) -> None:
    mime_type_for(path)
    _require_size(path, max_bytes)


def _chunks(items: list[Path], size: int) -> list[list[Path]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def _default_log(message: str) -> None:
    import sys

    print(message, file=sys.stderr)


class Translator:
    def __init__(
        self,
        client: VantageClient,
        *,
        timeout: float = 1800,
        poll_interval: float = 5,
        batch_size: int = BATCH_SIZE,
        log: Log | None = None,
        clock: Clock = time.monotonic,
        sleep: Sleeper = time.sleep,
    ) -> None:
        self.client = client
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.batch_size = batch_size
        self.log = log or _default_log
        self.clock = clock
        self.sleep = sleep

    def run(
        self,
        project_id: str,
        inputs: list[Path],
        output: Path,
        locales: list[str] | None = None,
    ) -> list[Path]:
        files = collect_files(inputs)
        project = data_object(
            self.client.get_json(f"/v1/projects/{project_id}"),
            "Project",
        )
        source = project.get("contentSourceType")
        if source != "file":
            raise VantageError(
                f"This project does not accept file imports (content source is {source})",
                error_code="project_type_not_supported",
            )
        selected = resolve_locales(project.get("targetLanguages") or [], locales)
        project_name = project.get("name") or project_id
        self.log(f"Project {project_name}: target locales {', '.join(selected)}")
        self.log(f"Sending {len(files)} file(s)")

        imported: list[tuple[str, str]] = []
        import_ids: list[str] = []
        for batch in _chunks(files, self.batch_size):
            batch_id, staged = self._stage(project_id, batch)
            for upload_id, path, target in staged:
                self.log(f"Uploading {path.name}")
                self.client.post_file(
                    str(target["url"]),
                    dict(target["fields"]),
                    mime_type_for(path),
                    path,
                )
            self.client.post_json(
                f"/v1/projects/{project_id}/uploads/actions/complete",
                {"batchId": batch_id},
            )
            staged_ids = [upload_id for upload_id, _, _ in staged]
            names = {upload_id: path.name for upload_id, path, _ in staged}
            self._poll(
                "uploads to be parsed",
                lambda: self._uploads_parsed(project_id, batch_id, staged_ids),
            )
            imported_payload = data_object(
                self.client.post_json(
                    f"/v1/projects/{project_id}/imports",
                    {"batchId": batch_id},
                ),
                "Import",
            )
            import_id = imported_payload.get("importId")
            if not isinstance(import_id, str) or not import_id:
                raise VantageError(
                    "Import response did not include importId",
                    error_code="invalid_response",
                )
            import_ids.append(import_id)
            records = self._poll(
                "the import to finish",
                lambda: self._uploads_imported(project_id, batch_id, staged_ids),
            )
            for record in records:
                upload_id = str(record["uploadId"])
                file_name = names[upload_id]
                item_id = str(record["itemId"])
                imported.append((file_name, item_id))
                self.log(f"Imported {file_name} as {item_id}")

        for import_id in import_ids:
            self._poll(
                f"translation of {', '.join(selected)}",
                lambda import_id=import_id: self._translation_ready(project_id, import_id, selected),
            )

        export_body = {
            "items": [
                {"itemId": item_id, "locales": selected}
                for _, item_id in imported
            ]
        }
        export_payload = data_object(
            self.client.post_json(f"/v1/projects/{project_id}/exports", export_body),
            "Export",
        )
        export_id = export_payload.get("exportId")
        if not isinstance(export_id, str) or not export_id:
            raise VantageError(
                "Export response did not include exportId",
                error_code="invalid_response",
            )
        self.log(f"Exporting {len(imported)} item(s)")
        self._poll(
            "the export",
            lambda: self._export_ready(project_id, export_id),
        )
        download = self._download(project_id, export_id)
        expected = expected_download_names([file_name for file_name, _ in imported], selected)
        written = save_download(download, output, expected)
        saved_names = {path.name for path in written}
        missing = sorted(expected - saved_names)
        if missing:
            self.log(
                "Vantage did not include "
                + ", ".join(missing)
                + ". Those items had no translated content to download."
            )
        for path in written:
            self.log(f"Saved {path}")
        return written

    def _stage(self, project_id: str, files: list[Path]) -> tuple[str, list[tuple[str, Path, dict]]]:
        payload = data_object(
            self.client.post_json(
                f"/v1/projects/{project_id}/uploads",
                {"uploads": [describe_file(path) for path in files]},
            ),
            "Stage",
        )
        batch_id = payload.get("batchId")
        entries = payload.get("uploads")
        if not isinstance(batch_id, str) or not isinstance(entries, list):
            raise VantageError(
                "Stage response did not include a batch",
                error_code="invalid_response",
            )
        by_name = {path.name: path for path in files}
        problems: list[str] = []
        staged: list[tuple[str, Path, dict]] = []
        seen_names: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                problems.append("stage entry was not an object")
                continue
            problem = entry_error(entry)
            name = entry.get("fileName")
            if problem:
                label = name if isinstance(name, str) else "file"
                problems.append(f"{label}: {problem}")
                if isinstance(name, str):
                    seen_names.add(name)
                continue
            upload = entry.get("upload")
            upload_id = entry.get("uploadId")
            if (
                not isinstance(name, str)
                or name not in by_name
                or not isinstance(upload_id, str)
                or not isinstance(upload, dict)
                or "url" not in upload
                or not isinstance(upload.get("fields"), dict)
            ):
                problems.append(f"{name}: incomplete upload target")
                if isinstance(name, str):
                    seen_names.add(name)
                continue
            seen_names.add(name)
            staged.append((upload_id, by_name[name], upload))
        for path in files:
            if path.name not in seen_names:
                problems.append(f"{path.name}: missing from the stage response")
        if problems:
            raise VantageError("; ".join(problems), error_code="stage_failed")
        return batch_id, staged

    def _list_uploads(self, project_id: str, batch_id: str) -> list[dict]:
        uploads: list[dict] = []
        after: str | None = None
        while True:
            query = {"filter-batch-id": batch_id}
            if after:
                query["after"] = after
            payload = self.client.get_json(f"/v1/projects/{project_id}/uploads", query=query)
            page = payload.get("data")
            if not isinstance(page, list):
                raise VantageError(
                    "Upload list response did not include data",
                    error_code="invalid_response",
                )
            uploads.extend(item for item in page if isinstance(item, dict))
            meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
            if not meta.get("hasMore"):
                return uploads
            cursor = meta.get("cursor")
            if not isinstance(cursor, str) or not cursor or cursor == after:
                return uploads
            after = cursor

    def _uploads_parsed(
        self,
        project_id: str,
        batch_id: str,
        staged_ids: list[str],
    ) -> list[dict] | None:
        records = self._records_for_batch(project_id, batch_id, staged_ids)
        if records is None:
            return None
        for record in records:
            failure = upload_failure(record)
            if failure is not None:
                raise failure
            if record.get("status") not in UPLOADED_STATUSES:
                self.log(f"{record.get('fileName')} is {record.get('status')}")
                return None
        return records

    def _uploads_imported(
        self,
        project_id: str,
        batch_id: str,
        staged_ids: list[str],
    ) -> list[dict] | None:
        records = self._records_for_batch(project_id, batch_id, staged_ids)
        if records is None:
            return None
        for record in records:
            failure = upload_failure(record)
            if failure is not None:
                raise failure
            if record.get("status") != "IMPORTED":
                self.log(f"{record.get('fileName')} is {record.get('status')}")
                return None
            if not record.get("itemId"):
                name = record.get("fileName") or record.get("uploadId")
                raise VantageError(
                    f"{name} was imported without an itemId",
                    error_code="missing_item_id",
                )
        return records

    def _records_for_batch(
        self,
        project_id: str,
        batch_id: str,
        staged_ids: list[str],
    ) -> list[dict] | None:
        by_id = {
            record.get("uploadId"): record
            for record in self._list_uploads(project_id, batch_id)
            if record.get("uploadId") in staged_ids
        }
        if any(upload_id not in by_id for upload_id in staged_ids):
            self.log("Waiting for every staged upload to appear")
            return None
        return [by_id[upload_id] for upload_id in staged_ids]

    def _translation_ready(
        self,
        project_id: str,
        import_id: str,
        locales: list[str],
    ) -> dict | None:
        payload = data_object(
            self.client.get_json(f"/v1/projects/{project_id}/translation-statuses/{import_id}"),
            "Translation status",
        )
        if payload.get("status") == "FAILED":
            raise VantageError(
                "Translation failed for this import",
                error_code="translation_failed",
            )
        languages = payload.get("languages") if isinstance(payload.get("languages"), list) else []
        by_locale = {
            locale_key(item["locale"]): item
            for item in languages
            if isinstance(item, dict) and isinstance(item.get("locale"), str)
        }
        pending: list[str] = []
        for locale in locales:
            item = by_locale.get(locale_key(locale))
            status = item.get("status") if isinstance(item, dict) else None
            if not isinstance(status, str) or not language_is_ready(status):
                pending.append(locale)
        if pending:
            overall = payload.get("status")
            self.log(f"AI translation still running for {', '.join(pending)} (import status {overall})")
            return None
        return payload

    def _export_ready(self, project_id: str, export_id: str) -> dict | None:
        payload = data_object(
            self.client.get_json(f"/v1/projects/{project_id}/exports/{export_id}"),
            "Export status",
        )
        status = payload.get("status")
        if status == "COMPLETED":
            return payload
        if status == "FAILED":
            reason = payload.get("failReason") or "Export failed"
            raise VantageError(str(reason), error_code="export_failed")
        self.log(f"Export status {status}")
        return None

    def _download(self, project_id: str, export_id: str) -> Download:
        last_status = None
        for _ in range(2):
            payload = data_object(
                self.client.get_json(
                    f"/v1/projects/{project_id}/exports/{export_id}/download-url"
                ),
                "Download",
            )
            url = payload.get("downloadUrl")
            if not isinstance(url, str) or not url:
                raise VantageError(
                    "Download response did not include downloadUrl",
                    error_code="invalid_response",
                )
            downloaded = self.client.get_download(url)
            if downloaded.status < 400:
                return downloaded
            last_status = downloaded.status
            self.log(f"Download link returned HTTP {downloaded.status}; requesting a new link")
        raise VantageError(
            f"Download failed with HTTP {last_status}",
            error_code="download_failed",
            status=last_status,
        )

    def _poll(self, label: str, check: Callable[[], object | None]) -> object:
        deadline = self.clock() + self.timeout
        while True:
            result = check()
            if result is not None:
                return result
            if self.clock() >= deadline:
                raise VantageError(
                    f"Timed out waiting for {label}",
                    error_code="timeout",
                )
            self.sleep(self.poll_interval)
