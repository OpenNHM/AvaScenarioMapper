#!/usr/bin/env python3
"""Restore raster path strings in an AvaDirectory GeoParquet.

Paths can be reconstructed from clipped per-PRA rasters, original FlowPy
results, or a clipped-first combination of both. The input is processed one
Parquet row group at a time, existing values are preserved by default, and the
input file is never overwritten.

This script does not clip or create TIFFs. In particular, ``source_only``
stores the large regional FlowPy raster paths. Use
``runAvaDirClipFromEuregio.py`` when per-PRA rasters clipped with ``res``
geometries are required.

Example for the EUREGIO source rasters::

    python runResDataForSubset.py /path/avaDirectoryResults_EUREGIO.parquet \
        --mode source_only --output /path/avaDirectoryResults_EUREGIO_withPaths.parquet
"""

from __future__ import annotations

import argparse
import gc
import json
import logging
import math
import os
import shutil
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Sequence

import geopandas as gpd
import pandas as pd
import pyarrow.parquet as pq

from helper.runAvaDirMergePathsToEuregio import (
    PATH_COLS,
    combine_geo_parquet_parts,
    normalize_path_cols,
    read_geo_row_group,
    write_geo_chunk,
)


log = logging.getLogger(__name__)

DEFAULT_FLOWPY_ROOTS = {
    "Tirol": Path(
        "/media/christoph/SSD 500 GB/Cairos/ModelChainResults/Euregio/"
        "NTirol/251023/alpha32_3_umax8_18_maxS5/09_flowPyBigDataStructure"
    ),
    "Südtirol": Path(
        "/media/christoph/SSD 500 GB/Cairos/ModelChainResults/Euregio/"
        "STirol/251023/alpha32_3_umax8_18_maxS5/09_flowPyBigDataStructure"
    ),
    "Trentino": Path(
        "/media/christoph/SSD 500 GB/Cairos/ModelChainResults/Euregio/"
        "Trentino/251023/alpha32_3_umax8_18_maxS5/09_flowPyBigDataStructure"
    ),
}

TYPE_PATTERNS = {
    "inputPRA": "-area_m.tif",
    "cellCounts": "_cellCounts_lzw.tif",
    "zDelta": "_zdelta_lzw.tif",
    "zDelta_sized": "_zdelta_sized_lzw.tif",
    "travelLengthMax": "_travelLengthMax_lzw.tif",
    "travelLengthMax_sized": "_travelLengthMax_sized_lzw.tif",
    "travelAngleMax": "_fpTravelAngleMax_lzw.tif",
    "travelAngleMax_sized": "_fpTravelAngleMax_sized_lzw.tif",
}

TYPE_TO_PATH_COL = {
    "cellCounts": "pathCellcounts",
    "inputPRA": "pathInputpra",
    "travelAngleMax": "pathTravelanglemax",
    "travelAngleMax_sized": "pathTravelanglemax_sized",
    "travelLengthMax": "pathTravellengthmax",
    "travelLengthMax_sized": "pathTravellengthmax_sized",
    "zDelta": "pathZdelta",
    "zDelta_sized": "pathZdelta_sized",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Restore AvaDirectory raster paths from clipped rasters and/or "
            "the original FlowPy result directories."
        )
    )
    parser.add_argument("input", type=Path, help="Input AvaDirectory GeoParquet.")
    parser.add_argument(
        "--output",
        type=Path,
        help="Output GeoParquet; defaults to INPUT_withPaths.parquet.",
    )
    parser.add_argument(
        "--mode",
        required=True,
        choices=("clipped_only", "source_only", "prefer_clipped"),
        help=(
            "clipped_only uses existing per-PRA rasters; source_only stores "
            "raw regional FlowPy paths (no clipping); prefer_clipped falls "
            "back to raw FlowPy paths. This command never creates TIFFs."
        ),
    )
    parser.add_argument(
        "--clip-root",
        type=Path,
        help="Root containing com4_RESULTID folders with clipped TIFFs.",
    )
    parser.add_argument(
        "--flowpy-root",
        action="append",
        metavar="REGION=PATH",
        help=(
            "FlowPy big-data root for an LKRegion; repeat as needed. If omitted, "
            "the current Tirol, Südtirol and Trentino roots are used."
        ),
    )
    parser.add_argument(
        "--fallback-root",
        type=Path,
        help="Optional FlowPy root for missing or unmapped LKRegion values.",
    )
    parser.add_argument(
        "--lkgebiet-id",
        action="append",
        metavar="ID[,ID...]",
        help=(
            "Only restore rows with these LKGebietID values. May be repeated "
            "and accepts comma-separated IDs."
        ),
    )
    parser.add_argument(
        "--selected-only",
        action="store_true",
        help=(
            "Write only the selected LKGebietID rows. Requires --lkgebiet-id; "
            "paired rel and res rows are retained."
        ),
    )
    parser.add_argument(
        "--path-style",
        choices=("absolute", "relative"),
        default="absolute",
        help=(
            "How paths are stored. Absolute is safest when subset tables move; "
            "relative paths are based on the output directory."
        ),
    )
    parser.add_argument(
        "--replace-existing",
        action="store_true",
        help="Replace populated path cells instead of filling only empty cells.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing output file; the input is still protected.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve and count paths without writing an output file.",
    )
    parser.add_argument(
        "--keep-temp-parts",
        action="store_true",
        help="Keep temporary row-group outputs after a successful run.",
    )
    return parser


def _parse_flowpy_roots(raw_roots: Sequence[str] | None) -> dict[str, Path]:
    if not raw_roots:
        return {name: path.resolve() for name, path in DEFAULT_FLOWPY_ROOTS.items()}

    roots = {}
    for value in raw_roots:
        if "=" not in value:
            raise ValueError(
                f"Invalid --flowpy-root '{value}'; expected REGION=/path/to/root"
            )
        region, raw_path = value.split("=", 1)
        region = region.strip()
        raw_path = raw_path.strip()
        if not region or not raw_path:
            raise ValueError(
                f"Invalid --flowpy-root '{value}'; expected REGION=/path/to/root"
            )
        roots[region] = Path(raw_path).expanduser().resolve()
    return roots


def _parse_lkgebiet_ids(raw_values: Sequence[str] | None) -> set[int]:
    selected = set()
    for raw_value in raw_values or []:
        for value in raw_value.split(","):
            value = value.strip()
            if not value:
                continue
            try:
                selected.add(int(value))
            except ValueError as error:
                raise ValueError(
                    f"Invalid --lkgebiet-id value '{value}'; expected integers."
                ) from error
    return selected


def _validate_args(args: argparse.Namespace) -> dict[str, Path]:
    args.input = args.input.expanduser().resolve()
    if not args.input.is_file():
        raise FileNotFoundError(args.input)
    if args.input.suffix.lower() not in {".parquet", ".geoparquet"}:
        raise ValueError(f"Input must be a GeoParquet file: {args.input}")

    if args.output is None:
        args.output = args.input.with_name(f"{args.input.stem}_withPaths.parquet")
    args.output = args.output.expanduser().resolve()
    if args.output == args.input:
        raise ValueError("Output must differ from input; in-place updates are disabled.")
    if not args.dry_run and args.output.exists() and not args.overwrite:
        raise FileExistsError(f"Output exists; pass --overwrite to replace it: {args.output}")

    args.lkgebiet_ids = _parse_lkgebiet_ids(args.lkgebiet_id)
    if args.selected_only and not args.lkgebiet_ids:
        raise ValueError("--selected-only requires at least one --lkgebiet-id.")

    if args.mode in {"clipped_only", "prefer_clipped"}:
        if args.clip_root is None:
            raise ValueError(f"--clip-root is required for mode {args.mode}")
        args.clip_root = args.clip_root.expanduser().resolve()
        if not args.clip_root.is_dir():
            raise FileNotFoundError(args.clip_root)

    roots = _parse_flowpy_roots(args.flowpy_root)
    if args.mode in {"source_only", "prefer_clipped"}:
        existing_roots = {}
        for region, root in roots.items():
            if root.is_dir():
                existing_roots[region] = root
            else:
                log.warning("Missing FlowPy root for %s: %s", region, root)
        roots = existing_roots
        if args.fallback_root:
            args.fallback_root = args.fallback_root.expanduser().resolve()
            if not args.fallback_root.is_dir():
                raise FileNotFoundError(args.fallback_root)
        if not roots and args.fallback_root is None:
            raise ValueError("No valid FlowPy roots are available.")
    return roots


def _ensure_schema(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    required = ["praID", "resultID"]
    missing = [column for column in required if column not in gdf.columns]
    if missing:
        raise ValueError(f"Missing required columns: {missing}")
    if gdf.geometry is None:
        raise ValueError("Missing geometry column")

    gdf["praID"] = pd.to_numeric(gdf["praID"], errors="coerce").astype("Int64")
    gdf["resultID"] = gdf["resultID"].astype("string").str.strip()
    if "LKRegion" in gdf.columns:
        gdf["LKRegion"] = gdf["LKRegion"].astype("string").str.strip()
    if "modType" in gdf.columns:
        gdf["modType"] = gdf["modType"].astype("string").str.strip()
    return normalize_path_cols(gdf)


def _filter_selected_rows(
    gdf: gpd.GeoDataFrame,
    lkgebiet_ids: set[int],
) -> gpd.GeoDataFrame:
    if not lkgebiet_ids:
        return gdf
    if "LKGebietID" not in gdf.columns:
        raise ValueError("--lkgebiet-id requires an LKGebietID column.")
    ids = pd.to_numeric(gdf["LKGebietID"], errors="coerce")
    return gdf.loc[ids.isin(lkgebiet_ids)].copy()


def _discover_target_regions(
    parquet_file: pq.ParquetFile,
    lkgebiet_ids: set[int],
) -> set[str]:
    if not lkgebiet_ids:
        return set()
    required = {"LKGebietID", "LKRegion"}
    if not required.issubset(parquet_file.schema_arrow.names):
        missing = sorted(required - set(parquet_file.schema_arrow.names))
        raise ValueError(f"Targeted restoration requires columns: {missing}")

    regions = set()
    for batch in parquet_file.iter_batches(columns=["LKGebietID", "LKRegion"]):
        frame = batch.to_pandas()
        ids = pd.to_numeric(frame["LKGebietID"], errors="coerce")
        values = frame.loc[ids.isin(lkgebiet_ids), "LKRegion"].dropna()
        regions.update(str(value).strip() for value in values if str(value).strip())
    return regions


def _extend_bounds(
    current: tuple[float, float, float, float] | None,
    gdf: gpd.GeoDataFrame,
) -> tuple[float, float, float, float] | None:
    if gdf.empty:
        return current
    bounds = tuple(float(value) for value in gdf.total_bounds)
    if len(bounds) != 4 or not all(math.isfinite(value) for value in bounds):
        return current
    if current is None:
        return bounds
    return (
        min(current[0], bounds[0]),
        min(current[1], bounds[1]),
        max(current[2], bounds[2]),
        max(current[3], bounds[3]),
    )


def _geo_metadata_with_bounds(
    raw_metadata: bytes | None,
    bounds: tuple[float, float, float, float] | None,
) -> bytes | None:
    if raw_metadata is None or bounds is None:
        return raw_metadata
    metadata = json.loads(raw_metadata.decode("utf-8"))
    primary_column = metadata.get("primary_column", "geometry")
    geometry_metadata = metadata.get("columns", {}).get(primary_column)
    if geometry_metadata is not None:
        geometry_metadata["bbox"] = list(bounds)
    return json.dumps(metadata).encode("utf-8")


def _normalize_region(value) -> str | None:
    if pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


def _build_res_dir_indices(root: Path) -> tuple[dict[str, Path], dict[str, Path]]:
    started = time.perf_counter()
    peak_index = {}
    size_index = {}
    for directory_path, directory_names, _ in os.walk(root):
        base_name = os.path.basename(directory_path)
        if base_name not in {"peakFiles", "sizeFiles"}:
            continue
        for directory_name in directory_names:
            if not directory_name.startswith("res_"):
                continue
            result_id = directory_name.removeprefix("res_").strip()
            result_directory = Path(directory_path) / directory_name
            if base_name == "peakFiles":
                peak_index[result_id] = result_directory
            else:
                size_index[result_id] = result_directory
    log.info(
        "Indexed %s: peakFiles=%d sizeFiles=%d in %.2fs",
        root,
        len(peak_index),
        len(size_index),
        time.perf_counter() - started,
    )
    return peak_index, size_index


def _pick_latest_by_suffix(folder: Path, suffix: str) -> Path | None:
    candidates = list(folder.glob(f"*{suffix}"))
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _find_rel_area_raster(result_directory: Path) -> Path | None:
    try:
        outputs_directory = result_directory.parent.parent.parent
        flow_directory = outputs_directory.parent.parent
        release_directory = flow_directory / "Inputs/REL"
        if not release_directory.is_dir():
            return None
        return next(iter(sorted(release_directory.glob(f"*{TYPE_PATTERNS['inputPRA']}"))), None)
    except (OSError, IndexError):
        return None


def _build_result_file_cache(
    peak_index: dict[str, Path],
    size_index: dict[str, Path],
) -> dict[str, dict[str, Path]]:
    started = time.perf_counter()
    cache = {}
    for result_id in set(peak_index) | set(size_index):
        peak_directory = peak_index.get(result_id)
        size_directory = size_index.get(result_id)
        entry = {}
        for type_key in (
            "cellCounts",
            "zDelta",
            "zDelta_sized",
            "travelLengthMax",
            "travelLengthMax_sized",
            "travelAngleMax",
            "travelAngleMax_sized",
        ):
            source_directory = size_directory if type_key.endswith("_sized") else peak_directory
            if source_directory is None:
                continue
            source_path = _pick_latest_by_suffix(
                source_directory, TYPE_PATTERNS[type_key]
            )
            if source_path:
                entry[type_key] = source_path

        base_directory = peak_directory or size_directory
        if base_directory:
            release_area = _find_rel_area_raster(base_directory)
            if release_area:
                entry["inputPRA"] = release_area
        if entry:
            cache[result_id] = entry
    log.info(
        "Built resultID-to-file cache for %d results in %.2fs",
        len(cache),
        time.perf_counter() - started,
    )
    return cache


def _build_region_caches(
    roots_by_region: dict[str, Path],
    fallback_root: Path | None,
) -> dict[str, dict[str, dict[str, Path]]]:
    caches = {}
    for region, root in roots_by_region.items():
        log.info("Building FlowPy cache for %s: %s", region, root)
        peak_index, size_index = _build_res_dir_indices(root)
        caches[region] = _build_result_file_cache(peak_index, size_index)
    if fallback_root:
        log.info("Building fallback FlowPy cache: %s", fallback_root)
        peak_index, size_index = _build_res_dir_indices(fallback_root)
        caches["__fallback__"] = _build_result_file_cache(peak_index, size_index)
    return caches


def _build_clipped_file_index(clip_root: Path) -> dict[tuple[str, int, str], Path]:
    started = time.perf_counter()
    index = {}
    result_directories = sorted(
        path
        for path in clip_root.iterdir()
        if path.is_dir() and path.name.startswith("com4_")
    )
    for output_directory in result_directories:
        result_id = output_directory.name.removeprefix("com4_").strip()
        if not result_id:
            continue
        for tif_path in output_directory.glob("*.tif"):
            if not tif_path.name.startswith("praID"):
                continue
            try:
                pra_id = int(
                    tif_path.name.split("_", 1)[0].removeprefix("praID")
                )
            except ValueError:
                continue
            type_key = next(
                (
                    candidate_type
                    for candidate_type, suffix in TYPE_PATTERNS.items()
                    if tif_path.name.endswith(suffix)
                ),
                None,
            )
            if type_key:
                index[(result_id, pra_id, type_key)] = tif_path
    log.info(
        "Indexed %d clipped TIFFs from %d com4 folders in %.2fs",
        len(index),
        len(result_directories),
        time.perf_counter() - started,
    )
    return index


def _get_source_map(
    result_id: str,
    region: str | None,
    region_caches: dict[str, dict[str, dict[str, Path]]],
) -> dict[str, Path] | None:
    if region in region_caches:
        return region_caches[region].get(result_id)
    fallback = region_caches.get("__fallback__")
    return fallback.get(result_id) if fallback is not None else None


def _resolve_candidate(
    mode: str,
    type_key: str,
    pra_id: int,
    result_id: str,
    region: str | None,
    clipped_index: dict[tuple[str, int, str], Path],
    region_caches: dict[str, dict[str, dict[str, Path]]],
) -> tuple[Path | None, str]:
    if mode in {"clipped_only", "prefer_clipped"}:
        clipped_path = clipped_index.get((result_id, pra_id, type_key))
        if clipped_path is not None and clipped_path.is_file():
            return clipped_path, "clipped"
        if mode == "clipped_only":
            return None, "missing_clipped"

    source_map = _get_source_map(result_id, region, region_caches)
    if source_map is None:
        return None, "missing_both" if mode == "prefer_clipped" else "no_source_map"
    source_path = source_map.get(type_key)
    if source_path is None or not source_path.is_file():
        return None, "missing_both" if mode == "prefer_clipped" else "missing_source_type"
    return source_path, "source"


def _format_path(path: Path, output_directory: Path, path_style: str) -> str:
    resolved = path.resolve()
    if path_style == "absolute":
        return str(resolved)
    return os.path.relpath(resolved, start=output_directory)


def _has_path(value) -> bool:
    if pd.isna(value):
        return False
    text = str(value).strip()
    return bool(text) and text.lower() not in {"none", "nan", "<na>"}


def _res_mask(gdf: gpd.GeoDataFrame) -> pd.Series:
    if "modType" not in gdf.columns:
        return pd.Series(True, index=gdf.index)
    normalized = gdf["modType"].astype("string").str.strip().str.lower()
    return normalized.isna() | normalized.eq("res")


def _restore_chunk_paths(
    gdf: gpd.GeoDataFrame,
    args: argparse.Namespace,
    clipped_index: dict[tuple[str, int, str], Path],
    region_caches: dict[str, dict[str, dict[str, Path]]],
) -> tuple[gpd.GeoDataFrame, Counter]:
    stats = Counter()
    mask = _res_mask(gdf)
    if args.lkgebiet_ids:
        if "LKGebietID" not in gdf.columns:
            raise ValueError("--lkgebiet-id requires an LKGebietID column.")
        lkgebiet_values = pd.to_numeric(gdf["LKGebietID"], errors="coerce")
        mask &= lkgebiet_values.isin(args.lkgebiet_ids)
    stats["res_rows"] = int(mask.sum())
    stats["rows_not_targeted"] = int((~mask).sum())
    selected = gdf.loc[mask]
    indices = selected.index.to_list()
    pra_ids = selected["praID"].to_list()
    result_ids = selected["resultID"].to_list()
    if "LKRegion" in selected.columns:
        regions = selected["LKRegion"].to_list()
    else:
        regions = [None] * len(selected)

    for type_key, column in TYPE_TO_PATH_COL.items():
        existing_values = selected[column].to_list()
        update_indices = []
        update_values = []
        for index, pra_value, result_value, region_value, existing in zip(
            indices, pra_ids, result_ids, regions, existing_values
        ):
            if not args.replace_existing and _has_path(existing):
                stats["preserved_existing_cells"] += 1
                continue
            if pd.isna(pra_value) or pd.isna(result_value):
                stats["invalid_key_cells"] += 1
                continue
            candidate, status = _resolve_candidate(
                args.mode,
                type_key,
                int(pra_value),
                str(result_value),
                _normalize_region(region_value),
                clipped_index,
                region_caches,
            )
            stats[status] += 1
            if candidate is not None:
                update_indices.append(index)
                update_values.append(
                    _format_path(candidate, args.output.parent, args.path_style)
                )
        if update_indices:
            gdf.loc[update_indices, column] = update_values
            stats["updated_cells"] += len(update_indices)
            stats[f"updated_{column}"] += len(update_indices)
        gdf[column] = gdf[column].astype("string")
    return gdf, stats


def _log_summary(stats: Counter, elapsed: float) -> None:
    log.info("Rows processed as res: %d", stats["res_rows"])
    log.info("Rows not targeted for path restoration: %d", stats["rows_not_targeted"])
    log.info("Updated path cells: %d", stats["updated_cells"])
    for column in PATH_COLS:
        if stats[f"updated_{column}"]:
            log.info("  %-28s %d", column, stats[f"updated_{column}"])
    log.info("Preserved existing path cells: %d", stats["preserved_existing_cells"])
    log.info("Found clipped paths: %d", stats["clipped"])
    log.info("Found source paths: %d", stats["source"])
    log.info("Missing clipped paths: %d", stats["missing_clipped"])
    log.info("Missing source maps: %d", stats["no_source_map"])
    log.info("Missing source raster types: %d", stats["missing_source_type"])
    log.info("Missing clipped and source paths: %d", stats["missing_both"])
    log.info("Invalid key cells: %d", stats["invalid_key_cells"])
    log.info("Completed in %.2fs", elapsed)


def main(argv: Sequence[str] | None = None) -> int:
    started = time.perf_counter()
    args = build_parser().parse_args(argv)
    flowpy_roots = _validate_args(args)

    log.info("Input GeoParquet: %s", args.input)
    log.info("Output GeoParquet: %s", args.output)
    log.info("Raster path mode: %s", args.mode)
    log.info("Stored path style: %s", args.path_style)
    log.info("Replace existing cells: %s", args.replace_existing)
    log.info("Dry run: %s", args.dry_run)

    parquet_file = pq.ParquetFile(args.input)
    if b"geo" not in (parquet_file.schema_arrow.metadata or {}):
        raise ValueError(f"Input has no GeoParquet metadata: {args.input}")
    log.info(
        "Input rows=%d, row groups=%d",
        parquet_file.metadata.num_rows,
        parquet_file.num_row_groups,
    )
    if args.lkgebiet_ids:
        log.info("Selected LKGebietID values: %s", sorted(args.lkgebiet_ids))
        target_regions = _discover_target_regions(parquet_file, args.lkgebiet_ids)
        log.info("Selected LKRegion values: %s", sorted(target_regions))
        flowpy_roots = {
            region: root
            for region, root in flowpy_roots.items()
            if region in target_regions
        }
        missing_roots = target_regions - set(flowpy_roots)
        if (
            missing_roots
            and args.mode in {"source_only", "prefer_clipped"}
            and args.fallback_root is None
        ):
            raise ValueError(
                "No FlowPy root configured for selected LKRegion values: "
                f"{sorted(missing_roots)}"
            )

    clipped_index = {}
    if args.mode in {"clipped_only", "prefer_clipped"}:
        clipped_index = _build_clipped_file_index(args.clip_root)

    region_caches = {}
    if args.mode in {"source_only", "prefer_clipped"}:
        region_caches = _build_region_caches(flowpy_roots, args.fallback_root)

    part_root = None
    part_files = []
    totals = Counter()
    output_bounds = None
    if not args.dry_run:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        part_root = Path(
            tempfile.mkdtemp(prefix=f".{args.output.stem}_parts_", dir=args.output.parent)
        )

    try:
        for row_group_index in range(parquet_file.num_row_groups):
            log.info(
                "Processing row group %d/%d",
                row_group_index + 1,
                parquet_file.num_row_groups,
            )
            gdf = read_geo_row_group(args.input, row_group_index)
            gdf = _ensure_schema(gdf)
            if args.selected_only:
                gdf = _filter_selected_rows(gdf, args.lkgebiet_ids)
            gdf, chunk_stats = _restore_chunk_paths(
                gdf, args, clipped_index, region_caches
            )
            totals.update(chunk_stats)
            if not args.dry_run:
                output_bounds = _extend_bounds(output_bounds, gdf)
                part_path = part_root / f"part-{row_group_index:05d}.parquet"
                write_geo_chunk(gdf, part_path)
                part_files.append(part_path)
            del gdf
            gc.collect()

        if args.dry_run:
            log.info("Dry run complete; no output written.")
        else:
            input_metadata = parquet_file.schema_arrow.metadata or {}
            geo_metadata = input_metadata.get(b"geo")
            if args.selected_only:
                geo_metadata = _geo_metadata_with_bounds(
                    geo_metadata,
                    output_bounds,
                )
            combine_geo_parquet_parts(
                part_files,
                args.output,
                geo_metadata=geo_metadata,
            )
            log.info("Wrote GeoParquet: %s", args.output)
        _log_summary(totals, time.perf_counter() - started)
        return 0
    finally:
        if part_root and part_root.exists() and not args.keep_temp_parts:
            shutil.rmtree(part_root)
            log.info("Removed temporary parts: %s", part_root)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    raise SystemExit(main())
