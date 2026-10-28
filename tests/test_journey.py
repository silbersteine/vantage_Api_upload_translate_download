"""Journey decisions against a fake Vantage HTTP API."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from vantage_client.client import Download, HttpResult, VantageClient, VantageError
from vantage_client.journey import (
    Translator,
    collect_files,
    expected_download_names,
    language_is_ready,
    resolve_locales,
    save_download,
    translated_name,
    validate_member_names,
)


def json_result(payload: dict, status: int = 200) -> HttpResult:
    return HttpResult(
        status=status,
        headers={"Content-Type": "application/json"},
        body=json.dumps(payload).encode("utf-8"),
    )


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeApi:
    """Stateful Vantage stand-in. Upload targets and downloads use other hosts."""

    def __init__(self) -> None:
        self.requests = []
        self.batches: dict[str, list[str]] = {}
        self.upload_names: dict[str, str] = {}
        self.imported: set[str] = set()
        self.export_bodies: list[dict] = []
        self.parse_script = ["UPLOADED"]
        self.parse_index = 0
        self.active_parse_status = "UPLOADED"
        self.translation_script: list[object] = [
            {"de-DE": "COMPLETED", "fr": "COMPLETED"}
        ]
        self.translation_index = 0
        self.export_script = ["COMPLETED"]
        self.export_index = 0
        self.download_statuses = [200]
        self.download_index = 0
        self.content_source = "file"
        self.stage_error: str | None = None
        self.paginate = False
        self.omit_item_id = False
        self.after_import_status = "IMPORTED"
        self.extra_members: list[str] = []
        self.omit_first_member = False
        self.single_download_name: str | None = None
        self.token = "secret-token"

    def __call__(self, method, url, headers, body, timeout):
        self.requests.append(
            {"method": method, "url": url, "headers": dict(headers), "body": body}
        )
        parsed = urlparse(url)
        if parsed.netloc == "uploads.example.test":
            if "X-Api-Token" in headers or (body and self.token.encode() in body):
                raise AssertionError("presigned upload included the API token")
            return HttpResult(204, {}, b"")
        if parsed.netloc == "downloads.example.test":
            if "X-Api-Token" in headers:
                raise AssertionError("download included the API token")
            return self._file_response()
        if headers.get("X-Api-Token") != self.token:
            raise AssertionError(f"missing API token on {method} {url}")
        path = parsed.path
        prefix = "/vantage"
        if path.startswith(prefix):
            path = path[len(prefix) :]
        query = parse_qs(parsed.query)
        return self._route(method, path, query, body)

    def _route(self, method, path, query, body) -> HttpResult:
        project = "/v1/projects/project-1"
        if method == "GET" and path == project:
            return json_result(self._project())
        if method == "POST" and path == f"{project}/uploads":
            return json_result(self._stage(json.loads(body.decode())), status=201)
        if method == "POST" and path == f"{project}/uploads/actions/complete":
            return HttpResult(202, {}, b"")
        if method == "GET" and path == f"{project}/uploads":
            batch_id = query["filter-batch-id"][0]
            after = query.get("after", [None])[0]
            return json_result(self._uploads(batch_id, after))
        if method == "POST" and path == f"{project}/imports":
            payload = json.loads(body.decode())
            batch_id = payload["batchId"]
            self.imported.add(batch_id)
            return json_result({"data": {"importId": f"import-{batch_id}"}}, status=202)
        if method == "GET" and "/translation-statuses/" in path:
            return json_result({"data": self._translation(path.rsplit("/", 1)[-1])})
        if method == "POST" and path == f"{project}/exports":
            payload = json.loads(body.decode())
            self.export_bodies.append(payload)
            return json_result(
                {"data": {"exportId": "export-1", "status": "IN_PROGRESS"}},
                status=202,
            )
        if method == "GET" and path.endswith("/download-url"):
            name, raw, is_zip = self._download_payload()
            suffix = "translations.zip" if is_zip else name
            return json_result(
                {"data": {"downloadUrl": f"https://downloads.example.test/export/{suffix}"}}
            )
        if method == "GET" and path.startswith(f"{project}/exports/"):
            return json_result({"data": self._export_status()})
        raise AssertionError(f"unexpected {method} {path}")

    def _project(self) -> dict:
        return {
            "data": {
                "projectId": "project-1",
                "name": "Docs",
                "contentSourceType": self.content_source,
                "baseLanguage": {"languageId": "lang-en", "locale": "en"},
                "targetLanguages": [
                    {"languageId": "lang-de", "locale": "de-DE"},
                    {"languageId": "lang-fr", "locale": "fr"},
                ],
            }
        }

    def _stage(self, payload: dict) -> dict:
        batch_id = f"batch-{len(self.batches) + 1}"
        uploads = []
        self.batches[batch_id] = []
        for item in payload["uploads"]:
            name = item["fileName"]
            if self.stage_error:
                uploads.append(
                    {
                        "fileName": name,
                        "errorCode": self.stage_error,
                        "message": "rejected",
                    }
                )
                continue
            upload_id = f"upload-{name}"
            self.batches[batch_id].append(upload_id)
            self.upload_names[upload_id] = name
            uploads.append(
                {
                    "fileName": name,
                    "uploadId": upload_id,
                    "status": "UPLOADING",
                    "upload": {
                        "url": f"https://uploads.example.test/{upload_id}",
                        "fields": {"key": upload_id, "Policy": "policy"},
                    },
                }
            )
        return {"data": {"batchId": batch_id, "uploads": uploads}}

    def _uploads(self, batch_id: str, after: str | None) -> dict:
        if after is None:
            self.active_parse_status = self.parse_script[self.parse_index]
            if self.parse_index < len(self.parse_script) - 1:
                self.parse_index += 1
        records = []
        if self.paginate and after is None:
            records.append(self._record("upload-foreign", "foreign.md", batch_id))
            return {"data": records, "meta": {"hasMore": True, "cursor": "cursor-1", "count": 1}}
        for upload_id in self.batches[batch_id]:
            records.append(self._record(upload_id, self.upload_names[upload_id], batch_id))
        records.append(self._record("upload-foreign", "foreign.md", batch_id))
        return {"data": records, "meta": {"hasMore": False, "count": len(records)}}

    def _record(self, upload_id: str, file_name: str, batch_id: str) -> dict:
        if batch_id in self.imported and upload_id != "upload-foreign":
            status = self.after_import_status
        elif upload_id == "upload-foreign":
            status = "UPLOADED"
        else:
            status = self.active_parse_status
        record = {
            "uploadId": upload_id,
            "batchId": batch_id,
            "fileName": file_name,
            "status": status,
        }
        if status == "IMPORTED" and not (self.omit_item_id and upload_id != "upload-foreign"):
            record["itemId"] = f"item-{file_name}"
        if status in {"UPLOAD_FAILED", "IMPORT_FAILED"}:
            record["error"] = {"errorCode": "PARSE_ERROR", "message": "could not parse"}
        return record

    def _translation(self, import_id: str) -> dict:
        step = self.translation_script[self.translation_index]
        if self.translation_index < len(self.translation_script) - 1:
            self.translation_index += 1
        if step == "FAILED":
            return {
                "projectId": "project-1",
                "importId": import_id,
                "status": "FAILED",
                "languages": [],
            }
        languages = []
        assert isinstance(step, dict)
        for locale, status in step.items():
            languages.append({"languageId": f"lang-{locale}", "locale": locale, "status": status})
        overall = "COMPLETED"
        if any(status == "TRANSLATING" for status in step.values()):
            overall = "TRANSLATING"
        elif any(status == "PENDING_REVIEW" for status in step.values()):
            overall = "PENDING_REVIEW"
        return {
            "projectId": "project-1",
            "importId": import_id,
            "status": overall,
            "languages": languages,
        }

    def _export_status(self) -> dict:
        status = self.export_script[self.export_index]
        if self.export_index < len(self.export_script) - 1:
            self.export_index += 1
        if status == "FAILED":
            return {"exportId": "export-1", "status": "FAILED", "failReason": "disk full"}
        return {"exportId": "export-1", "status": status, "failReason": None}

    def _download_payload(self) -> tuple[str, bytes, bool]:
        names = []
        for item in self.export_bodies[-1]["items"]:
            file_name = item["itemId"].removeprefix("item-")
            for locale in item["locales"]:
                names.append(translated_name(file_name, locale))
        if self.omit_first_member and names:
            names = names[1:]
        names.extend(self.extra_members)
        one_item = len(self.export_bodies[-1]["items"]) == 1
        one_locale = one_item and len(self.export_bodies[-1]["items"][0]["locales"]) == 1
        if (
            self.single_download_name
            and one_locale
            and not self.extra_members
            and not self.omit_first_member
        ):
            return self.single_download_name, b"wrong", False
        if len(names) == 1 and not self.extra_members and not self.omit_first_member:
            return names[0], b"PK\x03\x04translated", False
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for name in names:
                archive.writestr(name, f"content {name}")
        return "translations.zip", buffer.getvalue(), True

    def _file_response(self) -> HttpResult:
        status = self.download_statuses[min(self.download_index, len(self.download_statuses) - 1)]
        if self.download_index < len(self.download_statuses) - 1:
            self.download_index += 1
        if status >= 400:
            return HttpResult(status, {}, b"expired")
        name, raw, is_zip = self._download_payload()
        headers = {}
        if not is_zip:
            shown = self.single_download_name or name
            headers["Content-Disposition"] = f'attachment; filename="{shown}"'
        return HttpResult(status, headers, raw)

    def calls(self, method: str, suffix: str) -> list[dict]:
        matched = []
        for request in self.requests:
            parsed = urlparse(request["url"])
            if request["method"] == method and parsed.path.endswith(suffix):
                matched.append(request)
        return matched


class LocaleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.languages = [
            {"languageId": "1", "locale": "de-DE"},
            {"languageId": "2", "locale": "fr"},
        ]

    def test_defaults_to_every_target_locale(self) -> None:
        self.assertEqual(resolve_locales(self.languages, None), ["de-DE", "fr"])

    def test_matching_accepts_case_and_underscore(self) -> None:
        self.assertEqual(resolve_locales(self.languages, ["FR", "de_DE"]), ["fr", "de-DE"])

    def test_unknown_locale_is_rejected(self) -> None:
        with self.assertRaises(VantageError) as caught:
            resolve_locales(self.languages, ["en"])
        self.assertEqual(caught.exception.error_code, "unknown_locale")

    def test_translated_name_inserts_the_locale_before_the_extension(self) -> None:
        self.assertEqual(translated_name("guide.docx", "de-DE"), "guide.de-DE.docx")

    def test_pending_review_is_ready_to_download(self) -> None:
        self.assertTrue(language_is_ready("COMPLETED"))
        self.assertTrue(language_is_ready("PENDING_REVIEW"))
        self.assertFalse(language_is_ready("TRANSLATING"))
        self.assertFalse(language_is_ready("NEW_STATUS"))


class FileCollectionTests(unittest.TestCase):
    def test_directory_walk_skips_unsupported_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "guide.md").write_text("hello", encoding="utf-8")
            (root / "photo.png").write_bytes(b"png")
            nested = root / "nested"
            nested.mkdir()
            (nested / "notes.docx").write_bytes(b"doc")
            found = collect_files([root])
        self.assertEqual([path.name for path in found], ["guide.md", "notes.docx"])

    def test_json_po_yaml_and_xliff_are_not_supported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "guide.md").write_text("hello", encoding="utf-8")
            for name in ("strings.json", "copy.yaml", "copy.yml", "messages.po", "guide.xliff", "guide.xlf"):
                (root / name).write_text("{}", encoding="utf-8")
            found = collect_files([root])
            self.assertEqual([path.name for path in found], ["guide.md"])
            for name in ("strings.json", "copy.yaml", "copy.yml", "messages.po", "guide.xliff", "guide.xlf"):
                with self.assertRaises(VantageError) as caught:
                    collect_files([root / name])
                self.assertEqual(caught.exception.error_code, "unsupported_file_extension")

    def test_explicit_unsupported_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "photo.png"
            path.write_bytes(b"png")
            with self.assertRaises(VantageError) as caught:
                collect_files([path])
        self.assertEqual(caught.exception.error_code, "unsupported_file_extension")

    def test_oversize_file_is_rejected_before_any_network_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "guide.md"
            path.write_bytes(b"0123456789X")
            with self.assertRaises(VantageError) as caught:
                collect_files([path], max_bytes=10)
        self.assertEqual(caught.exception.error_code, "file_too_large")

    def test_duplicate_file_names_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "a"
            second = root / "b"
            first.mkdir()
            second.mkdir()
            (first / "guide.md").write_text("one", encoding="utf-8")
            (second / "guide.md").write_text("two", encoding="utf-8")
            with self.assertRaises(VantageError) as caught:
                collect_files([root])
        self.assertEqual(caught.exception.error_code, "duplicate_file_name")


class DownloadCheckTests(unittest.TestCase):
    def test_extra_archive_names_fail_and_missing_names_are_reported(self) -> None:
        expected = expected_download_names(["guide.md"], ["de-DE", "fr"])
        with self.assertRaises(VantageError) as extra:
            validate_member_names(["guide.de-DE.md", "guide.fr.md", "other.fr.md"], expected)
        self.assertEqual(extra.exception.error_code, "unexpected_download")
        self.assertIn("other.fr.md", extra.exception.message)
        missing = validate_member_names(["guide.de-DE.md"], expected)
        self.assertEqual(missing, {"guide.fr.md"})

    def test_archive_paths_are_not_extracted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "out"
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                archive.writestr("../secret.md", b"nope")
                archive.writestr("guide.de-DE.md", b"ok")
            download = Download(
                status=200,
                headers={},
                body=buffer.getvalue(),
                url="https://downloads.example.test/export/translations.zip",
            )
            with self.assertRaises(VantageError):
                save_download(download, output, {"guide.de-DE.md"})
            self.assertFalse((root / "secret.md").exists())
            self.assertEqual(list(output.iterdir()) if output.exists() else [], [])

    def test_single_docx_is_saved_without_unzipping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            body = b"PK\x03\x04this is a docx, not an export archive"
            download = Download(
                status=200,
                headers={"Content-Disposition": 'attachment; filename="guide.de-DE.docx"'},
                body=body,
                url="https://downloads.example.test/export/guide.de-DE.docx",
            )
            written = save_download(download, output, {"guide.de-DE.docx"})
            self.assertEqual(written[0].read_bytes(), body)

    def test_one_returned_file_is_saved_when_several_were_requested(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            body = "# Cómo empezar\n".encode()
            download = Download(
                status=200,
                headers={"Content-Disposition": "attachment; filename*=UTF-8''file2.es.md"},
                body=body,
                url="https://downloads.example.test/export/ef82a7f4-fde8-4170-9519-fe826f75029b",
            )
            written = save_download(
                download,
                output,
                {"file2.es.md", "getting-started.es.md"},
            )
            self.assertEqual([path.name for path in written], ["file2.es.md"])
            self.assertEqual(written[0].read_bytes(), body)


class JourneyTests(unittest.TestCase):
    def run_journey(self, api: FakeApi, files: list[Path], output: Path, **kwargs):
        clock = kwargs.pop("clock", Clock())
        log = kwargs.pop("log", lambda _message: None)
        locales = kwargs.pop("locales", ["de-DE"])
        client = VantageClient(
            api.token,
            base_url="https://api.example.test/vantage",
            send=api,
        )
        translator = Translator(
            client,
            clock=clock,
            sleep=clock.sleep,
            log=log,
            **kwargs,
        )
        return translator.run("project-1", files, output, locales)

    def test_export_contains_only_this_runs_item_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            alpha = root / "alpha.md"
            beta = root / "beta.md"
            alpha.write_text("alpha", encoding="utf-8")
            beta.write_text("beta", encoding="utf-8")
            output = root / "translated"
            api = FakeApi()
            api.paginate = True
            logs: list[str] = []
            written = self.run_journey(api, [alpha, beta], output, log=logs.append, locales=["de_DE"])
            alpha_md5 = md5_of(alpha)

        export = api.export_bodies[0]
        self.assertEqual(set(export), {"items"})
        self.assertNotIn("languageIds", export)
        self.assertEqual(
            [item["itemId"] for item in export["items"]],
            ["item-alpha.md", "item-beta.md"],
        )
        self.assertEqual(export["items"][0]["locales"], ["de-DE"])
        self.assertNotIn("item-foreign.md", json.dumps(export))
        self.assertEqual(sorted(path.name for path in written), ["alpha.de-DE.md", "beta.de-DE.md"])
        self.assertTrue(all("secret-token" not in line for line in logs))
        stage = json.loads(api.calls("POST", "/uploads")[0]["body"].decode())
        checksums = {item["fileName"]: item["checksum"] for item in stage["uploads"]}
        self.assertEqual(checksums["alpha.md"], alpha_md5)
        upload_hosts = [urlparse(request["url"]).netloc for request in api.requests]
        self.assertIn("uploads.example.test", upload_hosts)
        self.assertNotIn("en", json.dumps(export))

    def test_all_target_locales_exclude_the_base_language(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            guide = root / "guide.md"
            guide.write_text("hello", encoding="utf-8")
            api = FakeApi()
            written = self.run_journey(api, [guide], root / "out", locales=None)
        export = api.export_bodies[0]
        self.assertEqual(export["items"][0]["locales"], ["de-DE", "fr"])
        self.assertEqual(
            sorted(path.name for path in written),
            ["guide.de-DE.md", "guide.fr.md"],
        )

    def test_batches_still_export_one_item_list(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = []
            for name in ("alpha.md", "beta.md"):
                path = root / name
                path.write_text(name, encoding="utf-8")
                files.append(path)
            api = FakeApi()
            self.run_journey(api, files, root / "out", batch_size=1, locales=["fr"])
        self.assertEqual(len(api.batches), 2)
        self.assertEqual(len(api.calls("POST", "/imports")), 2)
        self.assertEqual(len(api.export_bodies), 1)
        self.assertEqual(
            [item["itemId"] for item in api.export_bodies[0]["items"]],
            ["item-alpha.md", "item-beta.md"],
        )

    def test_pending_review_downloads_without_waiting_for_reviewers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            guide = root / "guide.md"
            guide.write_text("hello", encoding="utf-8")
            api = FakeApi()
            api.translation_script = [{"de-DE": "PENDING_REVIEW", "fr": "TRANSLATING"}]
            written = self.run_journey(api, [guide], root / "out", timeout=0, locales=["de-DE"])
        self.assertEqual([path.name for path in written], ["guide.de-DE.md"])
        self.assertEqual(len(api.calls("GET", "/translation-statuses/import-batch-1")), 1)

    def test_translating_keeps_polling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            guide = root / "guide.md"
            guide.write_text("hello", encoding="utf-8")
            api = FakeApi()
            api.translation_script = [{"de-DE": "TRANSLATING"}]
            with self.assertRaises(VantageError) as caught:
                self.run_journey(api, [guide], root / "out", timeout=0, locales=["de-DE"])
        self.assertEqual(caught.exception.error_code, "timeout")
        self.assertEqual(api.export_bodies, [])

    def test_unknown_translation_status_keeps_polling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            guide = root / "guide.md"
            guide.write_text("hello", encoding="utf-8")
            api = FakeApi()
            api.translation_script = [
                {"de-DE": "NEW_STATUS"},
                {"de-DE": "COMPLETED"},
            ]
            self.run_journey(api, [guide], root / "out", timeout=30, poll_interval=5)
        self.assertEqual(len(api.calls("GET", "/translation-statuses/import-batch-1")), 2)

    def test_upload_failure_stops_before_import(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            guide = root / "guide.md"
            guide.write_text("hello", encoding="utf-8")
            api = FakeApi()
            api.parse_script = ["UPLOAD_FAILED"]
            with self.assertRaises(VantageError) as caught:
                self.run_journey(api, [guide], root / "out", timeout=0)
        self.assertEqual(caught.exception.error_code, "PARSE_ERROR")
        self.assertIn("could not parse", caught.exception.message)
        self.assertEqual(api.calls("POST", "/imports"), [])

    def test_import_failure_stops_before_export(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            guide = root / "guide.md"
            guide.write_text("hello", encoding="utf-8")
            api = FakeApi()
            api.after_import_status = "IMPORT_FAILED"
            with self.assertRaises(VantageError) as caught:
                self.run_journey(api, [guide], root / "out", timeout=0)
        self.assertEqual(caught.exception.error_code, "PARSE_ERROR")
        self.assertEqual(api.export_bodies, [])

    def test_missing_item_id_does_not_widen_the_export(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            guide = root / "guide.md"
            guide.write_text("hello", encoding="utf-8")
            api = FakeApi()
            api.omit_item_id = True
            with self.assertRaises(VantageError) as caught:
                self.run_journey(api, [guide], root / "out", timeout=0)
        self.assertEqual(caught.exception.error_code, "missing_item_id")
        self.assertEqual(api.export_bodies, [])

    def test_translation_and_export_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            guide = root / "guide.md"
            guide.write_text("hello", encoding="utf-8")
            failed_translation = FakeApi()
            failed_translation.translation_script = ["FAILED"]
            with self.assertRaises(VantageError) as caught:
                self.run_journey(failed_translation, [guide], root / "out", timeout=0)
            self.assertEqual(caught.exception.error_code, "translation_failed")

            failed_export = FakeApi()
            failed_export.export_script = ["FAILED"]
            with self.assertRaises(VantageError) as exported:
                self.run_journey(failed_export, [guide], root / "export-out", timeout=0)
            self.assertEqual(exported.exception.error_code, "export_failed")
            self.assertIn("disk full", exported.exception.message)
            self.assertEqual(failed_export.calls("GET", "/download-url"), [])

    def test_stage_error_uploads_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            guide = root / "guide.md"
            guide.write_text("hello", encoding="utf-8")
            api = FakeApi()
            api.stage_error = "UNSUPPORTED_FILE_EXTENSION"
            with self.assertRaises(VantageError) as caught:
                self.run_journey(api, [guide], root / "out")
        self.assertEqual(caught.exception.error_code, "stage_failed")
        self.assertEqual(
            [urlparse(request["url"]).netloc for request in api.requests],
            ["api.example.test", "api.example.test"],
        )

    def test_non_file_project_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            guide = root / "guide.md"
            guide.write_text("hello", encoding="utf-8")
            api = FakeApi()
            api.content_source = "google-drive"
            with self.assertRaises(VantageError) as caught:
                self.run_journey(api, [guide], root / "out")
        self.assertEqual(caught.exception.error_code, "project_type_not_supported")
        self.assertEqual(api.calls("POST", "/uploads"), [])

    def test_extra_download_name_is_not_written(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            guide = root / "guide.md"
            guide.write_text("hello", encoding="utf-8")
            output = root / "out"
            api = FakeApi()
            api.extra_members = ["other.de-DE.md"]
            with self.assertRaises(VantageError) as caught:
                self.run_journey(api, [guide], output, locales=["de-DE", "fr"])
            self.assertEqual(list(output.iterdir()), [])
        self.assertEqual(caught.exception.error_code, "unexpected_download")
        self.assertIn("other.de-DE.md", caught.exception.message)

    def test_missing_download_name_still_saves_the_files_that_arrived(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            guide = root / "guide.md"
            guide.write_text("hello", encoding="utf-8")
            output = root / "out"
            api = FakeApi()
            api.omit_first_member = True
            logs: list[str] = []
            written = self.run_journey(
                api, [guide], output, locales=["de-DE", "fr"], log=logs.append
            )
            saved = sorted(path.name for path in written)
        self.assertEqual(saved, ["guide.fr.md"])
        self.assertTrue(any("guide.de-DE.md" in line for line in logs))

    def test_single_file_name_must_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            guide = root / "guide.md"
            guide.write_text("hello", encoding="utf-8")
            output = root / "out"
            api = FakeApi()
            api.single_download_name = "wrong.md"
            with self.assertRaises(VantageError) as caught:
                self.run_journey(api, [guide], output, locales=["de-DE"])
            self.assertEqual(list(output.iterdir()), [])
        self.assertIn("wrong.md", caught.exception.message)

    def test_expired_download_link_is_requested_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            guide = root / "guide.md"
            guide.write_text("hello", encoding="utf-8")
            api = FakeApi()
            api.download_statuses = [403, 200]
            written = self.run_journey(api, [guide], root / "out", locales=["de-DE"])
            saved = written[0].read_bytes()
        self.assertEqual(len(api.calls("GET", "/download-url")), 2)
        self.assertEqual(saved, b"PK\x03\x04translated")

    def test_parse_poll_waits_until_uploaded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            guide = root / "guide.md"
            guide.write_text("hello", encoding="utf-8")
            api = FakeApi()
            api.parse_script = ["UPLOADING", "UPLOADED"]
            clock = Clock()
            self.run_journey(api, [guide], root / "out", timeout=30, poll_interval=5, clock=clock)
        self.assertGreaterEqual(clock.now, 5)
        self.assertEqual(len(api.export_bodies), 1)


def md5_of(path: Path) -> str:
    import hashlib

    return hashlib.md5(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    unittest.main()
