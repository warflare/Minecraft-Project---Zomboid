# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path
import xml.etree.ElementTree as ET


PZ_NAME_RE = re.compile(r"^(?P<tileset>.+)_(?P<local>\d+)$")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tsx", required=True, help="Path to pz_global.tsx")
    ap.add_argument("--out", required=True, help="Output CSV path")
    ap.add_argument("--only-unmapped", action="store_true",
                    help="If you also pass --existing, only output names not found in existing mapping.")
    ap.add_argument("--existing", default="", help="Optional: existing mapping.csv to compare against")
    args = ap.parse_args()

    tsx_path = Path(args.tsx)
    out_path = Path(args.out)
    existing_path = Path(args.existing) if args.existing else None

    if not tsx_path.exists():
        print(f"[ERROR] TSX not found: {tsx_path}", file=sys.stderr)
        return 2

    existing_keys = set()
    if existing_path and existing_path.exists():
        with existing_path.open("r", newline="", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            for row in reader:
                if len(row) < 2:
                    continue
                tileset = (row[0] or "").strip()
                local_id = (row[1] or "").strip()
                if tileset and local_id:
                    existing_keys.add(f"{tileset}_{local_id}")

    tree = ET.parse(tsx_path)
    root = tree.getroot()

    rows = []
    missing_parse = 0
    total = 0
    skipped_existing = 0

    for tile_el in root.findall("tile"):
        total += 1
        tsx_tile_id = tile_el.get("id", "")

        pz_name = None
        props = tile_el.find("properties")
        if props is not None:
            for prop in props.findall("property"):
                if prop.get("name") == "pz_name":
                    pz_name = prop.get("value")
                    break

        if not pz_name:
            continue

        m = PZ_NAME_RE.match(pz_name)
        if not m:
            # If you ever see names that don't end with _<digits>, we'll still output them but blank tileset/local_id
            missing_parse += 1
            tileset, local_id = pz_name, ""
        else:
            tileset = m.group("tileset")
            local_id = m.group("local")

        key = f"{tileset}_{local_id}" if local_id else tileset
        if args.only_unmapped and existing_keys:
            if key in existing_keys:
                skipped_existing += 1
                continue

        # template columns
        rows.append([tileset, local_id, "", "", "", pz_name, tsx_tile_id])

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["tileset", "local_id", "category", "mc_block", "Y-rule", "pz_name", "tsx_tile_id"])
        writer.writerows(rows)

    print(f"Wrote: {out_path}")
    print(f"Tiles in TSX: {total}")
    print(f"Rows written: {len(rows)}")
    if existing_keys and args.only_unmapped:
        print(f"Skipped existing: {skipped_existing}")
    if missing_parse:
        print(f"WARNING: {missing_parse} pz_name values did not match '<tileset>_<digits>' (kept but local_id blank).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())