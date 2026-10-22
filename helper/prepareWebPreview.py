#!/usr/bin/env python3
"""Prepare AvaScenarioMapper vector and raster data for a static web preview.

The default GeoJSON output works with the current Leaflet implementation.
Passing --pmtiles additionally creates a vector-tile archive with tippecanoe;
that archive is the recommended production format for large scenario layers.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.warp import calculate_default_transform, reproject


DEFAULT_SCENARIO = Path(
    r"D:\Cairos\ModelChainResults\Euregio\cairosAvaMaps\12_avaDirectory"
    r"\issw\avaDirectoryResults_issw.parquet"
)
DEFAULT_EXTENT = Path(
    r"D:\Cairos\ModelChainResults\_gis\issw\pilotBrennerExtentMerge.gpkg"
)
DEFAULT_HILLSHADE = Path(
    r"D:\Cairos\ModelChainResults\_gis\issw\10HS_isswAll.tif"
)
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "docs" / "data"
DEFAULT_VECTOR_NAME = "avaDirectoryResults_issw_mobile.geojson"

# Everything else is unnecessary for filtering or the current popup. Geometry
# is always retained by GeoPandas and therefore is not listed here.
KEEP_FIELDS = [
    "praID",
    "modType",
    "LKGebiet",
    "LWDGebietID",
    "praAreaM",
    "praElevMin",
    "praElevMean",
    "praElevMax",
    "resultID",
    "sector",
    "flow",
    "PPM",
    "PEM",
    "rSize",
    "AvaDistributionPotential",
    "AvaSizePotential",
    "scenarioName",
]


def output_path(directory: Path, name: str, overwrite: bool) -> Path:
    path = directory / name
    if path.exists() and not overwrite:
        raise FileExistsError(f"Output exists (pass --overwrite): {path}")
    return path


def size_mb(path: Path) -> float:
    return path.stat().st_size / 1_048_576


def write_geojson(gdf: gpd.GeoDataFrame, path: Path) -> None:
    temp = path.with_name(path.name + ".tmp.geojson")
    try:
        gdf.to_file(
            temp,
            driver="GeoJSON",
            index=False,
            coordinate_precision=6,
        )
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def prepare_vectors(args: argparse.Namespace) -> tuple[Path, Path, dict]:
    print(f"Reading extent: {args.extent}")
    extent = gpd.read_file(args.extent)
    if extent.crs is None:
        raise ValueError("Extent has no CRS")
    extent = extent[[extent.geometry.name]].dissolve().to_crs("EPSG:4326")

    print(f"Reading scenarios: {args.scenario}")
    if args.scenario.suffix.lower() in {".parquet", ".geoparquet"}:
        scenarios = gpd.read_parquet(args.scenario)
    else:
        scenarios = gpd.read_file(args.scenario)
    if scenarios.crs is None:
        raise ValueError("Scenario layer has no CRS")

    # AvaDirectoryResults uses lowercase ppm/pem, while scenario exports and
    # the web client conventionally use uppercase PPM/PEM.
    scenarios = scenarios.rename(
        columns={name: name.upper() for name in ("ppm", "pem") if name in scenarios}
    )

    present = [name for name in KEEP_FIELDS if name in scenarios.columns]
    missing = sorted(set(KEEP_FIELDS) - set(present))
    scenarios = scenarios[present + [scenarios.geometry.name]].copy()
    original_count = len(scenarios)

    if args.clip_scenarios_to_extent:
        clip_geometry = extent.to_crs(scenarios.crs).geometry.iloc[0]
        scenarios = scenarios[scenarios.geometry.intersects(clip_geometry)].copy()
        scenarios.geometry = scenarios.geometry.intersection(clip_geometry)
        scenarios = scenarios[~scenarios.geometry.is_empty]

    # Simplify while coordinates are still in the source metric CRS. Preserve
    # topology avoids many invalid rings at a modest processing cost.
    if args.simplify_metres > 0:
        if scenarios.crs.is_geographic:
            raise ValueError("Simplification requires a projected source CRS")
        print(f"Simplifying geometry by {args.simplify_metres:g} m")
        scenarios.geometry = scenarios.geometry.simplify(
            args.simplify_metres, preserve_topology=True
        )
        scenarios = scenarios[~scenarios.geometry.is_empty]

    scenarios = scenarios.to_crs("EPSG:4326")
    scenario_path = output_path(
        args.output, args.vector_name, args.overwrite
    )
    extent_path = output_path(
        args.output, "pilotBrennerExtentMerge.geojson", args.overwrite
    )
    print(f"Writing {len(scenarios):,} features: {scenario_path}")
    write_geojson(scenarios, scenario_path)
    write_geojson(extent, extent_path)

    summary = {
        "sourceFeatureCount": original_count,
        "previewFeatureCount": len(scenarios),
        "crs": "EPSG:4326",
        "simplifyMetres": args.simplify_metres,
        "clippedToExtent": args.clip_scenarios_to_extent,
        "keptFields": present,
        "missingOptionalFields": missing,
        "bounds": list(scenarios.total_bounds),
        "counts": {},
    }
    for field in ("modType", "flow", "sector", "PEM", "scenarioName"):
        if field in scenarios:
            summary["counts"][field] = {
                str(key): int(value)
                for key, value in scenarios[field].value_counts(dropna=False).items()
            }
    return scenario_path, extent_path, summary


def prepare_hillshade(args: argparse.Namespace) -> tuple[Path, Path]:
    png_path = output_path(args.output, "10HS_isswAll.png", args.overwrite)
    bounds_path = output_path(
        args.output, "10HS_isswAll_bounds.json", args.overwrite
    )
    temp = png_path.with_name(png_path.name + ".tmp.png")

    print(f"Reading hillshade: {args.hillshade}")
    try:
        with rasterio.open(args.hillshade) as src:
            if src.crs is None:
                raise ValueError("Hillshade has no CRS")
            transform, width, height = calculate_default_transform(
                src.crs, "EPSG:4326", src.width, src.height, *src.bounds
            )
            source = src.read(1, masked=True)
            valid = source.compressed()
            if not valid.size:
                raise ValueError("Hillshade contains no valid pixels")
            low, high = np.percentile(valid, [args.black_percentile, args.white_percentile])
            if high <= low:
                high = low + 1
            gray_source = np.clip((source.filled(low) - low) * 255 / (high - low), 0, 255).astype("uint8")
            mask_source = (~np.ma.getmaskarray(source)).astype("uint8") * 255
            gray = np.zeros((height, width), dtype="uint8")
            alpha = np.zeros((height, width), dtype="uint8")
            common = dict(
                src_transform=src.transform,
                src_crs=src.crs,
                dst_transform=transform,
                dst_crs="EPSG:4326",
            )
            reproject(gray_source, gray, resampling=Resampling.bilinear, **common)
            reproject(mask_source, alpha, resampling=Resampling.nearest, **common)
            profile = {
                "driver": "PNG",
                "width": width,
                "height": height,
                "count": 4,
                "dtype": "uint8",
                "transform": transform,
                "crs": "EPSG:4326",
            }
            with rasterio.open(temp, "w", **profile) as dst:
                dst.write(gray, 1)
                dst.write(gray, 2)
                dst.write(gray, 3)
                dst.write(alpha, 4)
            west, south, east, north = rasterio.transform.array_bounds(
                height, width, transform
            )
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    temp.replace(png_path)
    bounds_path.write_text(
        json.dumps({"bounds": [[south, west], [north, east]]}, indent=2),
        encoding="utf-8",
    )
    return png_path, bounds_path


def create_pmtiles(args: argparse.Namespace, geojson_path: Path) -> Path | None:
    if not args.pmtiles:
        return None
    tippecanoe = shutil.which("tippecanoe")
    if tippecanoe is None:
        raise RuntimeError(
            "--pmtiles requires tippecanoe in PATH. The GeoJSON output is complete."
        )
    pmtiles_path = output_path(
        args.output, Path(args.vector_name).with_suffix(".pmtiles").name, args.overwrite
    )
    command = [
        tippecanoe,
        "--output",
        str(pmtiles_path),
        "--layer",
        "ava_scenarios",
        "--minimum-zoom",
        str(args.min_zoom),
        "--maximum-zoom",
        str(args.max_zoom),
        "--no-feature-limit",
        "--no-tile-size-limit",
    ]
    if args.overwrite:
        command.append("--force")
    command.append(str(geojson_path))
    print("Running:", subprocess.list2cmdline(command))
    subprocess.run(command, check=True)
    return pmtiles_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", type=Path, default=DEFAULT_SCENARIO)
    parser.add_argument("--extent", type=Path, default=DEFAULT_EXTENT)
    parser.add_argument("--hillshade", type=Path, default=DEFAULT_HILLSHADE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--vector-name", default=DEFAULT_VECTOR_NAME)
    parser.add_argument("--simplify-metres", type=float, default=2.0)
    parser.add_argument("--clip-scenarios-to-extent", action="store_true")
    parser.add_argument("--black-percentile", type=float, default=2.0)
    parser.add_argument("--white-percentile", type=float, default=98.0)
    parser.add_argument("--pmtiles", action="store_true")
    parser.add_argument("--min-zoom", type=int, default=10)
    parser.add_argument("--max-zoom", type=int, default=16)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    for source in (args.scenario, args.extent, args.hillshade):
        if not source.is_file():
            raise FileNotFoundError(source)

    scenario_path, extent_path, summary = prepare_vectors(args)
    hillshade_path, bounds_path = prepare_hillshade(args)
    pmtiles_path = create_pmtiles(args, scenario_path)
    summary_path = output_path(
        args.output, Path(args.vector_name).with_suffix(".preview.json").name, args.overwrite
    )
    summary["files"] = {
        "scenario": scenario_path.name,
        "extent": extent_path.name,
        "hillshade": hillshade_path.name,
        "hillshadeBounds": bounds_path.name,
        "pmtiles": pmtiles_path.name if pmtiles_path else None,
    }
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print("\nPrepared preview data:")
    for path in (scenario_path, extent_path, hillshade_path, bounds_path, summary_path):
        print(f"  {path.name}: {size_mb(path):.2f} MB")
    if pmtiles_path:
        print(f"  {pmtiles_path.name}: {size_mb(pmtiles_path):.2f} MB")


if __name__ == "__main__":
    main()
