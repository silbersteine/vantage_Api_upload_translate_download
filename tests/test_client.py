"""HTTP client behavior: errors, checksums, and the presigned upload body."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from vantage_client.client import (
    HttpResult,
    VantageClient,
    VantageError,
    encode_multipart,
    multipart_fields,
    parse_error_payload,
)
from vantage_client.journey import MAX_FILE_BYTES, md5_hex


def json_result(status: int, payload: dict) -> HttpResult:
    return HttpResult(
        status=status,
        headers={"Content-Type": "application/json"},
        body=json.dumps(payload).encode("utf-8"),
    )


class ErrorParsingTests(unittest.TestCase):
    def test_single_error_object(self) -> None:
        error = parse_error_payload(
            404,
            {
                "message": "The requested resource was not found.",
                "errorCode": "resource_not_found",
            },
        )
        self.assertEqual(error.error_code, "resource_not_found")
        self.assertEqual(error.message, "The requested resource was not found.")
        self.assertEqual(error.status, 404)

    def test_multiple_errors(self) -> None:
        error = parse_error_payload(
            400,
            {
                "errors": [
                    {
                        "message": "Invalid value.",
                        "errorCode": "validation_error",
                        "details": {"field": "name"},
                    },
                    {
                        "message": "Invalid value.",
                        "errorCode": "other_error",
                        "details": {"field": "language"},
                    },
                ]
            },
        )
        self.assertEqual(error.error_code, "validation_error")
        self.assertIn("validation_error: Invalid value.", error.message)
        self.assertIn("other_error: Invalid value.", error.message)

    def test_null_success_body_is_an_empty_object(self) -> None:
        def send(method, url, headers, body, timeout):
            return HttpResult(202, {"Content-Type": "application/json"}, b"null")

        client = VantageClient("secret-token", base_url="https://api.example.test/vantage", send=send)
        self.assertEqual(client.post_json("/v1/projects/p/uploads/actions/complete", {}), {})

    def test_client_raises_the_api_error_code(self) -> None:
        def send(method, url, headers, body, timeout):
            self.assertEqual(headers["X-Api-Token"], "secret-token")
            self.assertIn("/v1/projects/missing", url)
            return json_result(
                404,
                {"message": "Missing project.", "errorCode": "resource_not_found"},
            )

        client = VantageClient("secret-token", base_url="https://api.example.test/vantage", send=send)
        with self.assertRaises(VantageError) as caught:
            client.get_json("/v1/projects/missing")
        self.assertEqual(caught.exception.error_code, "resource_not_found")
        self.assertEqual(caught.exception.message, "Missing project.")


class MultipartTests(unittest.TestCase):
    def test_field_order_puts_the_file_last(self) -> None:
        fields = multipart_fields({"key": "uploads/1", "Policy": "abc"}, "text/markdown")
        body = encode_multipart(
            fields,
            file_name="guide.md",
            mime_type="text/markdown",
            content=b"hello",
            boundary="BOUND",
        )
        text = body.decode("utf-8")
        key_at = text.index('name="key"')
        policy_at = text.index('name="Policy"')
        type_at = text.index('name="Content-Type"')
        file_at = text.index('name="file"')
        self.assertLess(key_at, policy_at)
        self.assertLess(policy_at, type_at)
        self.assertLess(type_at, file_at)
        self.assertIn("text/markdown", text)
        self.assertTrue(text.endswith("hello\r\n--BOUND--\r\n"))
        self.assertNotIn("X-Api-Token", text)

    def test_presigned_post_omits_the_api_token(self) -> None:
        captured = {}

        def send(method, url, headers, body, timeout):
            captured["method"] = method
            captured["url"] = url
            captured["headers"] = headers
            captured["body"] = body
            return HttpResult(204, {}, b"")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "guide.md"
            path.write_bytes(b"hello")
            client = VantageClient(
                "secret-token",
                base_url="https://api.example.test/vantage",
                send=send,
            )
            client.post_file(
                "https://uploads.example.test/target",
                {"key": "uploads/1", "Policy": "abc"},
                "text/markdown",
                path,
                boundary="BOUND",
            )

        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["url"], "https://uploads.example.test/target")
        self.assertNotIn("X-Api-Token", captured["headers"])
        self.assertNotIn(b"secret-token", captured["body"])
        self.assertLess(
            captured["body"].index(b'name="Content-Type"'),
            captured["body"].index(b'name="file"'),
        )

    def test_md5_is_the_hex_digest_of_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "guide.md"
            path.write_bytes(b"abc")
            self.assertEqual(md5_hex(path), hashlib.md5(b"abc").hexdigest())
        self.assertEqual(MAX_FILE_BYTES, 52_428_800)


if __name__ == "__main__":
    unittest.main()
