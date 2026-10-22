#!/usr/bin/env python3
"""Create lazy-loadable GeoJSON shards for the local Leaflet preview."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import geopandas as gpd


DEFAULT_INPUT = Path(
    r"D:\Cairos\ModelChainResults\Euregio\cairosAvaMaps\12_avaDirectory"
    r"\issw\avaDirectoryResults_issw.parquet"
)
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "docs" / "data"
KEEP_FIELDS = [
    "praID", "modType", "LKGebiet", "LWDGebietID", "praAreaM",
    "praElevMin", "praElevMean", "praElevMax", "resultID", "sector",
    "flow", "PPM", "PEM", "rSize",
]


def write_geojson(gdf: gpd.GeoDataFrame, path: Path, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"Output exists (pass --overwrite): {path}")
    temp = path.with_name(path.name + ".tmp.geojson")
    try:
        gdf.to_file(temp, driver="GeoJSON", index=False, coordinate_precision=6)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--simplify-metres", type=float, default=5.0)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    print(f"Reading master: {args.input}")
    data = gpd.read_parquet(args.input)
    if data.crs is None:
        raise ValueError("Master dataset has no CRS")
    data = data.rename(columns={"ppm": "PPM", "pem": "PEM"})
    missing = sorted(set(KEEP_FIELDS) - set(data.columns))
    if missing:
        raise ValueError(f"Required fields missing: {', '.join(missing)}")
    data = data[KEEP_FIELDS + [data.geometry.name]].copy()

    if args.simplify_metres > 0:
        if data.crs.is_geographic:
            raise ValueError("Simplification requires a projected source CRS")
        print(f"Simplifying {len(data):,} features by {args.simplify_metres:g} m")
        data.geometry = data.geometry.simplify(
            args.simplify_metres, preserve_topology=True
        )
        data = data[~data.geometry.is_empty]
    data = data.to_crs("EPSG:4326")

    manifest = {
        "source": str(args.input),
        "featureCount": len(data),
        "simplifyMetres": args.simplify_metres,
        "crs": "EPSG:4326",
        "shards": {},
    }
    grouped = data.groupby(["flow", "modType", "sector"], sort=True)
    for (flow, mod_type, sector), shard in grouped:
        name = f"avaDirectoryResults_issw_{flow}_{mod_type}_{sector}.geojson"
        path = args.output / name
        print(f"Writing {name}: {len(shard):,} features")
        write_geojson(shard, path, args.overwrite)
        manifest["shards"][f"{flow}/{mod_type}/{sector}"] = {
            "file": name,
            "features": len(shard),
            "bytes": path.stat().st_size,
        }

    manifest_path = args.output / "avaDirectoryResults_issw_shards.json"
    if manifest_path.exists() and not args.overwrite:
        raise FileExistsError(f"Output exists (pass --overwrite): {manifest_path}")
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    total_bytes = sum(item["bytes"] for item in manifest["shards"].values())
    print(f"Prepared {len(manifest['shards'])} shards, {total_bytes / 1_048_576:.2f} MB total")


if __name__ == "__main__":
    main()
