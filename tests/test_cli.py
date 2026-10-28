"""Command line wiring."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vantage_client.cli import load_env, main


class CliTests(unittest.TestCase):
    def test_missing_token(self) -> None:
        with patch.dict(os.environ, {}, clear=True), patch("vantage_client.cli.load_env"):
            code = main(["--input", "guide.md"])
        self.assertEqual(code, 1)

    def test_missing_project_id(self) -> None:
        with patch.dict(os.environ, {"VANTAGE_API_TOKEN": "secret-token"}, clear=True), patch(
            "vantage_client.cli.load_env"
        ):
            code = main(["--input", "guide.md"])
        self.assertEqual(code, 1)

    def test_dotenv_supplies_the_token_and_project(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                'VANTAGE_API_TOKEN="secret-token"\nVANTAGE_PROJECT_ID=project-1\n',
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True), patch(
                "vantage_client.cli.Path.cwd",
                return_value=Path(directory),
            ), patch("vantage_client.cli.Translator") as translator:
                translator.return_value.run.return_value = []
                code = main(["--input", "guide.md"])
        self.assertEqual(code, 0)
        self.assertEqual(translator.return_value.run.call_args.args[0], "project-1")

    def test_dotenv_does_not_override_existing_variables(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "VANTAGE_API_TOKEN=from-file\nVANTAGE_PROJECT_ID=\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"VANTAGE_API_TOKEN": "already"}, clear=True):
                load_env(env_file)
                self.assertEqual(os.environ["VANTAGE_API_TOKEN"], "already")
            with patch.dict(os.environ, {"VANTAGE_API_TOKEN": ""}, clear=True):
                load_env(env_file)
                self.assertEqual(os.environ["VANTAGE_API_TOKEN"], "from-file")
                self.assertNotIn("VANTAGE_PROJECT_ID", os.environ)

    def test_dotenv_in_a_parent_directory_is_used(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child = root / "source-lang"
            child.mkdir()
            (root / ".env").write_text(
                "VANTAGE_API_TOKEN=from-parent\nVANTAGE_PROJECT_ID=project-1\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True), patch(
                "vantage_client.cli.Path.cwd",
                return_value=child,
            ), patch("vantage_client.cli.Translator") as translator:
                translator.return_value.run.return_value = []
                code = main(["--input", "guide.md"])
        self.assertEqual(code, 0)
        self.assertEqual(translator.return_value.run.call_args.args[0], "project-1")

    def test_input_is_required(self) -> None:
        with patch.dict(os.environ, {"VANTAGE_API_TOKEN": "secret-token"}, clear=True):
            with self.assertRaises(SystemExit) as caught:
                main([])
        self.assertEqual(caught.exception.code, 2)

    def test_locales_are_passed_through(self) -> None:
        with patch.dict(
            os.environ,
            {"VANTAGE_API_TOKEN": "secret-token", "VANTAGE_PROJECT_ID": "project-1"},
            clear=True,
        ):
            with patch("vantage_client.cli.Translator") as translator:
                translator.return_value.run.return_value = []
                code = main(
                    [
                        "--input",
                        "guide.md",
                        "--locales",
                        "de-DE, fr",
                        "--output",
                        "out",
                    ]
                )
        self.assertEqual(code, 0)
        self.assertNotIn("accept_pending_review", translator.call_args.kwargs)
        translator.return_value.run.assert_called_once()
        project_id, inputs, output, locales = translator.return_value.run.call_args.args
        self.assertEqual(project_id, "project-1")
        self.assertEqual([path.name for path in inputs], ["guide.md"])
        self.assertEqual(output.name, "out")
        self.assertEqual(locales, ["de-DE", "fr"])


if __name__ == "__main__":
    unittest.main()
