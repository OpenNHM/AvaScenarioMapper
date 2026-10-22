#!/usr/bin/env python3
"""Prepare the "Runout Dimension" vector shards from the per-scenario depSize
GeoPackages (one fixed AvaSizePotential run per preset — North2000 = Dry
Winter, South2500 = Wet Spring), matching the same scenarios the existing
Destructive/Runout size rasters were rendered from.

Unlike prepareWebPreviewScenarios.py's rel/res shards, this is not split by
flow/sector/size — it's a single fixed snapshot per preset, so the only
lazy-loading axis is the spatial tile grid (reused from that script so the
client's existing tile math applies unchanged).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import geopandas as gpd

from prepareWebPreviewScenarios import assign_tiles, prepare_geometry, write_geojson_gz

DEFAULT_SOURCES = {
    "dry": Path(__file__).resolve().parents[1] / "_archivDocs" / "avaScen_ISSW-North2000_depSize.gpkg",
    "wet": Path(__file__).resolve().parents[1] / "_archivDocs" / "avaScen_ISSW-South2500_depSize.gpkg",
}
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "docs" / "data"
DEFAULT_LK_GEBIET_IDS = (162, 148)
KEEP_FIELDS = [
    "praID", "LKGebiet", "LWDGebietID", "praElevMin", "praElevMean",
    "praElevMax", "resultID", "sector", "flow", "depSize",
]


def read_res(path: Path, lk_gebiet_ids: tuple[int, ...]) -> gpd.GeoDataFrame:
    if not path.is_file():
        raise FileNotFoundError(path)
    print(f"Reading: {path}")
    data = gpd.read_file(path)
    missing = sorted((set(KEEP_FIELDS) | {"LKGebietID"}) - set(data.columns))
    if missing:
        raise ValueError(f"{path.name} missing fields: {', '.join(missing)}")
    selected = (data["modType"] == "res") & data["LKGebietID"].isin(lk_gebiet_ids)
    data = data.loc[selected, KEEP_FIELDS + [data.geometry.name]].copy()
    if data.crs is None or data.crs.is_geographic:
        raise ValueError(f"{path.name} must have a projected CRS to compute area/geometry")
    return data


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--lk-gebiet-ids",
        default=",".join(map(str, DEFAULT_LK_GEBIET_IDS)),
        help="Comma-separated LKGebietID values included in the Dimension Size layer",
    )
    parser.add_argument("--simplify-metres", type=float, default=5.0)
    parser.add_argument("--smooth-metres", type=float, default=8.0)
    parser.add_argument(
        "--tile-size-deg", type=float, default=None,
        help="Defaults to whatever is already recorded in the existing manifest's grid",
    )
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    manifest_path = args.output / "avaPreview_issw_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    tile_size_deg = args.tile_size_deg or manifest.get("grid", {}).get("cellSizeDeg", 0.08)
    manifest.setdefault("grid", {"originLat": 0, "originLon": 0, "cellSizeDeg": tile_size_deg})
    manifest["depSizeShards"] = {}
    lk_gebiet_ids = tuple(
        int(value.strip()) for value in args.lk_gebiet_ids.split(",") if value.strip()
    )
    print(f"Selected LKGebietID: {', '.join(map(str, lk_gebiet_ids))}")

    for preset, source in DEFAULT_SOURCES.items():
        data = read_res(source, lk_gebiet_ids)
        print(f"{preset}: {len(data):,} res features, depSize range "
              f"{data['depSize'].min():.2f}..{data['depSize'].max():.2f}")
        data = prepare_geometry(data, args.simplify_metres, args.smooth_metres)
        data = assign_tiles(data, tile_size_deg)

        for (tile_col, tile_row), shard in data.groupby(["tileCol", "tileRow"], sort=True):
            name = f"avaPreview_issw_depsize_{preset}_{tile_col}_{tile_row}.geojson"
            path = args.output / name
            print(f"Writing {name}.gz: {len(shard):,} features")
            gz_path = write_geojson_gz(shard.drop(columns=["tileCol", "tileRow"]), path, args.overwrite)
            manifest["depSizeShards"][f"{preset}/{tile_col}_{tile_row}"] = {
                "file": gz_path.name, "features": len(shard), "bytes": gz_path.stat().st_size,
            }

    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    total = sum(item["bytes"] for item in manifest["depSizeShards"].values())
    print(f"Prepared {len(manifest['depSizeShards'])} depSize shards, {total / 1_048_576:.2f} MB total")


if __name__ == "__main__":
    main()
