#!/usr/bin/env python3
"""Build a clipped AvaDirectory subset directly from EUREGIO.

The workflow keeps the selected ``rel``/``res`` row pairs, but creates raster
paths only for ``res`` rows. Every configured FlowPy source raster is clipped
with that row's ``res`` geometry and written below ``clipOutputRoot``. The
final GeoParquet is published atomically only after every requested clipped
raster exists.

Use ``--check`` for a read-only preflight. Existing clipped TIFFs are reused,
so an interrupted full run can be started again safely.
"""

from __future__ import annotations

import argparse
import configparser
import logging
import os
import re
import tempfile
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import geopandas as gpd
import pandas as pd
import rasterio
from rasterio.mask import mask as raster_mask
from rasterio.warp import transform_geom


log = logging.getLogger("avaDirClipFromEuregio")

DEFAULT_CONFIG = Path(__file__).with_name("avaDirClipCfg.ini")

RASTER_SPECS = {
    "pathCellcounts": ("peak", "_cellCounts_lzw.tif"),
    "pathInputpra": ("release", "-area_m.tif"),
    "pathTravelanglemax": ("peak", "_fpTravelAngleMax_lzw.tif"),
    "pathTravelanglemax_sized": ("size", "_fpTravelAngleMax_sized_lzw.tif"),
    "pathTravellengthmax": ("peak", "_travelLengthMax_lzw.tif"),
    "pathTravellengthmax_sized": ("size", "_travelLengthMax_sized_lzw.tif"),
    "pathZdelta": ("peak", "_zdelta_lzw.tif"),
    "pathZdelta_sized": ("size", "_zdelta_sized_lzw.tif"),
}
PATH_COLUMNS = tuple(RASTER_SPECS)
RESULT_PATH_COLUMNS = tuple(
    column for column in PATH_COLUMNS if column != "pathInputpra"
)
PAIR_KEY_COLUMNS = ["praID", "resultID"]
ROW_KEY_COLUMNS = PAIR_KEY_COLUMNS + ["modType"]
REQUIRED_COLUMNS = ROW_KEY_COLUMNS + ["LKGebietID", "LKRegion", "geometry"]
SAFE_RESULT_ID = re.compile(r"^[A-Za-z0-9._-]+$")
FLOWPY_ROOT_ALIASES = {
    "ntirol": "Tirol",
    "nordtirol": "Tirol",
    "tirol": "Tirol",
    "stirol": "Südtirol",
    "suedtirol": "Südtirol",
    "südtirol": "Südtirol",
    "trentino": "Trentino",
}


@dataclass(frozen=True)
class WorkflowConfig:
    input_parquet: Path
    output_parquet: Path
    clip_output_root: Path
    lkgebiet_ids: frozenset[int]
    flowpy_roots: dict[str, Path]
    path_columns: tuple[str, ...]
    path_style: str
    max_workers: int
    max_pending_jobs: int
    log_every_seconds: float
    overwrite_clips: bool
    overwrite_parquet: bool


def _configured_path(
    parser: configparser.ConfigParser,
    section: str,
    option: str,
) -> Path:
    value = parser.get(section, option, fallback="").strip()
    if not value:
        raise ValueError(f"Missing [{section}] {option} in the configuration.")
    return Path(os.path.expandvars(value)).expanduser().resolve()


def _parse_integer_list(value: str, label: str) -> frozenset[int]:
    selected = set()
    for item in value.replace("\n", ",").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            selected.add(int(item))
        except ValueError as error:
            raise ValueError(f"Invalid integer '{item}' in {label}.") from error
    if not selected:
        raise ValueError(f"No values configured for {label}.")
    return frozenset(selected)


def _parse_path_columns(value: str) -> tuple[str, ...]:
    requested = [
        item.strip()
        for item in value.replace("\n", ",").split(",")
        if item.strip()
    ]
    if len(requested) == 1 and requested[0].lower() == "all_results":
        return RESULT_PATH_COLUMNS
    if len(requested) == 1 and requested[0].lower() == "all":
        return PATH_COLUMNS
    unknown = sorted(set(requested) - set(PATH_COLUMNS))
    if unknown:
        raise ValueError(
            f"Unknown [RASTERS] rasterPathColumns: {unknown}. "
            f"Allowed: {list(PATH_COLUMNS)}, all_results, or all."
        )
    if not requested:
        raise ValueError("[RASTERS] rasterPathColumns must not be empty.")
    return tuple(dict.fromkeys(requested))


def load_config(config_path: Path) -> WorkflowConfig:
    config_path = config_path.expanduser().resolve()

    # Match the mapper configuration convention: a main INI is loaded first,
    # then local_<name>.ini overrides only the values that differ locally.
    # Passing the local file itself is also supported; its matching main file
    # is inferred from the filename.
    if config_path.name.startswith("local_"):
        main_config_path = config_path.with_name(
            config_path.name.removeprefix("local_")
        )
        local_config_path = config_path
    else:
        main_config_path = config_path
        local_config_path = config_path.with_name(f"local_{config_path.name}")

    if not main_config_path.is_file():
        raise FileNotFoundError(main_config_path)
    if config_path.name.startswith("local_") and not local_config_path.is_file():
        raise FileNotFoundError(local_config_path)

    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    with main_config_path.open("r", encoding="utf-8") as stream:
        parser.read_file(stream)
    log.info("Loaded main configuration: %s", main_config_path)
    if local_config_path.is_file():
        parser.read(local_config_path, encoding="utf-8")
        log.info("Loaded local override: %s", local_config_path)
    else:
        log.info("No local override found: %s", local_config_path)

    input_parquet = _configured_path(parser, "PATHS", "inputParquet")
    output_parquet = _configured_path(parser, "PATHS", "outputParquet")
    clip_output_root = _configured_path(parser, "PATHS", "clipOutputRoot")
    if input_parquet == output_parquet:
        raise ValueError("inputParquet and outputParquet must be different files.")

    lkgebiet_ids = _parse_integer_list(
        parser.get("SELECTION", "LKGebietID", fallback=""),
        "[SELECTION] LKGebietID",
    )
    path_columns = _parse_path_columns(
        parser.get("RASTERS", "rasterPathColumns", fallback="all_results")
    )
    path_style = parser.get(
        "RASTERS", "pathStyle", fallback="absolute"
    ).strip().lower()
    if path_style not in {"absolute", "relative"}:
        raise ValueError("[RASTERS] pathStyle must be absolute or relative.")

    if not parser.has_section("FLOWPY_ROOTS"):
        raise ValueError("Missing [FLOWPY_ROOTS] section.")
    flowpy_roots = {}
    for configured_region, raw_path in parser.items("FLOWPY_ROOTS"):
        configured_region = configured_region.strip()
        raw_path = raw_path.strip()
        if not configured_region or not raw_path:
            continue
        parquet_region = FLOWPY_ROOT_ALIASES.get(
            configured_region.casefold(), configured_region
        )
        if parquet_region in flowpy_roots:
            raise ValueError(
                "Multiple [FLOWPY_ROOTS] entries map to LKRegion "
                f"'{parquet_region}'."
            )
        flowpy_roots[parquet_region] = (
            Path(os.path.expandvars(raw_path)).expanduser().resolve()
        )
        if parquet_region != configured_region:
            log.info(
                "Mapped FlowPy root alias %s -> LKRegion %s",
                configured_region,
                parquet_region,
            )
    if not flowpy_roots:
        raise ValueError("[FLOWPY_ROOTS] contains no region paths.")

    max_workers = parser.getint(
        "PROCESSING", "maxWorkers", fallback=min(16, os.cpu_count() or 8)
    )
    max_pending_jobs = parser.getint(
        "PROCESSING", "maxPendingJobs", fallback=max_workers * 4
    )
    if max_workers < 1:
        raise ValueError("[PROCESSING] maxWorkers must be at least 1.")
    if max_pending_jobs < max_workers:
        raise ValueError(
            "[PROCESSING] maxPendingJobs must be at least maxWorkers."
        )

    return WorkflowConfig(
        input_parquet=input_parquet,
        output_parquet=output_parquet,
        clip_output_root=clip_output_root,
        lkgebiet_ids=lkgebiet_ids,
        flowpy_roots=flowpy_roots,
        path_columns=path_columns,
        path_style=path_style,
        max_workers=max_workers,
        max_pending_jobs=max_pending_jobs,
        log_every_seconds=parser.getfloat(
            "PROCESSING", "logEverySeconds", fallback=30.0
        ),
        overwrite_clips=parser.getboolean(
            "PROCESSING", "overwriteClips", fallback=False
        ),
        overwrite_parquet=parser.getboolean(
            "PROCESSING", "overwriteParquet", fallback=False
        ),
    )


def _validate_input_paths(config: WorkflowConfig, check_only: bool) -> None:
    if not config.input_parquet.is_file():
        raise FileNotFoundError(config.input_parquet)
    if config.input_parquet.suffix.lower() not in {".parquet", ".geoparquet"}:
        raise ValueError(f"Input must be GeoParquet: {config.input_parquet}")
    if (
        config.output_parquet.exists()
        and not config.overwrite_parquet
        and not check_only
    ):
        raise FileExistsError(
            "Output already exists and overwriteParquet=False: "
            f"{config.output_parquet}"
        )
    for region, root in config.flowpy_roots.items():
        if not root.is_dir():
            raise FileNotFoundError(f"FlowPy root for {region}: {root}")


def _load_selected_rows(config: WorkflowConfig) -> gpd.GeoDataFrame:
    ids = sorted(config.lkgebiet_ids)
    log.info("Reading selected LKGebietID values from %s", config.input_parquet)
    gdf = gpd.read_parquet(
        config.input_parquet,
        filters=[("LKGebietID", "in", ids)],
    )
    missing_columns = sorted(set(REQUIRED_COLUMNS) - set(gdf.columns))
    if missing_columns:
        raise ValueError(f"Input is missing required columns: {missing_columns}")
    if gdf.crs is None:
        raise ValueError("Input GeoParquet has no CRS.")

    numeric_ids = pd.to_numeric(gdf["LKGebietID"], errors="coerce")
    gdf = gdf.loc[numeric_ids.isin(config.lkgebiet_ids)].copy()
    gdf["modType"] = gdf["modType"].astype("string").str.strip().str.lower()
    gdf = gdf.loc[gdf["modType"].isin(["rel", "res"])].copy()
    gdf["praID"] = pd.to_numeric(gdf["praID"], errors="coerce").astype("Int64")
    gdf["resultID"] = gdf["resultID"].astype("string").str.strip()
    gdf["LKRegion"] = gdf["LKRegion"].astype("string").str.strip()

    missing_ids = sorted(config.lkgebiet_ids - set(numeric_ids.dropna().astype(int)))
    if missing_ids:
        raise ValueError(f"Configured LKGebietID values not found: {missing_ids}")
    if gdf.empty:
        raise ValueError("The LKGebietID selection returned no rel/res rows.")
    if gdf[["praID", "resultID", "LKRegion"]].isna().any().any():
        raise ValueError("Selected rows contain empty praID, resultID, or LKRegion values.")
    invalid_result_ids = sorted(
        {
            str(value)
            for value in gdf["resultID"].unique()
            if not SAFE_RESULT_ID.fullmatch(str(value))
        }
    )
    if invalid_result_ids:
        raise ValueError(
            "Unsafe resultID values cannot be used as directory names: "
            f"{invalid_result_ids[:10]}"
        )
    if gdf.duplicated(ROW_KEY_COLUMNS).any():
        sample = gdf.loc[gdf.duplicated(ROW_KEY_COLUMNS, keep=False), ROW_KEY_COLUMNS]
        raise ValueError(
            "Duplicate praID/resultID/modType rows found; sample: "
            f"{sample.head(10).to_dict('records')}"
        )

    pair_types = gdf.groupby(PAIR_KEY_COLUMNS, dropna=False)["modType"].agg(set)
    bad_pairs = pair_types[pair_types != {"rel", "res"}]
    if not bad_pairs.empty:
        raise ValueError(
            "Every selected avalanche must contain one rel/res pair; bad pair "
            f"count={len(bad_pairs):,}, sample={list(bad_pairs.index[:10])}"
        )

    res = gdf.loc[gdf["modType"].eq("res")]
    invalid_geometry = (
        res.geometry.isna() | res.geometry.is_empty | ~res.geometry.is_valid
    )
    if invalid_geometry.any():
        raise ValueError(
            f"Invalid res geometries found: {int(invalid_geometry.sum()):,}"
        )

    # Never carry raw or pilot paths from the main EUREGIO table into this
    # product. Only paths generated below are allowed in the output.
    for column in PATH_COLUMNS:
        gdf[column] = pd.Series(pd.NA, index=gdf.index, dtype="string")

    log.info(
        "Selected rows=%d (rel=%d, res=%d), regions=%s",
        len(gdf),
        int(gdf["modType"].eq("rel").sum()),
        int(gdf["modType"].eq("res").sum()),
        sorted(str(value) for value in gdf["LKRegion"].unique()),
    )
    return gdf


def _index_result_directories(
    root: Path,
    wanted_result_ids: set[str],
    need_peak: bool,
    need_size: bool,
) -> tuple[dict[str, Path], dict[str, Path]]:
    started = time.perf_counter()
    peak_index: dict[str, Path] = {}
    size_index: dict[str, Path] = {}
    for directory_path, directory_names, _ in os.walk(root):
        base_name = Path(directory_path).name
        if base_name not in {"peakFiles", "sizeFiles"}:
            continue
        target_index = peak_index if base_name == "peakFiles" else size_index
        for directory_name in directory_names:
            if not directory_name.startswith("res_"):
                continue
            result_id = directory_name.removeprefix("res_").strip()
            if result_id not in wanted_result_ids:
                continue
            candidate = Path(directory_path) / directory_name
            previous = target_index.get(result_id)
            if previous is not None and previous != candidate:
                raise ValueError(
                    f"Duplicate {base_name} directories for resultID {result_id}: "
                    f"{previous} and {candidate}"
                )
            target_index[result_id] = candidate
        # Result folders contain large rasters, not additional result indexes.
        directory_names[:] = []
        peak_complete = not need_peak or wanted_result_ids <= set(peak_index)
        size_complete = not need_size or wanted_result_ids <= set(size_index)
        if peak_complete and size_complete:
            break
    log.info(
        "Indexed %s: selected peak=%d, size=%d in %.1fs",
        root,
        len(peak_index),
        len(size_index),
        time.perf_counter() - started,
    )
    return peak_index, size_index


def _latest_with_suffix(folder: Path, suffix: str) -> Path | None:
    candidates = list(folder.glob(f"*{suffix}"))
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _find_release_raster(result_directory: Path) -> Path | None:
    outputs_directory = result_directory.parent.parent.parent
    flow_directory = outputs_directory.parent.parent
    release_directory = flow_directory / "Inputs" / "REL"
    if not release_directory.is_dir():
        return None
    return next(
        iter(sorted(release_directory.glob(f"*{RASTER_SPECS['pathInputpra'][1]}"))),
        None,
    )


def _build_source_caches(
    config: WorkflowConfig,
    res: gpd.GeoDataFrame,
) -> dict[str, dict[str, dict[str, Path]]]:
    requested_regions = {str(value) for value in res["LKRegion"].unique()}
    missing_roots = sorted(requested_regions - set(config.flowpy_roots))
    if missing_roots:
        raise ValueError(f"No [FLOWPY_ROOTS] entries for: {missing_roots}")

    groups = {RASTER_SPECS[column][0] for column in config.path_columns}
    need_peak = bool(groups & {"peak", "release"})
    need_size = bool(groups & {"size", "release"})
    caches: dict[str, dict[str, dict[str, Path]]] = {}
    missing: list[tuple[str, str, str]] = []

    for region in sorted(requested_regions):
        wanted = {
            str(value)
            for value in res.loc[res["LKRegion"].eq(region), "resultID"].unique()
        }
        peak_index, size_index = _index_result_directories(
            config.flowpy_roots[region], wanted, need_peak, need_size
        )
        region_cache: dict[str, dict[str, Path]] = {}
        for result_id in wanted:
            entry: dict[str, Path] = {}
            for column in config.path_columns:
                source_group, suffix = RASTER_SPECS[column]
                if source_group == "peak":
                    source_directory = peak_index.get(result_id)
                    source = (
                        _latest_with_suffix(source_directory, suffix)
                        if source_directory
                        else None
                    )
                elif source_group == "size":
                    source_directory = size_index.get(result_id)
                    source = (
                        _latest_with_suffix(source_directory, suffix)
                        if source_directory
                        else None
                    )
                else:
                    source_directory = peak_index.get(result_id) or size_index.get(
                        result_id
                    )
                    source = (
                        _find_release_raster(source_directory)
                        if source_directory
                        else None
                    )
                if source is None or not source.is_file():
                    missing.append((region, result_id, column))
                else:
                    entry[column] = source.resolve()
            region_cache[result_id] = entry
        caches[region] = region_cache

    if missing:
        sample = ", ".join(
            f"{region}/{result_id}/{column}"
            for region, result_id, column in missing[:12]
        )
        raise FileNotFoundError(
            f"Missing {len(missing):,} configured source raster mappings. "
            f"Sample: {sample}"
        )
    return caches


def _expected_output(
    clip_root: Path,
    pra_id: int,
    result_id: str,
    source: Path,
) -> Path:
    return (
        clip_root
        / f"com4_{result_id}"
        / f"praID{pra_id}_{source.name}"
    )


def _stored_path(path: Path, config: WorkflowConfig) -> str:
    if config.path_style == "absolute":
        return str(path)
    return os.path.relpath(path, start=config.output_parquet.parent)


def _resolve_stored_path(value: str, config: WorkflowConfig) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return (config.output_parquet.parent / path).resolve()


def _assign_expected_paths(
    gdf: gpd.GeoDataFrame,
    config: WorkflowConfig,
    source_caches: dict[str, dict[str, dict[str, Path]]],
) -> None:
    res_mask = gdf["modType"].eq("res")
    res = gdf.loc[res_mask, ["praID", "resultID", "LKRegion"]]
    records = list(res.itertuples(index=True, name=None))
    for column in config.path_columns:
        values = []
        for _, pra_value, result_value, region_value in records:
            pra_id = int(pra_value)
            result_id = str(result_value)
            region = str(region_value)
            source = source_caches[region][result_id][column]
            output = _expected_output(
                config.clip_output_root, pra_id, result_id, source
            )
            values.append(_stored_path(output, config))
        gdf.loc[res.index, column] = pd.array(values, dtype="string")


def _iter_clip_jobs(
    gdf: gpd.GeoDataFrame,
    config: WorkflowConfig,
    source_caches: dict[str, dict[str, dict[str, Path]]],
) -> Iterable[tuple[Path, Path, dict, object]]:
    columns = ["praID", "resultID", "LKRegion", gdf.geometry.name]
    res = gdf.loc[gdf["modType"].eq("res"), columns]
    for pra_value, result_value, region_value, geometry in res.itertuples(
        index=False, name=None
    ):
        pra_id = int(pra_value)
        result_id = str(result_value)
        region = str(region_value)
        geometry_json = geometry.__geo_interface__
        for column in config.path_columns:
            source = source_caches[region][result_id][column]
            output = _expected_output(
                config.clip_output_root, pra_id, result_id, source
            )
            yield source, output, geometry_json, gdf.crs


def _clip_raster_atomic(
    source: Path,
    output: Path,
    geometry_json: dict,
    geometry_crs,
) -> tuple[bool, str | None]:
    temporary = output.with_suffix(output.suffix + ".tmp")
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        if temporary.exists():
            temporary.unlink()
        with rasterio.Env(GDAL_CACHEMAX=512):
            with rasterio.open(source) as src:
                clip_geometry = geometry_json
                if src.crs is None:
                    raise ValueError(f"Source raster has no CRS: {source}")
                if geometry_crs is not None and src.crs != geometry_crs:
                    clip_geometry = transform_geom(
                        geometry_crs, src.crs, geometry_json
                    )
                clipped, transform = raster_mask(
                    src,
                    [clip_geometry],
                    crop=True,
                )
                metadata = src.meta.copy()
                metadata.update(
                    driver="GTiff",
                    height=clipped.shape[1],
                    width=clipped.shape[2],
                    transform=transform,
                    compress="LZW",
                    BIGTIFF="IF_SAFER",
                )
                with rasterio.open(temporary, "w", **metadata) as dst:
                    dst.write(clipped)
        os.replace(temporary, output)
        return True, None
    except Exception as error:  # Report failures after the bounded job queue drains.
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass
        return False, f"{source} -> {output}: {error}"


def _collect_future(
    future: Future,
    pending: dict[Future, Path],
    stats: Counter,
    failures: list[str],
) -> None:
    output = pending.pop(future)
    try:
        ok, error = future.result()
    except Exception as unexpected:
        ok, error = False, f"{output}: unexpected worker error: {unexpected}"
    stats["completed"] += 1
    if ok:
        stats["created"] += 1
    else:
        stats["failed"] += 1
        if len(failures) < 50:
            failures.append(error or f"Unknown failure for {output}")


def _run_clipping(
    gdf: gpd.GeoDataFrame,
    config: WorkflowConfig,
    source_caches: dict[str, dict[str, dict[str, Path]]],
) -> Counter:
    total_jobs = int(gdf["modType"].eq("res").sum()) * len(config.path_columns)
    stats = Counter(total=total_jobs)
    failures: list[str] = []
    pending: dict[Future, Path] = {}
    started = time.perf_counter()
    last_log = started

    config.clip_output_root.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=config.max_workers) as executor:
        for source, output, geometry_json, geometry_crs in _iter_clip_jobs(
            gdf, config, source_caches
        ):
            if output.is_file() and not config.overwrite_clips:
                stats["existing"] += 1
                continue
            future = executor.submit(
                _clip_raster_atomic,
                source,
                output,
                geometry_json,
                geometry_crs,
            )
            pending[future] = output
            stats["submitted"] += 1

            if len(pending) >= config.max_pending_jobs:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for completed in done:
                    _collect_future(completed, pending, stats, failures)
                if failures:
                    # Stop feeding a million-file run after the first concrete
                    # error (for example a full disk or non-overlapping raster).
                    # Already completed TIFFs remain usable for a later resume.
                    break

            now = time.perf_counter()
            if now - last_log >= config.log_every_seconds:
                processed = stats["existing"] + stats["completed"]
                rate = processed / max(now - started, 1e-9)
                log.info(
                    "Clip progress %d/%d | created=%d existing=%d failed=%d | %.1f files/s",
                    processed,
                    total_jobs,
                    stats["created"],
                    stats["existing"],
                    stats["failed"],
                    rate,
                )
                last_log = now

        if failures:
            for future in pending:
                future.cancel()
        else:
            while pending:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for completed in done:
                    _collect_future(completed, pending, stats, failures)
                if failures:
                    for future in pending:
                        future.cancel()
                    break

    if failures:
        for failure in failures[:10]:
            log.error("Clip failed: %s", failure)
        raise RuntimeError(
            f"{stats['failed']:,} raster clips failed. The final parquet was not "
            "written. Existing successful TIFFs will be reused on the next run."
        )
    return stats


def _count_existing_outputs(
    gdf: gpd.GeoDataFrame,
    config: WorkflowConfig,
    source_caches: dict[str, dict[str, dict[str, Path]]],
) -> tuple[int, int]:
    total = int(gdf["modType"].eq("res").sum()) * len(config.path_columns)
    if not config.clip_output_root.is_dir():
        return 0, total
    try:
        with os.scandir(config.clip_output_root) as entries:
            if next(entries, None) is None:
                return 0, total
    except OSError:
        pass
    existing = 0
    for _, output, _, _ in _iter_clip_jobs(gdf, config, source_caches):
        if output.is_file():
            existing += 1
    return existing, total - existing


def _validate_final_paths(
    gdf: gpd.GeoDataFrame,
    config: WorkflowConfig,
) -> None:
    clip_root = config.clip_output_root.resolve()
    rel = gdf.loc[gdf["modType"].eq("rel")]
    if rel[list(PATH_COLUMNS)].notna().any().any():
        raise ValueError("Internal error: rel rows contain raster paths.")

    missing: list[str] = []
    outside: list[str] = []
    res = gdf.loc[gdf["modType"].eq("res")]
    for column in config.path_columns:
        if res[column].isna().any():
            raise ValueError(f"Internal error: empty res paths in {column}.")
        for value in res[column].astype(str):
            path = _resolve_stored_path(value, config)
            if not path.is_relative_to(clip_root):
                if len(outside) < 10:
                    outside.append(str(path))
            elif not path.is_file() and len(missing) < 10:
                missing.append(str(path))
    if outside:
        raise ValueError(
            f"Output contains paths outside clipOutputRoot: {outside}"
        )
    if missing:
        raise FileNotFoundError(
            f"Clipped output files are missing; sample: {missing}"
        )


def _write_parquet_atomic(
    gdf: gpd.GeoDataFrame,
    output: Path,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.stem}_", suffix=".parquet", dir=output.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        gdf.to_parquet(temporary, index=False)
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Select LKGebiete from EUREGIO, clip all configured rasters with "
            "res geometries, and write a clipped-path AvaDirectory parquet."
        )
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help=(
            f"Main or local workflow INI (default: {DEFAULT_CONFIG.name}); "
            "a matching local_ INI is overlaid automatically."
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Read-only preflight: validate sources and report planned clips.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config(args.config)
    _validate_input_paths(config, args.check)

    log.info("Configuration: %s", args.config.expanduser().resolve())
    log.info("Input EUREGIO: %s", config.input_parquet)
    log.info("Output parquet: %s", config.output_parquet)
    log.info("Clipped TIFF root: %s", config.clip_output_root)
    log.info("LKGebietID: %s", sorted(config.lkgebiet_ids))
    log.info("Raster path columns: %s", ", ".join(config.path_columns))
    log.info("Geometry used for clipping: modType=res")

    gdf = _load_selected_rows(config)
    res = gdf.loc[gdf["modType"].eq("res")]
    source_caches = _build_source_caches(config, res)
    existing, remaining = _count_existing_outputs(gdf, config, source_caches)
    total = existing + remaining
    log.info(
        "Preflight complete: requested clipped TIFFs=%d, existing=%d, remaining=%d",
        total,
        existing,
        remaining,
    )

    if args.check:
        log.info("CHECK ONLY: no directories, TIFFs, or parquet files were written.")
        return 0

    _assign_expected_paths(gdf, config, source_caches)
    stats = _run_clipping(gdf, config, source_caches)
    _validate_final_paths(gdf, config)
    _write_parquet_atomic(gdf, config.output_parquet)
    log.info(
        "Finished: created=%d, reused=%d, output rows=%d",
        stats["created"],
        stats["existing"],
        len(gdf),
    )
    log.info("Wrote clipped-path GeoParquet: %s", config.output_parquet)
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    raise SystemExit(main())
