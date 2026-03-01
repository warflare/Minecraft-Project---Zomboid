# -*- coding: utf-8 -*-
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

from lxml import etree
from tqdm import tqdm

# !!! FILL ME OUT !!!
TMX_DIR = Path(r"C:\Users\...")
OUT_CSV = Path(r"C:\Users\...\b42_tiles_used.csv")
OUT_JSON = Path(r"C:\Users\...\b42_tiles_used.json")
# !!! FILL ME OUT !!!

def parse_tmx_tilesets_and_layers(tmx_file: Path):
    tree = etree.parse(str(tmx_file))
    root = tree.getroot()

    width = int(root.attrib["width"])
    height = int(root.attrib["height"])

    # tilesets: list of (firstgid, name)
    tilesets = []
    for ts in root.findall("tileset"):
        firstgid = int(ts.attrib["firstgid"])
        name = ts.attrib.get("name")
        if name is None and "source" in ts.attrib:
            name = Path(ts.attrib["source"]).stem
        tilesets.append((firstgid, name))
    tilesets.sort(key=lambda x: x[0])

    layers = []
    for layer in root.findall("layer"):
        lname = layer.attrib.get("name", "layer")
        data = layer.find("data")
        if data is None:
            continue
        encoding = data.attrib.get("encoding")
        if encoding != "csv":
            # Skip non-csv layers
            continue
        csv_text = (data.text or "").strip()
        gids = [int(x) for x in csv_text.replace("\n", "").split(",") if x.strip() != ""]
        if len(gids) != width * height:
            # skip malformed
            continue
        layers.append((lname, gids))

    return tilesets, layers


def resolve_tileset(tilesets, gid: int):
    if gid == 0:
        return None, None
    chosen = None
    for firstgid, name in tilesets:
        if firstgid <= gid:
            chosen = (firstgid, name)
        else:
            break
    if chosen is None:
        return None, None
    firstgid, name = chosen
    local_id = gid - firstgid
    return name, local_id


def main():
    tmx_files = sorted(TMX_DIR.glob("*.tmx"))
    if not tmx_files:
        raise FileNotFoundError(f"No .tmx files found in {TMX_DIR}")

    freq = Counter()
    # For each tile key, count layer-name occurrences too
    layer_hits = defaultdict(Counter)

    for tmx in tqdm(tmx_files, desc="Scanning TMX files"):
        tilesets, layers = parse_tmx_tilesets_and_layers(tmx)
        for lname, gids in layers:
            for gid in gids:
                if gid == 0:
                    continue
                ts, local = resolve_tileset(tilesets, gid)
                if ts is None:
                    continue
                key = (ts, local)
                freq[key] += 1
                layer_hits[key][lname] += 1

    # Write CSV
    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["tileset", "local_id", "count", "top_layers"])
        for (ts, local), count in freq.most_common():
            # show top 5 layer names where this tile appears
            tops = layer_hits[(ts, local)].most_common(5)
            top_layers = "; ".join([f"{n}({c})" for n, c in tops])
            w.writerow([ts, local, count, top_layers])

    # Write JSON (easy to load later)
    out = []
    for (ts, local), count in freq.most_common():
        out.append({
            "tileset": ts,
            "local_id": local,
            "count": count,
            "top_layers": layer_hits[(ts, local)].most_common(10),
        })
    OUT_JSON.write_text(json.dumps(out, indent=2), encoding="utf-8")

    print(f"\nDone.\nWrote: {OUT_CSV.resolve()}\nWrote: {OUT_JSON.resolve()}")
    print(f"Unique tiles found: {len(freq)}")


if __name__ == "__main__":
    main()