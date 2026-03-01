# -*- coding: utf-8 -*-
"""
TMX -> Minecraft (Amulet) for Project Zomboid B42

Keeps:
- 7-col mapping only (tileset,tile_idx,category,value,placement,pz_name,gid)
- trees (oak/birch/spruce)
- walls N high (see WALL_HEIGHT)
- doors 2-block with NBT states
- protected blocks (doors)
- grass overlays y+1
- wall_inherit -> dominant exterior wall in same TMX (queues even if value=air)
- roof edge solver (stairs half=top)
- markings placed last (traffic lines)

Transform:
1) rotate around Y by +90 degrees
2) mirror X (east<->west)

Basements fix:
- Per-CELL "ground z" detection (weighted outdoor signals) so ground aligns to base_y
- Relative z (rel_z = pz_z - ground_z[cell]) so basements go below base_y
- Basement carve: for rel_z < 0, clear air above floor tiles up to next floor height

Roofs:
- Roof anchoring uses per-column structural TOP rel_z
- roof_y = base_y + struct_relz*FLOOR_HEIGHT + ROOF_OFFSET + max(0, rel_z-struct_relz)*ROOF_STEP
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from collections import Counter

import amulet
import amulet_nbt
from amulet.api.block import Block

TMX_NAME_RE = re.compile(r"^(-?\d+)_(-?\d+)_z(-?\d+)\.tmx$", re.IGNORECASE)

TREE_VALUES = {"oak_tree", "birch_tree", "spruce_tree"}

FLOOR_HEIGHT = 4          # structural floors spacing
ROOF_STEP = 1             # per-z roof slope step
ROOF_OFFSET = 4           # roof vertical offset above top structural floor (per column)

WALL_HEIGHT = 4           # wall column height

# ---- base terrain preset (INTENTIONALLY UNUSED / IGNORED) ----
MIN_Y = -64
BEDROCK = Block("minecraft", "bedrock", {})
DEEPSLATE = Block("minecraft", "deepslate", {})
STONE = Block("minecraft", "stone", {})
DIRT = Block("minecraft", "dirt", {})
GRASS = Block("minecraft", "grass_block", {})

@dataclass(frozen=True)
class MapRule:
    category: str
    value: str
    placement: str

# ------------------ small utils ------------------
def nb_s(v: str):
    return amulet_nbt.TAG_String(v)

def nb_b(v: int):
    return amulet_nbt.TAG_Byte(v)

def norm_id(s: str) -> Optional[str]:
    if s is None:
        return None
    t = str(s).strip()
    if not t or t.lower() == "ignore":
        return None
    if t.startswith("block.minecraft."):
        t = "minecraft:" + t[len("block.minecraft."):]
    if ":" not in t:
        t = "minecraft:" + t
    return t

def block_from(bid: Optional[str]) -> Block:
    bid = norm_id(bid) or "minecraft:air"
    ns, name = bid.split(":", 1)
    return Block(ns, name, {})

def is_door(bid: Optional[str]) -> bool:
    return bool(bid) and bid.startswith("minecraft:") and bid.endswith("_door")

def is_roof_rule(rule: Optional[MapRule]) -> bool:
    if not rule:
        return False
    cat = (rule.category or "").strip().lower()
    plc = (rule.placement or "").strip().lower()
    return (cat == "roof") or (plc == "roof")

def is_nonroof_placeable(rule: Optional[MapRule]) -> bool:
    """Used in the prepass to find top structural z per column."""
    if not rule:
        return False
    if is_roof_rule(rule):
        return False
    val = (rule.value or "").strip().lower()
    if val in TREE_VALUES:
        return True
    if val.startswith("schematic:"):
        return True
    bid = norm_id(rule.value)
    return bool(bid) and bid != "minecraft:air"

def roof_material_pair(bid: str) -> tuple[str, str]:
    """
    Return (stair_id, solid_id) for a roof material.
    Accepts either a stair id or a solid id.
    Handles common pluralization like *_tile_stairs -> *_tiles, *_brick_stairs -> *_bricks.
    """
    bid = norm_id(bid) or "minecraft:stone_brick_stairs"

    if bid.endswith("_stairs"):
        stair_id = bid
        if bid.endswith("_tile_stairs"):
            solid_id = bid.replace("_tile_stairs", "_tiles")
        elif bid.endswith("_brick_stairs"):
            solid_id = bid.replace("_brick_stairs", "_bricks")
        else:
            solid_id = bid.replace("_stairs", "")
        return stair_id, solid_id

    solid_id = bid
    if bid.endswith("_tiles"):
        stair_id = bid.replace("_tiles", "_tile_stairs")
    elif bid.endswith("_bricks"):
        stair_id = bid.replace("_bricks", "_brick_stairs")
    else:
        stair_id = bid + "_stairs"
    return stair_id, solid_id

def _dir_left_of(f: str) -> str:
    return {"north": "west", "west": "south", "south": "east", "east": "north"}[f]

def _dir_right_of(f: str) -> str:
    return {"north": "east", "east": "south", "south": "west", "west": "north"}[f]

def _step(lx: int, lz: int, d: str) -> tuple[int, int]:
    if d == "north":
        return (lx, lz - 1)
    if d == "south":
        return (lx, lz + 1)
    if d == "east":
        return (lx + 1, lz)
    return (lx - 1, lz)  # west

def roof_face_by_nearest_edge(lx: int, lz: int, roof_mask, inb) -> str:
    """Pick slope direction by looking for nearest edge distance N/S/E/W."""
    def has(x, z) -> bool:
        return inb(x, z) and bool(roof_mask[x][z])

    def dist(dx: int, dz: int) -> int:
        d = 0
        x, z = lx, lz
        while True:
            x += dx
            z += dz
            d += 1
            if not has(x, z):
                return d

    dn = dist(0, -1)
    ds = dist(0,  1)
    de = dist(1,  0)
    dw = dist(-1, 0)
    m = min(dn, ds, de, dw)
    if m == dn:
        return "north"
    if m == ds:
        return "south"
    if m == de:
        return "east"
    return "west"

def roof_edge_facing_shape(lx: int, lz: int, roof_mask, inb):
    """
    Robust eave solver:
    - missing directions are outward.
    - 1 missing => straight edge facing outward.
    - 2 adjacent missing => convex outer corner.
    Returns (facing_raw, shape_raw)
    """
    def has(x, z) -> bool:
        return inb(x, z) and bool(roof_mask[x][z])

    n = has(lx, lz - 1)
    s = has(lx, lz + 1)
    e = has(lx + 1, lz)
    w = has(lx - 1, lz)

    missing = []
    if not n: missing.append("north")
    if not s: missing.append("south")
    if not e: missing.append("east")
    if not w: missing.append("west")

    if len(missing) == 1:
        return missing[0], "straight"

    miss = set(missing)
    adjacent_pairs = [
        ("north", "east"),
        ("east", "south"),
        ("south", "west"),
        ("west", "north"),
    ]
    for a, b in adjacent_pairs:
        if a in miss and b in miss:
            facing = a
            other = b
            if _dir_left_of(facing) == other:
                return facing, "outer_left"
            if _dir_right_of(facing) == other:
                return facing, "outer_right"
            return facing, "straight"

    return (missing[0] if missing else "north"), "straight"

def roof_shape_from_mask(lx: int, lz: int, face_raw: str, roof_mask, inb) -> str:
    f = face_raw
    left = _dir_left_of(f)
    right = _dir_right_of(f)

    fx, fz = _step(lx, lz, f)
    lx1, lz1 = _step(lx, lz, left)
    rx1, rz1 = _step(lx, lz, right)

    flx, flz = _step(fx, fz, left)
    frx, frz = _step(fx, fz, right)

    def has(x, z) -> bool:
        return inb(x, z) and bool(roof_mask[x][z])

    f_has = has(fx, fz)
    l_has = has(lx1, lz1)
    r_has = has(rx1, rz1)
    fl_has = has(flx, flz)
    fr_has = has(frx, frz)

    if not f_has:
        if not l_has and r_has:
            return "outer_left"
        if not r_has and l_has:
            return "outer_right"
        return "straight"

    if l_has and not fl_has:
        return "inner_left"
    if r_has and not fr_has:
        return "inner_right"
    return "straight"

def y_off(placement: str, category: str) -> int:
    p = (placement or "").strip().lower()
    c = (category or "").strip().lower()
    if p in ("ground", "floor", "road") or c in ("ground", "floor", "road"):
        return 0
    if p in ("wall", "above_ground", "aboveground") or c in ("wall", "fence", "object", "wall_inherit"):
        return 1
    return 0

def is_marking(rule: Optional[MapRule], pz_name: Optional[str]) -> bool:
    if not rule:
        return False
    cat = (rule.category or "").strip().lower()
    plc = (rule.placement or "").strip().lower()
    name = (pz_name or "").strip().lower()
    if cat in ("marking", "road_marking", "paint", "decal", "lines", "line"):
        return True
    if plc in ("marking", "road_marking", "paint", "decal"):
        return True
    return any(k in name for k in ("traffic", "lane", "parking", "stripe", "stripes", "crosswalk", "arrow", "stop", "yield", "lines", "line"))

def load_rules(csv_path: Path) -> Dict[str, MapRule]:
    out: Dict[str, MapRule] = {}
    with csv_path.open("r", newline="", encoding="utf-8-sig") as f:
        for row in csv.reader(f):
            if not row:
                continue
            row = [c.strip() for c in row]
            if len(row) < 7:
                continue
            pz_name = row[5]
            if not pz_name:
                continue
            out[pz_name] = MapRule(category=row[2], value=row[3], placement=row[4])
    return out

def load_tsx(tsx: Path) -> Dict[int, str]:
    gid_to_name: Dict[int, str] = {}
    root = ET.parse(tsx).getroot()
    for tile in root.findall("tile"):
        tid = tile.get("id")
        if tid is None:
            continue
        props = tile.find("properties")
        if props is None:
            continue
        for prop in props.findall("property"):
            if prop.get("name") == "pz_name":
                gid_to_name[int(tid) + 1] = prop.get("value")
                break
    return gid_to_name

def parse_tmx(tmx: Path) -> Tuple[int, int, List[Tuple[str, List[int]]]]:
    root = ET.parse(tmx).getroot()
    w, h = int(root.get("width", "0")), int(root.get("height", "0"))
    if w <= 0 or h <= 0:
        raise ValueError(f"{tmx.name}: invalid width/height")
    layers: List[Tuple[str, List[int]]] = []
    for layer in root.findall("layer"):
        data = layer.find("data")
        if data is None or data.get("encoding") != "csv":
            continue
        parts = [p.strip() for p in (data.text or "").replace("\n", ",").split(",") if p.strip() != ""]
        gids = [int(p) for p in parts]
        if len(gids) != w * h:
            raise ValueError(f"{tmx.name}:{layer.get('name','layer')}: expected {w*h}, got {len(gids)}")
        layers.append((layer.get("name", "layer"), gids))
    return w, h, layers

def z_of(p: Path) -> int:
    m = TMX_NAME_RE.match(p.name)
    return int(m.group(3)) if m else 0

# ------------------ transform (rotate + mirror X) ------------------
def rot_y_plus_90(x: int, z: int, pivot_x: int, pivot_z: int) -> Tuple[int, int]:
    dx = x - pivot_x
    dz = z - pivot_z
    return pivot_x - dz, pivot_z + dx

def mirror_x(x: int, pivot_x: int) -> int:
    return (2 * pivot_x) - x

def xz_transform(raw_x: int, raw_z: int, pivot_x: int, pivot_z: int) -> Tuple[int, int]:
    x1, z1 = rot_y_plus_90(raw_x, raw_z, pivot_x, pivot_z)
    x2 = mirror_x(x1, pivot_x)
    return x2, z1

def rot_facing_plus_90(f: str) -> str:
    f = f if f in ("north", "south", "east", "west") else "north"
    return {"north": "east", "east": "south", "south": "west", "west": "north"}[f]

def mirror_facing_x(f: str) -> str:
    f = f if f in ("north", "south", "east", "west") else "north"
    return {"east": "west", "west": "east", "north": "north", "south": "south"}[f]

def facing_transform(f: str) -> str:
    return mirror_facing_x(rot_facing_plus_90(f))

def _swap_lr_shape_after_mirror(shape: str) -> str:
    return {
        "inner_left": "inner_right",
        "inner_right": "inner_left",
        "outer_left": "outer_right",
        "outer_right": "outer_left",
    }.get(shape, shape)

# ------------------ basement: choose ground_z PER CELL (weighted outdoor) ------------------
def ground_weight(rule: Optional[MapRule], pz_name: Optional[str]) -> int:
    """
    Weight evidence that a tile is the outdoor ground surface.
    Returns 0 if it should NOT vote for ground_z.
    """
    n = (pz_name or "").lower()

    # Strongest: outdoor blends
    if n.startswith("blends_natural_"):
        return 10
    if n.startswith("blends_street_"):
        return 10

    # Exterior street/floor surfaces
    if n.startswith("floors_exterior_street_"):
        return 8
    if n.startswith("floors_exterior_"):
        return 6

    # Grass overlays: weak (can exist in weird places)
    if n.startswith("blends_grassoverlays_"):
        return 2

    # Never let interior floors vote
    if n.startswith("floors_interior_"):
        return 0

    # Safer fallback: explicit ROAD only (not generic floor/ground)
    if rule:
        cat = (rule.category or "").strip().lower()
        plc = (rule.placement or "").strip().lower()
        if cat == "road" or plc == "road":
            return 4

    return 0

def carve_air_column(level, dim, x: int, z: int, y0: int, y1: int, plat, ver,
                     protected: set, wall_written: set):
    """Set blocks to air from y0..y1 inclusive, but don't destroy protected blocks or walls."""
    air = block_from("minecraft:air")
    for yy in range(y0, y1 + 1):
        pos = (x, yy, z)
        if pos in protected or pos in wall_written:
            continue
        level.set_version_block(x, yy, z, dim, (plat, ver), air)

# ------------------ special placements ------------------
def place_door(level, dim, x, y, z, door_id, plat, ver, facing="north"):
    door_id = norm_id(door_id) or "minecraft:oak_door"
    ns, name = door_id.split(":", 1)
    facing = facing if facing in ("north", "south", "east", "west") else "north"

    if ns != "minecraft":
        lower = Block(ns, name, {})
        upper = Block(ns, name, {})
    else:
        common = {"facing": nb_s(facing), "hinge": nb_s("left"), "open": nb_b(0), "powered": nb_b(0)}
        lower = Block("minecraft", name, {**common, "half": nb_s("lower")})
        upper = Block("minecraft", name, {**common, "half": nb_s("upper")})

    level.set_version_block(x, y, z, dim, (plat, ver), lower)
    level.set_version_block(x, y + 1, z, dim, (plat, ver), upper)

def place_stair(level, dim, x, y, z, stair_id, plat, ver, facing, half="top", shape="straight"):
    stair_id = norm_id(stair_id) or "minecraft:stone_brick_stairs"
    ns, name = stair_id.split(":", 1)

    if facing not in ("north", "south", "east", "west"):
        facing = "north"
    if half not in ("top", "bottom"):
        half = "top"
    if shape not in ("straight", "inner_left", "inner_right", "outer_left", "outer_right"):
        shape = "straight"

    props = {"facing": nb_s(facing), "half": nb_s(half), "shape": nb_s(shape), "waterlogged": nb_b(0)}
    level.set_version_block(x, y, z, dim, (plat, ver), Block(ns, name, props))

def spawn_tree(level, dim, x, z, y_floor, plat, ver, ttype):
    from amulet_nbt import TAG_String

    if ttype == "birch_tree":
        trunk, leaves = Block("minecraft", "birch_log", {}), Block("minecraft", "birch_leaves", {})
    elif ttype == "spruce_tree":
        trunk, leaves = Block("minecraft", "spruce_log", {}), Block("minecraft", "spruce_leaves", {})
    else:
        trunk, leaves = Block("minecraft", "oak_log", {}), Block("minecraft", "oak_leaves", {})

    for dy in range(1, 5):
        level.set_version_block(x, y_floor + dy, z, dim, (plat, ver), trunk)

    persistent_leaves = Block(leaves.namespace, leaves.base_name, {"persistent": TAG_String("true")})
    for y_off2, r in ((5, 2), (6, 1), (7, 0)):
        yy = y_floor + y_off2
        for dx in range(-r, r + 1):
            for dz in range(-r, r + 1):
                level.set_version_block(x + dx, yy, z + dz, dim, (plat, ver), persistent_leaves)

def place_schematic(level, dim, x, y, z, path: Path, plat, ver):
    if not path.exists():
        print(f"[WARN] Schematic not found: {path}", file=sys.stderr)
        return

    nbt = amulet_nbt.load(str(path))  # NamedTag
    comp = nbt.compound

    if not all(k in comp for k in ("size", "palette", "blocks")):
        print(f"[WARN] Invalid structure NBT (missing size/palette/blocks): {path}", file=sys.stderr)
        return

    size_list = comp["size"].py_list
    if len(size_list) < 3:
        print(f"[WARN] Invalid size tag in: {path}", file=sys.stderr)
        return

    palette_list = comp["palette"].py_list
    blocks_list = comp["blocks"].py_list

    from amulet.api.block_entity import BlockEntity

    for entry in blocks_list:
        e = entry
        state_idx = e["state"].py_int
        pos_list = e["pos"].py_list
        if len(pos_list) < 3:
            continue

        px = pos_list[0].py_int if hasattr(pos_list[0], "py_int") else int(pos_list[0])
        py = pos_list[1].py_int if hasattr(pos_list[1], "py_int") else int(pos_list[1])
        pz = pos_list[2].py_int if hasattr(pos_list[2], "py_int") else int(pos_list[2])

        wx, wy, wz = x + px, y + py, z + pz

        if state_idx < 0 or state_idx >= len(palette_list):
            print(f"[WARN] Invalid palette index {state_idx} at {wx},{wy},{wz} in {path.name}", file=sys.stderr)
            continue

        pal = palette_list[state_idx]
        name = pal["Name"].py_str
        ns, base_name = name.split(":", 1) if ":" in name else ("minecraft", name)

        props = {}
        props_tag = pal.get("Properties")
        if props_tag is not None:
            props = {k: v for k, v in props_tag.items()}

        block = Block(ns, base_name, props)
        level.set_version_block(wx, wy, wz, dim, (plat, ver), block)

        # --- Block Entity (optional) ---
        nbt_tag = e.get("nbt")
        if nbt_tag is None:
            continue

        be_comp = nbt_tag.compound if hasattr(nbt_tag, "compound") else nbt_tag
        be_comp["x"] = amulet_nbt.TAG_Int(wx)
        be_comp["y"] = amulet_nbt.TAG_Int(wy)
        be_comp["z"] = amulet_nbt.TAG_Int(wz)

        be_id_tag = be_comp.get("id")
        be_id = be_id_tag.py_str if be_id_tag is not None else "minecraft:unknown"
        be_ns, be_base = be_id.split(":", 1) if ":" in be_id else ("minecraft", be_id)

        be_named = nbt_tag if hasattr(nbt_tag, "compound") else amulet_nbt.NamedTag(nbt_tag)
        block_entity = BlockEntity(namespace=be_ns, base_name=be_base, x=wx, y=wy, z=wz, nbt=be_named)

        cx, cz = wx >> 4, wz >> 4
        chunk = level.get_chunk(cx, cz, dim)
        if chunk is None:
            print(f"[WARN] No chunk at {cx},{cz} for BE at {wx},{wy},{wz}", file=sys.stderr)
            continue

        local_pos = (wx & 15, wy, wz & 15)
        chunk.block_entities[local_pos] = block_entity
        level.put_chunk(chunk, dim)

# ------------------ main ------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tmx-dir", default=str(Path(__file__).resolve().parent))
    ap.add_argument("--all", action="store_true", help="Build all TMX files in tmx-dir")
    ap.add_argument("--tmx", help="Specific TMX file(s), comma separated")
    ap.add_argument("--cell", help="Build all Z levels for a cell, e.g. 25_21")
    ap.add_argument("--z", type=int, help="Build all TMX files for a specific Z level")
    ap.add_argument("--world-dir", required=True)
    ap.add_argument("--mapping-csv", required=True)
    ap.add_argument("--base-y", type=int, default=70)  # you asked for y=70 baseline
    ap.add_argument("--offset-x", type=int, default=0)
    ap.add_argument("--offset-z", type=int, default=0)
    ap.add_argument("--unknown-block", default="minecraft:air")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    tmx_dir = Path(args.tmx_dir)
    schem_dir = tmx_dir / "schematics"

    all_tmx = sorted([p for p in tmx_dir.glob("*.tmx") if TMX_NAME_RE.match(p.name)])
    if not all_tmx:
        print("[ERROR] No TMX files found in directory", file=sys.stderr)
        return 2

    sel: List[Path] = []
    if args.all or (not args.tmx and not args.cell and args.z is None):
        sel = all_tmx
    elif args.tmx:
        for name in [n.strip() for n in args.tmx.split(",") if n.strip()]:
            p = tmx_dir / name
            if not p.exists():
                print(f"[ERROR] TMX not found: {p}", file=sys.stderr)
                return 2
            sel.append(p)
    elif args.cell:
        pref = f"{args.cell}_z"
        sel = sorted([p for p in all_tmx if p.name.startswith(pref)], key=z_of)
        if not sel:
            print(f"[ERROR] No TMX files for cell {args.cell}", file=sys.stderr)
            return 2
    else:
        suf = f"_z{args.z}.tmx"
        sel = [p for p in all_tmx if p.name.endswith(suf)]
        if not sel:
            print(f"[ERROR] No TMX files for z={args.z}", file=sys.stderr)
            return 2

    tsx = tmx_dir / "pz_global.tsx"
    if not tsx.exists():
        print(f"[ERROR] Missing TSX: {tsx}", file=sys.stderr)
        return 2

    print("[1/4] Loading TSX + mapping...")
    gid_to_pz = load_tsx(tsx)
    rules = load_rules(Path(args.mapping_csv))

    unk_id = norm_id(args.unknown_block)
    skip_unknown = (unk_id is None) or (unk_id == "minecraft:air")
    unk_block = None if skip_unknown else block_from(unk_id)

    # ------------------------------------------------------------
    # PREPASS A: determine "ground z" PER CELL (weighted outdoor signals)
    # ------------------------------------------------------------
    print("[2/4] Prepass A: computing per-cell ground z...")
    ground_votes_by_cell: Dict[Tuple[int, int], Counter] = {}

    for tmx in sel:
        mm = TMX_NAME_RE.match(tmx.name)
        if not mm:
            continue
        cell_x, cell_y, pz_z = int(mm.group(1)), int(mm.group(2)), int(mm.group(3))
        w, h, layers = parse_tmx(tmx)

        ckey = (cell_x, cell_y)
        if ckey not in ground_votes_by_cell:
            ground_votes_by_cell[ckey] = Counter()

        for _, gids in layers:
            for gid in gids:
                if gid == 0:
                    continue
                pz_name = gid_to_pz.get(gid)
                rule = rules.get(pz_name) if pz_name else None
                wgt = ground_weight(rule, pz_name)
                if wgt > 0:
                    ground_votes_by_cell[ckey][pz_z] += wgt

    ground_z_by_cell: Dict[Tuple[int, int], int] = {}
    for ckey, ctr in ground_votes_by_cell.items():
        ground_z_by_cell[ckey] = ctr.most_common(1)[0][0] if ctr else 0

    # ------------------------------------------------------------
    # PREPASS B: determine top structural rel_z PER COLUMN (mc_x, mc_z)
    # ------------------------------------------------------------
    print("[2/4] Prepass B: computing per-column structural top (relative) z...")
    max_struct_relz_by_col: Dict[Tuple[int, int], int] = {}

    for tmx in sel:
        mm = TMX_NAME_RE.match(tmx.name)
        if not mm:
            continue
        cell_x, cell_y, pz_z = int(mm.group(1)), int(mm.group(2)), int(mm.group(3))
        w, h, layers = parse_tmx(tmx)

        ground_z = ground_z_by_cell.get((cell_x, cell_y), 0)
        rel_z = pz_z - ground_z

        cell_ox = args.offset_x + cell_x * 256
        cell_oz = args.offset_z + cell_y * 256
        pivot_x, pivot_z = cell_ox + 128, cell_oz + 128

        for _, gids in layers:
            for idx, gid in enumerate(gids):
                if gid == 0:
                    continue
                pz_name = gid_to_pz.get(gid)
                rule = rules.get(pz_name) if pz_name else None
                if not is_nonroof_placeable(rule):
                    continue

                lx, lz = idx % w, idx // w
                raw_x = cell_ox + lx
                raw_z = cell_oz + lz
                mc_x, mc_z = xz_transform(raw_x, raw_z, pivot_x, pivot_z)

                key = (mc_x, mc_z)
                prev = max_struct_relz_by_col.get(key)
                if prev is None or rel_z > prev:
                    max_struct_relz_by_col[key] = rel_z

    if not max_struct_relz_by_col:
        print(" [WARN] No structural tiles detected in prepass; roofs will anchor to rel_z=0.")

    # ------------------------------------------------------------
    # BUILD
    # ------------------------------------------------------------
    print("[3/4] Opening Minecraft world...")
    level = amulet.load_level(str(Path(args.world_dir)))
    dim = "minecraft:overworld"
    plat, ver = "java", (1, 20, 4)

    protected: set[Tuple[int, int, int]] = set()
    wall_written: set[Tuple[int, int, int]] = set()
    spawned_trees: set[Tuple[int, int, int]] = set()

    placed = 0
    unmapped = 0

    print("[4/4] Building selected TMX files...")
    try:
        for tmx in sel:
            print(f" -> {tmx.name}")
            mm = TMX_NAME_RE.match(tmx.name)
            if not mm:
                continue
            cell_x, cell_y, pz_z = int(mm.group(1)), int(mm.group(2)), int(mm.group(3))
            w, h, layers = parse_tmx(tmx)

            # REBASE: make this cell's ground align to base_y
            ground_z = ground_z_by_cell.get((cell_x, cell_y), 0)
            rel_z = pz_z - ground_z
            base_floor_y = args.base_y + (rel_z * FLOOR_HEIGHT)

            cell_ox = args.offset_x + cell_x * 256
            cell_oz = args.offset_z + cell_y * 256
            pivot_x, pivot_z = cell_ox + 128, cell_oz + 128

            # roof mask prepass for THIS TMX
            roof = [[False] * h for _ in range(w)]
            for _, gids in layers:
                for idx, gid in enumerate(gids):
                    if gid == 0:
                        continue
                    name = gid_to_pz.get(gid)
                    if not name:
                        continue
                    r = rules.get(name)
                    if not r:
                        continue
                    if is_roof_rule(r):
                        lx, lz = idx % w, idx // w
                        roof[lx][lz] = True

            def inb(x, z):
                return 0 <= x < w and 0 <= z < h

            def roof_edge(x, z):
                if not roof[x][z]:
                    return False
                for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nx, nz = x + dx, z + dz
                    if not inb(nx, nz) or not roof[nx][nz]:
                        return True
                return False

            def roof_face_simple(x, z):
                if not inb(x, z - 1) or not roof[x][z - 1]:
                    return "north"
                if not inb(x, z + 1) or not roof[x][z + 1]:
                    return "south"
                if not inb(x + 1, z) or not roof[x + 1][z]:
                    return "east"
                return "west"

            wall_votes = Counter()
            inherit_jobs: List[Tuple[int, int, int]] = []
            markings: List[Tuple[int, int, int, Block]] = []
            ground_written: set[Tuple[int, int, int]] = set()

            for _, gids in layers:
                for idx, gid in enumerate(gids):
                    if gid == 0:
                        continue

                    pz_name = gid_to_pz.get(gid)
                    rule = rules.get(pz_name) if pz_name else None

                    lx, lz = idx % w, idx // w
                    raw_x = cell_ox + lx
                    raw_z = cell_oz + lz
                    mc_x, mc_z = xz_transform(raw_x, raw_z, pivot_x, pivot_z)

                    # unmapped / unknown
                    if not pz_name or rule is None:
                        if skip_unknown:
                            continue
                        unmapped += 1
                        block = unk_block
                        cat = plc = ""
                        off = 0
                        is_roof = False
                        val = ""
                    else:
                        val = (rule.value or "").strip().lower()
                        cat = (rule.category or "").strip().lower()
                        plc = (rule.placement or "").strip().lower()
                        is_roof = is_roof_rule(rule)
                        off = y_off(rule.placement, rule.category)

                    # ✅ wall_inherit queued BEFORE skipping air/empty values
                    if (not is_roof) and (cat == "wall_inherit"):
                        mc_y = base_floor_y + off
                        inherit_jobs.append((mc_x, mc_y, mc_z))
                        placed += 1
                        continue

                    # trees (non-roof)
                    if (not is_roof) and (val in TREE_VALUES):
                        key = (mc_x, mc_z, base_floor_y)
                        if key not in spawned_trees and not args.dry_run:
                            spawned_trees.add(key)
                            spawn_tree(level, dim, mc_x, mc_z, base_floor_y, plat, ver, val)
                        placed += 1
                        continue

                    # schematics (non-roof)
                    if (not is_roof) and val.startswith("schematic:"):
                        schematic_name = val[len("schematic:"):].strip()
                        schematic_path = schem_dir / (schematic_name + ".nbt")
                        mc_y = base_floor_y + off
                        pos = (mc_x, mc_y, mc_z)
                        if pos in protected:
                            continue
                        if args.dry_run:
                            placed += 1
                            continue
                        place_schematic(level, dim, mc_x, mc_y, mc_z, schematic_path, plat, ver)
                        placed += 1
                        continue

                    # resolve mapped block id
                    bid = norm_id(rule.value) if rule else None
                    if not bid:
                        continue
                    if bid == "minecraft:air":
                        continue
                    block = block_from(bid)

                    # grass overlays sit above
                    if pz_name and pz_name.startswith("blends_grassoverlays"):
                        off = max(off, 1)

                    # markings last
                    if rule and is_marking(rule, pz_name):
                        mc_y = base_floor_y + off
                        pos = (mc_x, mc_y, mc_z)
                        if pos not in protected:
                            markings.append((mc_x, mc_y, mc_z, block))
                        placed += 1
                        continue

                    # roof tiles (slope-aware, anchored per column)
                    if is_roof:
                        struct_relz = max_struct_relz_by_col.get((mc_x, mc_z), 0)
                        roof_extra = max(0, rel_z - struct_relz)
                        roof_y = args.base_y + (struct_relz * FLOOR_HEIGHT) + ROOF_OFFSET + (roof_extra * ROOF_STEP)

                        pos = (mc_x, roof_y, mc_z)
                        if pos in protected:
                            continue

                        stair_id, solid_id = roof_material_pair(rule.value)

                        if roof_edge(lx, lz):
                            face_raw, shape_raw = roof_edge_facing_shape(lx, lz, roof, inb)
                        else:
                            face_raw = roof_face_by_nearest_edge(lx, lz, roof, inb)
                            shape_raw = roof_shape_from_mask(lx, lz, face_raw, roof, inb)

                        face = facing_transform(face_raw)
                        # invert to push slope outward
                        face = {"north": "south", "south": "north", "east": "west", "west": "east"}.get(face, face)
                        shape = _swap_lr_shape_after_mirror(shape_raw)

                        if args.dry_run:
                            placed += 1
                            continue

                        if roof_edge(lx, lz):
                            place_stair(level, dim, mc_x, roof_y, mc_z, stair_id, plat, ver, face, half="bottom", shape=shape)
                        else:
                            level.set_version_block(mc_x, roof_y, mc_z, dim, (plat, ver), block_from(solid_id))

                        placed += 1
                        continue

                    # non-roof placement Y
                    mc_y = base_floor_y + off

                    # Basement carve: if below ground, clear room air above floor tiles
                    if (rel_z < 0) and rule and (not args.dry_run):
                        cat_l = (rule.category or "").strip().lower()
                        plc_l = (rule.placement or "").strip().lower()
                        is_floorish = (cat_l in ("ground", "floor", "road")) or (plc_l in ("ground", "floor", "road"))
                        # optional heuristic help
                        if (not is_floorish) and pz_name:
                            n = pz_name.lower()
                            if n.startswith("floors_interior_") or n.startswith("floors_exterior_"):
                                is_floorish = True
                        if is_floorish:
                            carve_air_column(
                                level, dim, mc_x, mc_z,
                                y0=mc_y + 1,
                                y1=mc_y + (FLOOR_HEIGHT - 1),
                                plat=plat, ver=ver,
                                protected=protected,
                                wall_written=wall_written
                            )

                    # ground first-wins
                    is_ground = (plc in ("ground", "floor", "road")) or (cat in ("ground", "floor", "road"))
                    if is_ground:
                        keyg = (mc_x, mc_y, mc_z)
                        if keyg in ground_written:
                            continue
                        ground_written.add(keyg)

                    if args.dry_run:
                        placed += 1
                        continue

                    # doors
                    door_id = norm_id(rule.value) if rule else None
                    if is_door(door_id):
                        door_y = max(mc_y, base_floor_y + 1)
                        if (mc_x, door_y, mc_z) in protected or (mc_x, door_y + 1, mc_z) in protected:
                            continue
                        door_face = facing_transform("north")
                        place_door(level, dim, mc_x, door_y, mc_z, door_id, plat, ver, facing=door_face)
                        protected.add((mc_x, door_y, mc_z))
                        protected.add((mc_x, door_y + 1, mc_z))
                        placed += 1
                        continue

                    # walls (WALL_HEIGHT high)
                    is_wall = (cat == "wall") or (plc == "wall")
                    if is_wall and rule:
                        bid2 = norm_id(rule.value)
                        if bid2 and bid2 != "minecraft:air" and not is_door(bid2):
                            wall_votes[bid2] += 1

                        # all-or-nothing: if ANY layer hits protected, skip whole wall column
                        if any((mc_x, mc_y + dy, mc_z) in protected for dy in range(WALL_HEIGHT)):
                            placed += 1
                            continue

                        for dy in range(WALL_HEIGHT):
                            yy = mc_y + dy
                            pos = (mc_x, yy, mc_z)
                            level.set_version_block(mc_x, yy, mc_z, dim, (plat, ver), block)
                            wall_written.add(pos)

                        placed += 1
                        continue

                    # default single (don't punch holes in walls/doors)
                    pos = (mc_x, mc_y, mc_z)
                    if pos in protected:
                        continue
                    if pos in wall_written:
                        continue
                    level.set_version_block(mc_x, mc_y, mc_z, dim, (plat, ver), block)
                    placed += 1

            # apply inherit (fill missing wall spots only)
            if inherit_jobs:
                dom = wall_votes.most_common(1)[0][0] if wall_votes else "minecraft:stone_bricks"
                dom_block = block_from(dom)
                for x, y, z in inherit_jobs:
                    if any((x, y + dy, z) in protected for dy in range(WALL_HEIGHT)):
                        continue
                    for dy in range(WALL_HEIGHT):
                        yy = y + dy
                        pos = (x, yy, z)
                        if pos in wall_written:
                            continue
                        level.set_version_block(x, yy, z, dim, (plat, ver), dom_block)
                        wall_written.add(pos)

            # apply markings last (don't overwrite walls/doors)
            for x, y, z, b in markings:
                pos = (x, y, z)
                if pos in protected:
                    continue
                if pos in wall_written:
                    continue
                level.set_version_block(x, y, z, dim, (plat, ver), b)

        if args.dry_run:
            print("Dry-run complete (no blocks written).")
        else:
            print("Saving world...")
            level.save()

    finally:
        level.close()

    print(f"Done. placed={placed:,} unmapped={unmapped:,} (unknown={args.unknown_block})")
    print(f"Trees spawned: {len(spawned_trees):,}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())