# -*- coding: utf-8 -*-
"""
Project Zomboid Build 42 map exporter:
- Reads .lotheader and world_X_Y.lotpack
- Emits TMX files (one per cell per Z) with "stratum" layers
- Writes a global TSX tileset mapping gid -> PZ tile name (property: pz_name)

Designed for large vanilla exports (thousands of TMX files).
No external dependencies.

USAGE (PowerShell example):
  python pz_b42_to_tmx.py `
    --map-dir "C:\Program Files (x86)\Steam\steamapps\common\ProjectZomboid\media\maps\Muldraugh, KY" `
    --out-dir "D:\pz_tmx_out" `
    --workers 6

DEBUG: export ONE TMX only (one cell + one z)
  python pz_b42_to_tmx.py `
    --map-dir "C:\Program Files (x86)\Steam\steamapps\common\ProjectZomboid\media\maps\Muldraugh, KY" `
    --out-dir "D:\pz_tmx_test" `
    --workers 1 `
    --one "0_18" `
    --one-z 0 `
    --flat
"""

from __future__ import annotations

import argparse
import os
import re
import struct
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple, Optional
import xml.etree.ElementTree as ET


LOTHEADER_RE = re.compile(r"^(-?\d+)_(-?\d+)\.lotheader$", re.IGNORECASE)
LOTPACK_RE = re.compile(r"^world_(-?\d+)_(-?\d+)\.lotpack$", re.IGNORECASE)


@dataclass(frozen=True)
class CellKey:
    x: int
    y: int


def _read_i32(buf: bytes, off: int) -> Tuple[int, int]:
    return struct.unpack_from("<i", buf, off)[0], off + 4


def _read_u64(buf: bytes, off: int) -> Tuple[int, int]:
    return struct.unpack_from("<Q", buf, off)[0], off + 8


def parse_tile_names_from_lotheader(path: Path) -> List[str]:
    """
    B42 lotheader begins with:
      0x00: 'LOTH'
      0x04: int32 version
      0x08: int32 tile_count
      0x0C: tile_count lines of UTF-8 strings separated by '\n'
    Followed by additional binary structures (rooms/buildings/zpop/etc) we don't need for tiles.
    """
    data = path.read_bytes()
    if data[:4] != b"LOTH":
        raise ValueError(f"{path.name}: not a LOTH file")
    version = struct.unpack_from("<i", data, 4)[0]
    if version not in (0, 1):
        print(f"[WARN] {path.name}: unexpected LOTH version {version}", file=sys.stderr)

    tile_count = struct.unpack_from("<i", data, 8)[0]
    off = 12

    names: List[str] = []
    for _ in range(tile_count):
        nl = data.find(b"\n", off)
        if nl == -1:
            raise ValueError(f"{path.name}: ran out of tile-name lines (expected {tile_count})")
        s = data[off:nl].decode("utf-8", errors="replace")
        names.append(s)
        off = nl + 1

    return names


def parse_lotpack_blocks(path: Path) -> Tuple[List[int], bytes]:
    """
    Returns (block_offsets, file_bytes)

    B42 lotpack layout:
      0x00: 'LOTP'
      0x04: int32 version
      0x08: int32 nblocks  (expected 1024 for 32x32 blocks)
      0x0C: uint64 offsets[nblocks]  (start of each block's payload, relative to file start)

    Block payload encodes one or more Z-layers sequentially.
    Each Z-layer encodes 8x8 squares (64 squares) in row-major order using RLE:
      read int32 count
        if count == -1:
          read int32 skip
          emit skip empty squares
        else:
          read int32 room_id (discard)
          read (count-1) int32 tile_indices
          emit this square's tile stack

    After 64 squares, if bytes remain in the block payload, that indicates another Z-layer stream.
    """
    data = path.read_bytes()
    if data[:4] != b"LOTP":
        raise ValueError(f"{path.name}: not a LOTP file")
    version = struct.unpack_from("<i", data, 4)[0]
    if version not in (0, 1):
        print(f"[WARN] {path.name}: unexpected LOTP version {version}", file=sys.stderr)

    nblocks = struct.unpack_from("<i", data, 8)[0]
    if nblocks <= 0:
        raise ValueError(f"{path.name}: invalid nblocks={nblocks}")

    off = 12
    offsets: List[int] = []
    for _ in range(nblocks):
        u, off = _read_u64(data, off)
        offsets.append(u)

    return offsets, data


def decode_block_layers(block_bytes: bytes) -> List[List[List[int]]]:
    """
    Decode one block's payload into:
      layers[z][square_index] = [tile_idx0, tile_idx1, ...]
    square_index is 0..63 (8x8 row-major)
    """
    layers: List[List[List[int]]] = []
    off = 0
    blen = len(block_bytes)

    while off + 4 <= blen:
        squares: List[List[int]] = []
        square_count = 0

        # decode exactly 64 squares (RLE can skip)
        while square_count < 64 and off + 4 <= blen:
            count, off = _read_i32(block_bytes, off)
            if count == -1:
                if off + 4 > blen:
                    break
                skip, off = _read_i32(block_bytes, off)
                if skip < 0:
                    break
                take = min(skip, 64 - square_count)
                squares.extend([[]] * take)
                square_count += take
            else:
                if off + 4 > blen:
                    break
                _room_id, off = _read_i32(block_bytes, off)

                tiles: List[int] = []
                nt = max(0, count - 1)
                if off + 4 * nt > blen:
                    break
                for _ in range(nt):
                    t, off = _read_i32(block_bytes, off)
                    tiles.append(t)

                squares.append(tiles)
                square_count += 1

        if square_count == 0:
            break

        if len(squares) < 64:
            squares.extend([[]] * (64 - len(squares)))
        else:
            squares = squares[:64]

        layers.append(squares)

        if off >= blen:
            break
        if blen - off < 8:
            break

    return layers


def write_global_tsx(out_tsx: Path, name_to_gid: Dict[str, int]) -> None:
    """
    A "property-only" tileset: each gid maps to a PZ tile name.
    No image is provided (intended for machine consumption / translation).
    """
    tileset = ET.Element("tileset", {
        "version": "1.10",
        "tiledversion": "1.10.2",
        "name": "pz_global",
        "tilewidth": "1",
        "tileheight": "1",
        "tilecount": str(len(name_to_gid)),
        "columns": "0",
    })

    items = sorted(name_to_gid.items(), key=lambda kv: kv[1])
    for tile_name, gid in items:
        tile = ET.SubElement(tileset, "tile", {"id": str(gid - 1)})
        props = ET.SubElement(tile, "properties")
        ET.SubElement(props, "property", {"name": "pz_name", "value": tile_name})

    tree = ET.ElementTree(tileset)
    out_tsx.parent.mkdir(parents=True, exist_ok=True)
    tree.write(out_tsx, encoding="utf-8", xml_declaration=True)


def write_tmx(
    out_tmx: Path,
    tsx_rel: str,
    map_width: int,
    map_height: int,
    layers: List[List[int]],
    layer_names: List[str],
) -> None:
    """
    Write a TMX with CSV-encoded layers (one tile layer per stratum).
    Each layer is a flat list length map_width*map_height in row-major order.
    """
    if len(layers) != len(layer_names):
        raise ValueError("layers and layer_names length mismatch")

    m = ET.Element("map", {
        "version": "1.10",
        "tiledversion": "1.10.2",
        "orientation": "orthogonal",
        "renderorder": "right-down",
        "width": str(map_width),
        "height": str(map_height),
        "tilewidth": "1",
        "tileheight": "1",
        "infinite": "0",
    })

    ET.SubElement(m, "tileset", {"firstgid": "1", "source": tsx_rel})

    for lname, data in zip(layer_names, layers):
        layer_el = ET.SubElement(m, "layer", {
            "name": lname,
            "width": str(map_width),
            "height": str(map_height),
        })
        data_el = ET.SubElement(layer_el, "data", {"encoding": "csv"})

        rows = []
        idx = 0
        for _y in range(map_height):
            row = ",".join(str(v) for v in data[idx: idx + map_width])
            rows.append(row)
            idx += map_width
        data_el.text = "\n" + ",\n".join(rows) + "\n"

    tree = ET.ElementTree(m)
    out_tmx.parent.mkdir(parents=True, exist_ok=True)
    tree.write(out_tmx, encoding="utf-8", xml_declaration=True)


def build_global_registry(map_dir: Path) -> Tuple[Dict[str, int], Dict[CellKey, Path]]:
    """
    Scans all *.lotheader and builds:
      - name_to_gid: unique PZ tile names across the whole map folder
      - cell_to_lotheader: mapping of (x,y)->lotheader path
    """
    cell_to_lotheader: Dict[CellKey, Path] = {}
    unique_names: Dict[str, None] = {}

    lotheaders = sorted(map_dir.glob("*.lotheader"))
    if not lotheaders:
        raise FileNotFoundError(f"No .lotheader files found in: {map_dir}")

    for p in lotheaders:
        m = LOTHEADER_RE.match(p.name)
        if not m:
            continue
        cx, cy = int(m.group(1)), int(m.group(2))
        cell_to_lotheader[CellKey(cx, cy)] = p

        names = parse_tile_names_from_lotheader(p)
        for n in names:
            unique_names.setdefault(n, None)

    all_names = sorted(unique_names.keys())
    name_to_gid: Dict[str, int] = {n: i + 1 for i, n in enumerate(all_names)}
    return name_to_gid, cell_to_lotheader


def export_one_cell(
    cell: CellKey,
    lotheader_path: Path,
    lotpack_path: Path,
    name_to_gid: Dict[str, int],
    out_dir: Path,
    tsx_name: str,
    z_offset: int,
    only_z: Optional[int] = None,
    flat: bool = False,
) -> Tuple[CellKey, int, int]:
    """
    Exports TMX for each Z layer found in lotpack for this cell.
    Returns (cell, z_count, file_count)
    """
    tile_names = parse_tile_names_from_lotheader(lotheader_path)
    offsets, lp_bytes = parse_lotpack_blocks(lotpack_path)

    # z_grids[z][y][x] = stack_gids
    z_grids: List[List[List[Optional[List[int]]]]] = []
    z_max_depth: List[int] = []

    def ensure_z(z: int) -> None:
        while len(z_grids) <= z:
            z_grids.append([[None] * 256 for _ in range(256)])
            z_max_depth.append(0)

    nblocks = len(offsets)
    for bi in range(nblocks):
        start = offsets[bi]
        end = offsets[bi + 1] if bi + 1 < nblocks else len(lp_bytes)
        if start >= end:
            continue

        payload = lp_bytes[start:end]
        layers = decode_block_layers(payload)

        bx = bi % 32
        by = bi // 32

        for z, squares in enumerate(layers):
            ensure_z(z)
            grid = z_grids[z]
            for si in range(64):
                sx = si % 8
                sy = si // 8
                gx = bx * 8 + sx
                gy = by * 8 + sy

                tile_idxs = squares[si]
                if not tile_idxs:
                    continue

                stack_gids: List[int] = []
                for tidx in tile_idxs:
                    if 0 <= tidx < len(tile_names):
                        tname = tile_names[tidx]
                        gid = name_to_gid.get(tname)
                        if gid is not None:
                            stack_gids.append(gid)

                if not stack_gids:
                    continue

                grid[gy][gx] = stack_gids
                if len(stack_gids) > z_max_depth[z]:
                    z_max_depth[z] = len(stack_gids)

    tsx_rel = tsx_name  # keep TSX in same output folder root
    written = 0
    z_count = 0

    for z, grid in enumerate(z_grids):
        depth = z_max_depth[z]
        if depth <= 0:
            continue

        z_label = z + z_offset
        if only_z is not None and z_label != only_z:
            continue

        layer_datas: List[List[int]] = []
        layer_names: List[str] = []
        for k in range(depth):
            flat_arr = [0] * (256 * 256)
            idx = 0
            for y in range(256):
                row = grid[y]
                for x in range(256):
                    stack = row[x]
                    if stack is not None and k < len(stack):
                        flat_arr[idx] = stack[k]
                    idx += 1
            layer_datas.append(flat_arr)
            layer_names.append(f"stratum_{k}")

        if flat:
            out_tmx = out_dir / f"{cell.x}_{cell.y}_z{z_label}.tmx"
        else:
            out_tmx = out_dir / f"{cell.x}_{cell.y}" / f"{cell.x}_{cell.y}_z{z_label}.tmx"

        write_tmx(
            out_tmx,
            tsx_rel=tsx_rel,
            map_width=256,
            map_height=256,
            layers=layer_datas,
            layer_names=layer_names,
        )
        written += 1
        z_count += 1

    return cell, z_count, written


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--map-dir", required=True, help="Folder containing .lotheader/.lotpack/.bin etc (e.g. ...\\media\\maps\\Muldraugh, KY)")
    ap.add_argument("--out-dir", required=True, help="Output folder for TMX/TSX")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) - 2), help="Parallel workers for export pass")
    ap.add_argument("--limit", type=int, default=0, help="Optional: export only first N cells (debug)")

    # New debug/structure options
    ap.add_argument("--z-offset", type=int, default=0, help="Shift exported Z labels by this amount (e.g. -2)")
    ap.add_argument("--one", default="", help="Export only one cell, format: X_Y (example: 0_18 or -5_12)")
    ap.add_argument("--one-z", type=int, default=None, help="Export only this Z (after z-offset applied) for the --one cell (example: 0)")
    ap.add_argument("--flat", action="store_true", help="Write TMX directly into out-dir (no subfolders)")
    args = ap.parse_args()

    map_dir = Path(args.map_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[1/2] Scanning lotheaders in: {map_dir}")
    t0 = time.time()
    name_to_gid, cell_to_lh = build_global_registry(map_dir)
    tsx_path = out_dir / "pz_global.tsx"
    write_global_tsx(tsx_path, name_to_gid)
    dt = time.time() - t0
    print(f"  -> Found {len(cell_to_lh)} cells and {len(name_to_gid)} unique tiles")
    print(f"  -> Wrote tileset: {tsx_path}")
    print(f"  -> Scan time: {dt:.1f}s")

    # Build cell list that has corresponding lotpack
    cells: List[Tuple[CellKey, Path, Path]] = []
    for cell, lh_path in cell_to_lh.items():
        lp = map_dir / f"world_{cell.x}_{cell.y}.lotpack"
        if lp.exists():
            cells.append((cell, lh_path, lp))

    cells.sort(key=lambda t: (t[0].x, t[0].y))

    # Filter to one cell if requested
    if args.one:
        m = re.match(r"^(-?\d+)_(-?\d+)$", args.one.strip())
        if not m:
            raise SystemExit("--one must be like X_Y (example: 0_18 or -5_12)")
        only_cell = (int(m.group(1)), int(m.group(2)))
        cells = [t for t in cells if (t[0].x, t[0].y) == only_cell]
        if not cells:
            raise SystemExit(f"No matching cell found for --one {args.one} (lotheader/lotpack missing?)")

    if args.limit and args.limit > 0:
        cells = cells[: args.limit]

    print(f"[2/2] Exporting TMX for {len(cells)} cells with {args.workers} workers...")
    t1 = time.time()
    tsx_name = "pz_global.tsx"

    done = 0
    total_written = 0
    total_z = 0

    # If exporting only one cell, force workers=1 to keep it simple/fast
    max_workers = 1 if args.one else max(1, args.workers)

    with ProcessPoolExecutor(max_workers=max_workers) as ex:
        futures = [
            ex.submit(
                export_one_cell,
                cell, lh, lp,
                name_to_gid,
                out_dir,
                tsx_name,
                args.z_offset,
                args.one_z,
                args.flat,
            )
            for (cell, lh, lp) in cells
        ]
        for f in as_completed(futures):
            cell, z_count, written = f.result()
            done += 1
            total_written += written
            total_z += z_count
            if done % 50 == 0 and not args.one:
                elapsed = time.time() - t1
                print(f"  ...{done}/{len(cells)} cells done ({total_written} TMX, {elapsed:.1f}s)")

    elapsed = time.time() - t1
    print("Done.")
    print(f"  Cells: {len(cells)}")
    print(f"  TMX files written: {total_written}")
    print(f"  Total Z-levels written (across cells): {total_z}")
    print(f"  Output: {out_dir}")
    print(f"  Time: {elapsed:.1f} seconds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())