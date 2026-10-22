#!/usr/bin/env python3
"""
Append regional-climate size classes (from buildRegionalSizeMap.py) to each
release ("rel") feature in an avaDirectoryResults parquet.

For every elevation threshold (default: 1000 / 1200 / 1400 m), uses the
polygonized zone layer AvaScenarioDev/grapGeosphereData/outputs/size_climate/
06_size_zones_elev{threshold}.gpkg (4 polygons, one per selectedSize class
1-4) rather than the raw raster - it's the same classification, just as a
handful of big polygons instead of a 190M-pixel grid, so no per-feature
raster masking is needed. The majority-overlap zone under each release
polygon is written to a new column 'selectedSize_elev{threshold}'. "res"
(result/runout) rows are left as <NA>, since the size map is defined on
release-area terrain.

Majority is by intersection area (not centroid) because some release areas
span >100 m of elevation and can straddle a threshold boundary.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
import geopandas as gpd


DEFAULT_INPUT = Path(
    r"D:\tmp\output\12_avaDirectory\BnCh2_subC500_100_5_sizeF500\avaDirectoryResults.parquet"
)
DEFAULT_ZONES_DIR = Path(
    r"C:\Users\ChristophHesselbach\Applications\AvaScenarioDev\grapGeosphereData"
    r"\outputs\size_climate"
)
DEFAULT_ZONES_PATTERN = "06_size_zones_elev{threshold}.gpkg"
DEFAULT_THRESHOLDS = [1000, 1200, 1400]
SIZE_COLUMN = "selectedSize"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)

    p.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    p.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output parquet path. Default: '<input>_withRegionalSize.parquet' "
        "beside the input. Pass --in-place to overwrite the input instead.",
    )
    p.add_argument(
        "--in-place",
        action="store_true",
        help="Overwrite --input (via safe temp-file + replace).",
    )

    p.add_argument(
        "--zones",
        action="append",
        default=None,
        metavar="THRESHOLD=PATH",
        help="Explicit 'threshold=path' mapping to a size_zones gpkg, may be "
        f"repeated. Default: {DEFAULT_THRESHOLDS} under {DEFAULT_ZONES_DIR}",
    )
    p.add_argument("--zones-dir", type=Path, default=DEFAULT_ZONES_DIR)
    p.add_argument("--zones-pattern", default=DEFAULT_ZONES_PATTERN)
    p.add_argument(
        "--thresholds", nargs="+", type=int, default=None,
        help="Used together with --zones-dir/--zones-pattern when --zones "
        f"is not given. Default: {DEFAULT_THRESHOLDS}",
    )

    p.add_argument("--modtype-column", default="modType")
    p.add_argument("--rel-value", default="rel")
    p.add_argument(
        "--column-template",
        default="selectedSize_elev{threshold}",
        help="Name template for the new columns.",
    )
    p.add_argument(
        "--overwrite-columns", action="store_true",
        help="Allow replacing columns that already exist in the input.",
    )
    return p.parse_args()


def resolve_zones(args: argparse.Namespace) -> dict[int, Path]:
    if args.zones:
        zones = {}
        for item in args.zones:
            threshold_str, _, path_str = item.partition("=")
            if not path_str:
                raise ValueError(f"--zones must be 'THRESHOLD=PATH', got: {item}")
            zones[int(threshold_str)] = Path(path_str)
        return zones

    thresholds = args.thresholds or DEFAULT_THRESHOLDS
    return {
        t: args.zones_dir / args.zones_pattern.format(threshold=t)
        for t in thresholds
    }


def majority_size_from_zones(rel: gpd.GeoDataFrame, zones_path: Path) -> pd.Series:
    """For each rel polygon, the selectedSize class it overlaps the most."""
    zones = gpd.read_file(zones_path)
    if SIZE_COLUMN not in zones.columns:
        raise ValueError(f"Column '{SIZE_COLUMN}' not found in {zones_path}")
    if rel.crs != zones.crs:
        zones = zones.to_crs(rel.crs)

    # Each class is one dissolved MultiPolygon whose bounding box spans almost
    # the whole project area, so a spatial index on these 4 rows can't filter
    # anything. Explode into individual parts (small, local bounding boxes)
    # so the index actually discriminates.
    zones = zones[[SIZE_COLUMN, "geometry"]].explode(index_parts=False).reset_index(drop=True)

    result = pd.Series(pd.array([pd.NA] * len(rel), dtype="Int64"), index=rel.index)

    # Cheap pass: a spatial-index "intersects" test (no geometry clipping) for
    # every rel polygon. Most release areas sit entirely inside one zone, so
    # this alone resolves almost everything.
    hits = gpd.sjoin(
        rel[["geometry"]], zones[[SIZE_COLUMN, "geometry"]], predicate="intersects", how="inner"
    )
    match_counts = hits.groupby(level=0).size()

    single = match_counts[match_counts == 1].index
    result.loc[single] = hits.loc[single, SIZE_COLUMN].to_numpy()

    # Expensive pass, only for the rare polygons that touch more than one
    # zone (straddling a class boundary): compute the actual overlap area
    # per zone and keep the majority.
    ambiguous = match_counts[match_counts > 1].index
    if len(ambiguous):
        rel_geom = rel.loc[ambiguous, ["geometry"]].copy()
        rel_geom["_row"] = rel_geom.index

        joined = gpd.overlay(
            rel_geom, zones[[SIZE_COLUMN, "geometry"]], how="intersection", keep_geom_type=False
        )
        joined["_area"] = joined.geometry.area
        best = joined.loc[joined.groupby("_row")["_area"].idxmax()]
        result.loc[best["_row"].to_numpy()] = best[SIZE_COLUMN].to_numpy()

    return result


def main() -> None:
    args = parse_args()
    zones = resolve_zones(args)

    for threshold, path in zones.items():
        if not path.is_file():
            raise FileNotFoundError(f"Zones file for threshold {threshold} not found: {path}")

    if args.in_place and args.output is not None:
        raise ValueError("Pass either --output or --in-place, not both.")

    output_path = args.output
    if output_path is None and not args.in_place:
        output_path = args.input.with_name(f"{args.input.stem}_withRegionalSize{args.input.suffix}")
    elif args.in_place:
        output_path = args.input

    new_columns = [
        args.column_template.format(threshold=t) for t in sorted(zones)
    ]

    print(f"Reading: {args.input}")
    gdf = gpd.read_parquet(args.input)

    if args.modtype_column not in gdf.columns:
        raise ValueError(f"Column not found: {args.modtype_column}")

    existing = [c for c in new_columns if c in gdf.columns]
    if existing and not args.overwrite_columns:
        raise ValueError(
            f"Columns already exist (pass --overwrite-columns to replace): {existing}"
        )

    rel_mask = gdf[args.modtype_column] == args.rel_value
    rel = gdf.loc[rel_mask]
    print(f"Total rows: {len(gdf)} | rel rows: {len(rel)}")

    for threshold in sorted(zones):
        path = zones[threshold]
        column = args.column_template.format(threshold=threshold)

        print(f"\nSampling {column} <- {path}")
        values = majority_size_from_zones(rel, path)

        gdf[column] = pd.array([pd.NA] * len(gdf), dtype="Int64")
        gdf.loc[rel_mask, column] = values

        n_assigned = int(values.notna().sum())
        n_missing = len(rel) - n_assigned
        print(f"  Assigned: {n_assigned} | No zone overlap (left <NA>): {n_missing}")

    print(f"\nWriting: {output_path}")
    if args.in_place:
        temp_path = output_path.with_name(f"{output_path.stem}.__tmp__{output_path.suffix}")
        gdf.to_parquet(temp_path)
        temp_path.replace(output_path)
    else:
        gdf.to_parquet(output_path)

    print("Done.")


if __name__ == "__main__":
    main()
