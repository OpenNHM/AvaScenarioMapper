#!/usr/bin/env python3
"""Create merged TIFFs from an existing Mapper scenario output."""

import argparse
import logging
from pathlib import Path

import geopandas as gpd

import out1Utils.scenarioRasterUtils as scenarioRasterUtils


def _parseArgs():
    parser = argparse.ArgumentParser(
        description="Merge raster paths from a scenario GeoParquet or GPKG."
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--data-root", type=Path)
    parser.add_argument(
        "--map-types",
        default="pathZdelta,pathTravelanglemax,pathTravellengthmax",
    )
    parser.add_argument("--resolution", type=float, default=10.0)
    parser.add_argument("--nodata", type=float, default=-9999.0)
    parser.add_argument("--extent", default="")
    parser.add_argument("--rel-to-100", action="store_true")
    parser.add_argument("--ignore-zero", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parseArgs()
    if not args.input.is_file():
        raise FileNotFoundError(args.input)
    if args.input.suffix.lower() == ".parquet":
        scenarioGdf = gpd.read_parquet(args.input)
    elif args.input.suffix.lower() == ".gpkg":
        scenarioGdf = gpd.read_file(args.input)
    else:
        raise ValueError("--input must be a .parquet or .gpkg file.")

    dataRoot = args.data_root or scenarioRasterUtils.deriveDataRoot(args.input)
    rasterConfig = {
        "mapTypes": scenarioRasterUtils.parseMapTypes(args.map_types),
        "resolution": args.resolution,
        "nodata": args.nodata,
        "extent": scenarioRasterUtils.parseExtent(args.extent),
        "relTo100": args.rel_to_100,
        "ignoreZero": args.ignore_zero,
        "overwrite": args.overwrite,
    }
    scenarioRasterUtils.makeScenarioRasters(
        scenarioGdf=scenarioGdf,
        scenarioName=args.input.stem,
        scenMapsDir=args.output_dir,
        pathBaseDir=args.input.parent,
        dataRoot=dataRoot,
        rasterConfig=rasterConfig,
    )
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    raise SystemExit(main())
