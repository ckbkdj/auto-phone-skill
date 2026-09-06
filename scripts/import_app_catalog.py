#!/usr/bin/env python3
"""Merge a caller-provided top-N app CSV into the local alias seed.

Expected columns: name, package, aliases(optional, semicolon-separated), category(optional).
The script does not claim to certify the imported applications. It only adds discovery aliases.
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

import yaml

from lobster_phone_agent.apps.registry import AppRecord


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--output", type=Path, default=Path("config/apps.generated.yaml"))
    parser.add_argument("--merge", type=Path, default=Path("config/apps.yaml"))
    args = parser.parse_args()

    existing: list[dict[str, object]] = []
    if args.merge.exists():
        raw = yaml.safe_load(args.merge.read_text("utf-8")) or {}
        existing = raw.get("apps", raw) if isinstance(raw, dict) else raw
    records: dict[str, AppRecord] = {}
    for raw in existing:
        record = AppRecord.model_validate(raw)
        records[record.package or record.name] = record
    imported = 0
    with args.csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            record = AppRecord(
                name=row["name"].strip(),
                package=row["package"].strip() or None,
                aliases=[value.strip() for value in (row.get("aliases") or "").split(";") if value.strip()],
                category=(row.get("category") or "other").strip(),
            )
            key = record.package or record.name
            previous = records.get(key)
            if previous:
                record.aliases = list(dict.fromkeys([*previous.aliases, *record.aliases]))
            records[key] = record
            imported += 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = {"apps": [record.model_dump(exclude_none=True, exclude_defaults=True) for record in records.values()]}
    args.output.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), "utf-8")
    print(f"imported={imported} total={len(records)} output={args.output}")


if __name__ == "__main__":
    main()
