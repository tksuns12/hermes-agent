#!/usr/bin/env python3
"""Generate representative DOCX/XLSX fixtures for M004/S04 live browser proof."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.e2e.office_fixture_builder import build_representative_office_fixtures


def _to_repo_relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path.resolve())


def _fixture_summary(path: Path) -> dict[str, object]:
    return {
        "path": _to_repo_relative(path),
        "filename": path.name,
        "bytes": path.stat().st_size,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate representative DOCX/XLSX fixtures used by S04 live verification.",
    )
    parser.add_argument(
        "--output-dir",
        default="tmp/m004-s04-office-samples",
        help="Target directory for generated fixtures (default: tmp/m004-s04-office-samples).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable output.",
    )
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    output_dir = (REPO_ROOT / args.output_dir).resolve()
    fixtures = build_representative_office_fixtures(output_dir)

    docx_path = fixtures["docx"]
    xlsx_path = fixtures["xlsx"]

    payload = {
        "output_dir": _to_repo_relative(output_dir),
        "docx": _fixture_summary(docx_path),
        "xlsx": _fixture_summary(xlsx_path),
    }

    if args.json:
        print(json.dumps(payload, sort_keys=True))
        return 0

    print("Generated representative Office fixtures for M004/S04 live browser proof.")
    print(f"output_dir={payload['output_dir']}")
    print(
        "docx="
        + f"{payload['docx']['path']}"
        + f" bytes={payload['docx']['bytes']}"
    )
    print(
        "xlsx="
        + f"{payload['xlsx']['path']}"
        + f" bytes={payload['xlsx']['bytes']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
