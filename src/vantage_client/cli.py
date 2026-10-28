"""Command line for the Vantage file journey."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from vantage_client.client import DEFAULT_BASE_URL, VantageClient, VantageError
from vantage_client.journey import Translator


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vantage-translate",
        description="Upload files to a Lokalise Vantage project and download the translations.",
    )
    parser.add_argument(
        "--input",
        action="append",
        required=True,
        dest="inputs",
        help="File or directory to send. Repeat for more than one path. Directories are walked.",
    )
    parser.add_argument(
        "--output",
        default="translated",
        help="Directory for the translated files. Default: translated",
    )
    parser.add_argument(
        "--project-id",
        default=None,
        help="Vantage project UUID. Defaults to the VANTAGE_PROJECT_ID environment variable.",
    )
    parser.add_argument(
        "--locales",
        default=None,
        help="Comma-separated target locales to download. Default: every target language on the project.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=1800,
        help="Seconds to wait for each phase: upload parse, import, translation, and export. Default: 1800.",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=5,
        help="Seconds between status checks. Default: 5.",
    )
    parser.add_argument(
        "--api-base",
        default=DEFAULT_BASE_URL,
        help=f"Vantage API base URL. Default: {DEFAULT_BASE_URL}",
    )
    return parser


def _find_env_file(start: Path) -> Path | None:
    current = start.resolve()
    for directory in (current, *current.parents):
        candidate = directory / ".env"
        if candidate.is_file():
            return candidate
    return None


def load_env(path: Path | None = None) -> None:
    """Fill unset environment variables from the nearest .env file."""
    env_path = path or _find_env_file(Path.cwd())
    if env_path is None or not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key and value and not os.environ.get(key):
            os.environ[key] = value


def parse_locales(value: str | None) -> list[str] | None:
    if value is None or not value.strip():
        return None
    locales = [part.strip() for part in value.split(",") if part.strip()]
    return locales or None


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.timeout < 0:
        parser.error("--timeout must be zero or greater")
    if args.poll_interval <= 0:
        parser.error("--poll-interval must be greater than zero")

    load_env()
    token = os.environ.get("VANTAGE_API_TOKEN")
    if not token:
        print("missing_token: VANTAGE_API_TOKEN is not set", file=sys.stderr)
        return 1
    project_id = args.project_id or os.environ.get("VANTAGE_PROJECT_ID")
    if not project_id:
        print(
            "missing_project: set VANTAGE_PROJECT_ID or pass --project-id",
            file=sys.stderr,
        )
        return 1

    client = VantageClient(token, base_url=args.api_base)
    translator = Translator(
        client,
        timeout=args.timeout,
        poll_interval=args.poll_interval,
    )
    try:
        translator.run(
            project_id,
            [Path(value) for value in args.inputs],
            Path(args.output),
            parse_locales(args.locales),
        )
    except VantageError as exc:
        if exc.error_code and not exc.message.startswith(str(exc.error_code)):
            print(f"{exc.error_code}: {exc.message}", file=sys.stderr)
        else:
            print(exc.message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
