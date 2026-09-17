#!/usr/bin/env python3
"""Prepare deduplicated, lazy-loadable ISSW scenario shards for the preview."""

from __future__ import annotations

import argparse
import gzip
import json
import shutil
from functools import reduce
from operator import or_
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

DEFAULT_INPUT = Path(r"D:\Cairos\ModelChainResults\Euregio\cairosAvaMaps\13_avaScenMaps\pilotBrenner")
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "docs" / "data"
DEFAULT_LK_GEBIET_IDS = (
    100, 107, 130, 148, 162, 171, 190,
    227, 306, 309, 354, 369, 376,
)
KEEP_FIELDS = [
    "praID", "modType", "LKGebiet", "LWDGebietID", "praAreaM",
    "praElevMin", "praElevMean", "praElevMax", "resultID", "sector",
    "flow", "PPM", "PEM", "rSize",
]


def scenario_path(root: Path, size: int) -> Path:
    name = f"avaScen_ISSW-Preview-VH{size}"
    return root / name / f"{name}.parquet"


def read_scenario(
    root: Path, size: int, mod_type: str, lk_gebiet_ids: tuple[int, ...]
) -> gpd.GeoDataFrame:
    path = scenario_path(root, size)
    if not path.is_file():
        raise FileNotFoundError(path)
    print(f"Reading VH{size} {mod_type.upper()}: {path}")
    data = gpd.read_parquet(path).rename(columns={"ppm": "PPM", "pem": "PEM"})
    missing = sorted((set(KEEP_FIELDS) | {"LKGebietID"}) - set(data.columns))
    if missing:
        raise ValueError(f"{path.name} missing fields: {', '.join(missing)}")
    selected = (data["modType"] == mod_type) & data["LKGebietID"].isin(lk_gebiet_ids)
    data = data.loc[selected, KEEP_FIELDS + [data.geometry.name]].copy()
    if data.crs is None:
        raise ValueError(f"{path.name} has no CRS")
    return data


def prepare_geometry(
    data: gpd.GeoDataFrame, tolerance: float, smooth_metres: float
) -> gpd.GeoDataFrame:
    if tolerance > 0 or smooth_metres > 0:
        if data.crs.is_geographic:
            raise ValueError("Smoothing and simplification require a projected source CRS")
    if smooth_metres > 0:
        print(f"Smoothing {len(data):,} features with an {smooth_metres:g} m rounded buffer")
        data.geometry = data.geometry.buffer(
            smooth_metres, join_style="round"
        ).buffer(-smooth_metres, join_style="round")
    if tolerance > 0:
        data.geometry = data.geometry.simplify(tolerance, preserve_topology=True)
    data = data.loc[data.geometry.notna() & ~data.geometry.is_empty].copy()
    return data.to_crs("EPSG:4326")


def write_geojson_gz(data: gpd.GeoDataFrame, path: Path, overwrite: bool) -> Path:
    """Write GeoJSON gzip-compressed to <path>.gz; the plain GeoJSON never
    touches disk as a final artifact, so docs/data only ever holds the
    compressed shard actually served to the browser."""
    gz_path = path.with_name(path.name + ".gz")
    if gz_path.exists() and not overwrite:
        raise FileExistsError(f"Output exists (pass --overwrite): {gz_path}")
    temp = path.with_name(path.name + ".tmp.geojson")
    try:
        data.to_file(temp, driver="GeoJSON", index=False, coordinate_precision=5)
        temp_gz = gz_path.with_name(gz_path.name + ".tmp")
        with open(temp, "rb") as src, gzip.open(temp_gz, "wb", compresslevel=9) as dst:
            shutil.copyfileobj(src, dst)
        temp_gz.replace(gz_path)
    finally:
        temp.unlink(missing_ok=True)
    return gz_path


def assign_tiles(data: gpd.GeoDataFrame, tile_size_deg: float) -> gpd.GeoDataFrame:
    """Tag each feature with the (col, row) of a fixed, origin-(0,0) lon/lat
    grid cell it falls in, so tile IDs stay stable across regenerations.
    Uses representative_point (guaranteed inside the geometry, unlike
    centroid) purely to pick a cell — features aren't clipped to it, so the
    client pads its viewport query to cover features near a tile edge."""
    point = data.geometry.representative_point()
    data = data.copy()
    data["tileCol"] = np.floor(point.x / tile_size_deg).astype("int32")
    data["tileRow"] = np.floor(point.y / tile_size_deg).astype("int32")
    return data


def deduplicate_res(data: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    attrs = data[KEEP_FIELDS].copy()
    attrs["_geometry"] = data.geometry.to_wkb()
    data["_featureKey"] = pd.util.hash_pandas_object(attrs, index=False).to_numpy()
    masks = data.groupby("_featureKey", sort=False)["sizeMask"].agg(
        lambda values: reduce(or_, (int(value) for value in values), 0)
    )
    unique = data.drop_duplicates("_featureKey", keep="first").copy()
    unique["sizeMask"] = unique["_featureKey"].map(masks).astype("uint8")
    return unique.drop(columns="_featureKey")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--simplify-metres", type=float, default=5.0)
    parser.add_argument(
        "--smooth-metres", type=float, default=8.0,
        help="Per-feature rounded closing-buffer radius; 0 disables smoothing",
    )
    parser.add_argument(
        "--lk-gebiet-ids",
        default=",".join(map(str, DEFAULT_LK_GEBIET_IDS)),
        help="Comma-separated LKGebietID values included in the preview",
    )
    parser.add_argument(
        "--tile-size-deg", type=float, default=0.08,
        help="Spatial grid cell size (degrees) shards are additionally split by",
    )
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    lk_gebiet_ids = tuple(
        int(value.strip()) for value in args.lk_gebiet_ids.split(",") if value.strip()
    )
    if not lk_gebiet_ids:
        raise ValueError("At least one LKGebietID is required")
    print(f"Selected LKGebietID: {', '.join(map(str, lk_gebiet_ids))}")
    rel = prepare_geometry(
        read_scenario(args.input, 1, "rel", lk_gebiet_ids),
        args.simplify_metres,
        args.smooth_metres,
    )
    res_parts, source_counts = [], {}
    for size in range(2, 6):
        part = read_scenario(args.input, size, "res", lk_gebiet_ids)
        part["sizeMask"] = 1 << (size - 1)
        source_counts[f"VH{size}"] = len(part)
        res_parts.append(part)
    res = deduplicate_res(gpd.GeoDataFrame(
        pd.concat(res_parts, ignore_index=True),
        geometry="geometry",
        crs=res_parts[0].crs,
    ))
    print(f"Deduplicated RES: {sum(source_counts.values()):,} -> {len(res):,} features")
    res = prepare_geometry(res, args.simplify_metres, args.smooth_metres)

    rel = assign_tiles(rel, args.tile_size_deg)
    res = assign_tiles(res, args.tile_size_deg)

    manifest = {
        "source": str(args.input), "simplifyMetres": args.simplify_metres,
        "smoothMetres": args.smooth_metres,
        "crs": "EPSG:4326", "lkGebietIDs": list(lk_gebiet_ids), "relFeatures": len(rel),
        "resSourceFeatures": source_counts, "resUniqueFeatures": len(res),
        "sizeMask": {str(size): 1 << (size - 1) for size in range(1, 6)},
        "grid": {"originLat": 0, "originLon": 0, "cellSizeDeg": args.tile_size_deg},
        "shards": {},
    }
    for mod_type, dataset in (("rel", rel), ("res", res)):
        groups = dataset.groupby(["flow", "sector", "tileCol", "tileRow"], sort=True)
        for (flow, sector, tile_col, tile_row), shard in groups:
            name = f"avaPreview_issw_{mod_type}_{flow}_{sector}_{tile_col}_{tile_row}.geojson"
            path = args.output / name
            print(f"Writing {name}.gz: {len(shard):,} features")
            gz_path = write_geojson_gz(shard.drop(columns=["tileCol", "tileRow"]), path, args.overwrite)
            manifest["shards"][f"{mod_type}/{flow}/{sector}/{tile_col}_{tile_row}"] = {
                "file": gz_path.name, "features": len(shard), "bytes": gz_path.stat().st_size,
            }
    manifest_path = args.output / "avaPreview_issw_manifest.json"
    if manifest_path.exists() and not args.overwrite:
        raise FileExistsError(f"Output exists (pass --overwrite): {manifest_path}")
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    total = sum(item["bytes"] for item in manifest["shards"].values())
    print(f"Prepared {len(manifest['shards'])} shards, {total / 1_048_576:.2f} MB total")


if __name__ == "__main__":
    main()
