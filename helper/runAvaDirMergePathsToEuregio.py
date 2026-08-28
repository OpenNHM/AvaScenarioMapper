#!/usr/bin/env python3
"""Merge raster path columns from pilot AvaDirectory tables into EUREGIO.

The large EUREGIO GeoParquet is processed one row group at a time. Existing
path values are preserved; pilot values only fill empty cells. Run with
``--dry-run`` first to report the number of cells that would be filled.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Sequence

import geopandas as gpd
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq


DEFAULT_AVA_DIRECTORY_ROOT = Path(
    "/media/christoph/SSD 500 GB/Cairos/ModelChainResults/Euregio/"
    "cairosAvaMaps/12_avaDirectory"
)
DEFAULT_EUREGIO_FILE = (
    DEFAULT_AVA_DIRECTORY_ROOT
    / "EUREGIO/avaDirectoryResults_EUREGIO.parquet"
)
DEFAULT_PILOT_FILES = [
    DEFAULT_AVA_DIRECTORY_ROOT
    / "pilotSella/avaDirectoryResults_pilotSella.parquet",
    DEFAULT_AVA_DIRECTORY_ROOT
    / "pilotBrenner/avaDirectoryResults_pilotBrenner.parquet",
]
DEFAULT_OUT_FILE = (
    DEFAULT_AVA_DIRECTORY_ROOT
    / "EUREGIO/avaDirectoryResults_EUREGIOxPilotRegions.parquet"
)

KEY_COLS = ["praID", "resultID", "modType"]
PATH_COLS = [
    "pathCellcounts",
    "pathInputpra",
    "pathTravelanglemax",
    "pathTravelanglemax_sized",
    "pathTravellengthmax",
    "pathTravellengthmax_sized",
    "pathZdelta",
    "pathZdelta_sized",
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Fill empty EUREGIO raster-path cells from one or more pilot "
            "AvaDirectory GeoParquet files."
        )
    )
    parser.add_argument(
        "--euregio-file",
        type=Path,
        default=DEFAULT_EUREGIO_FILE,
        help=f"Base EUREGIO GeoParquet (default: {DEFAULT_EUREGIO_FILE})",
    )
    parser.add_argument(
        "--pilot-file",
        type=Path,
        action="append",
        dest="pilot_files",
        help=(
            "Pilot GeoParquet containing raster paths; repeat for multiple "
            "pilots. Defaults to the current Sella and Brenner files."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUT_FILE,
        help=f"Output GeoParquet (default: {DEFAULT_OUT_FILE})",
    )
    parser.add_argument(
        "--checkpoint-dir",
        type=Path,
        help="Optionally retain one merged checkpoint GeoParquet per row group.",
    )
    parser.add_argument(
        "--keep-temp-parts",
        action="store_true",
        help="Keep temporary row-group parts after a successful run.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing output file.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Calculate matches and fills without writing files.",
    )
    return parser


def print_title(text: str) -> None:
    print("\n" + "=" * 90)
    print(text)
    print("=" * 90)


def _require_columns(columns: Sequence[str], required: Sequence[str], source: Path) -> None:
    missing = [column for column in required if column not in columns]
    if missing:
        raise ValueError(f"Missing columns in {source}: {missing}")


def norm(value):
    if pd.isna(value):
        return pd.NA
    text = str(value).strip()
    if not text or text.lower() in {"none", "nan", "<na>"}:
        return pd.NA
    return text


def normalize_path_cols(df: pd.DataFrame) -> pd.DataFrame:
    for column in PATH_COLS:
        if column not in df.columns:
            df[column] = pd.Series(pd.NA, index=df.index, dtype="string")
        else:
            values = df[column].astype("string").str.strip()
            invalid = values.isna() | values.str.lower().isin({"", "none", "nan", "<na>"})
            df[column] = values.mask(invalid, pd.NA)
    return df


def resolve_paths(df: pd.DataFrame, parquet_path: Path) -> pd.DataFrame:
    """Store pilot raster paths as absolute paths accepted by the new mapper."""
    base_dir = parquet_path.parent
    for column in PATH_COLS:
        df[column] = df[column].map(
            lambda value: (
                pd.NA
                if pd.isna(value)
                else str(
                    Path(str(value))
                    if Path(str(value)).is_absolute()
                    else (base_dir / str(value)).resolve()
                )
            )
        ).astype("string")
    return df


def check_key_uniqueness(df: pd.DataFrame, name: str) -> None:
    unique_count = df[KEY_COLS].drop_duplicates().shape[0]
    duplicate_count = len(df) - unique_count
    print(
        f"{name}: rows={len(df):,}, unique keys={unique_count:,}, "
        f"duplicate key rows={duplicate_count:,}"
    )
    if duplicate_count:
        raise ValueError(f"{name} has duplicate keys: {KEY_COLS}")


def _read_geo_metadata(parquet_path: Path) -> tuple[str, dict, object]:
    schema = pq.ParquetFile(parquet_path).schema_arrow
    metadata = schema.metadata or {}
    if b"geo" not in metadata:
        raise ValueError(f"Missing GeoParquet metadata in {parquet_path}")
    geo = json.loads(metadata[b"geo"].decode("utf-8"))
    geometry_column = geo.get("primary_column", "geometry")
    geometry_metadata = geo.get("columns", {}).get(geometry_column, {})
    return geometry_column, geometry_metadata, geometry_metadata.get("crs")


def read_geo_row_group(parquet_path: Path, row_group_idx: int) -> gpd.GeoDataFrame:
    table = pq.ParquetFile(parquet_path).read_row_group(row_group_idx)
    geometry_column, geometry_metadata, crs = _read_geo_metadata(parquet_path)
    encoding = str(geometry_metadata.get("encoding", "")).lower()
    df = table.to_pandas()

    if geometry_column not in df.columns:
        raise ValueError(
            f"Geometry column '{geometry_column}' not found in row group {row_group_idx}"
        )
    if encoding == "wkb":
        geometry = gpd.GeoSeries.from_wkb(df[geometry_column], crs=None)
    elif encoding == "wkt":
        geometry = gpd.GeoSeries.from_wkt(df[geometry_column], crs=None)
    else:
        raise ValueError(
            f"Unsupported geometry encoding '{encoding}' in {parquet_path}; "
            "expected WKB or WKT."
        )

    df = df.drop(columns=[geometry_column])
    gdf = gpd.GeoDataFrame(df, geometry=geometry)
    if crs:
        gdf.set_crs(crs, inplace=True, allow_override=True)
    return gdf


def load_pilot_minimal(pilot_file: Path) -> pd.DataFrame:
    if not pilot_file.is_file():
        raise FileNotFoundError(pilot_file)

    columns = pq.ParquetFile(pilot_file).schema_arrow.names
    _require_columns(columns, KEY_COLS, pilot_file)
    available_paths = [column for column in PATH_COLS if column in columns]
    pilot = pd.read_parquet(pilot_file, columns=KEY_COLS + available_paths)
    pilot = normalize_path_cols(pilot)
    pilot = resolve_paths(pilot, pilot_file)
    check_key_uniqueness(pilot, pilot_file.stem)

    pilot = pilot[pilot["modType"].astype("string").str.strip().str.lower() == "res"]
    return pilot[KEY_COLS + PATH_COLS].set_index(KEY_COLS)


def merge_paths_into_base_chunk(
    base_chunk: gpd.GeoDataFrame,
    pilot_idx: pd.DataFrame,
    pilot_name: str,
) -> tuple[gpd.GeoDataFrame, dict]:
    base_chunk = normalize_path_cols(base_chunk)
    base_res_mask = (
        base_chunk["modType"].astype("string").str.strip().str.lower() == "res"
    )
    stats = {
        "matching_rows": 0,
        "enriched_rows": 0,
        "filled_cells": {column: 0 for column in PATH_COLS},
    }
    if not base_res_mask.any():
        return base_chunk, stats

    base_res = base_chunk.loc[base_res_mask, KEY_COLS + PATH_COLS].copy()
    base_res_idx = base_res.set_index(KEY_COLS)
    common_idx = base_res_idx.index.intersection(pilot_idx.index)
    stats["matching_rows"] = len(common_idx)
    if not len(common_idx):
        return base_chunk, stats

    updated_rows = pd.Series(False, index=base_res_idx.index)
    for column in PATH_COLS:
        base_values = base_res_idx.loc[common_idx, column]
        pilot_values = pilot_idx.loc[common_idx, column]
        fill_mask = base_values.isna() & pilot_values.notna()
        fill_count = int(fill_mask.sum())
        stats["filled_cells"][column] = fill_count
        if fill_count:
            fill_idx = common_idx[fill_mask.to_numpy()]
            base_res_idx.loc[fill_idx, column] = pilot_idx.loc[fill_idx, column]
            updated_rows.loc[fill_idx] = True

    stats["enriched_rows"] = int(updated_rows.sum())
    updated_res = base_res_idx.reset_index()
    base_chunk.loc[base_res_mask, PATH_COLS] = updated_res[PATH_COLS].to_numpy()
    print(
        f"{pilot_name}: matching={stats['matching_rows']:,}, "
        f"enriched={stats['enriched_rows']:,}"
    )
    for column, count in stats["filled_cells"].items():
        if count:
            print(f"  {column:28s}: {count:,}")
    return base_chunk, stats


def write_geo_chunk(gdf: gpd.GeoDataFrame, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_parquet(out_path, index=False)


def combine_geo_parquet_parts(
    part_files: Sequence[Path],
    out_file: Path,
    geo_metadata: bytes | None = None,
) -> None:
    if not part_files:
        raise ValueError("No part files to combine.")

    out_file.parent.mkdir(parents=True, exist_ok=True)
    file_handle, temporary_name = tempfile.mkstemp(
        prefix=f".{out_file.stem}_", suffix=".parquet", dir=out_file.parent
    )
    os.close(file_handle)
    temporary_output = Path(temporary_name)
    temporary_output.unlink()

    writer = None
    output_schema = None
    try:
        for index, part in enumerate(part_files, start=1):
            table = pq.read_table(part)
            if writer is None:
                output_schema = table.schema
                if geo_metadata is not None:
                    metadata = dict(output_schema.metadata or {})
                    metadata[b"geo"] = geo_metadata
                    output_schema = output_schema.with_metadata(metadata)
                    table = table.replace_schema_metadata(output_schema.metadata)
                writer = pq.ParquetWriter(
                    temporary_output,
                    output_schema,
                    compression="snappy",
                )
            else:
                if not table.schema.equals(output_schema, check_metadata=False):
                    # A selected row group may contain only null values in a
                    # normally string-typed descriptive column. GeoPandas then
                    # writes that part as Arrow ``null``. Cast it back to the
                    # stable schema established by the first non-empty part.
                    table = table.cast(output_schema)
                table = table.replace_schema_metadata(output_schema.metadata)
            writer.write_table(table)
            print(f"appended part {index:02d}/{len(part_files):02d}: {part.name}")
        writer.close()
        writer = None
        os.replace(temporary_output, out_file)
    finally:
        if writer is not None:
            writer.close()
        if temporary_output.exists():
            temporary_output.unlink()


def _empty_totals(pilot_tables: Sequence[tuple[str, pd.DataFrame]]) -> dict:
    return {
        name: {
            "matching_rows": 0,
            "enriched_rows": 0,
            "filled_cells": {column: 0 for column in PATH_COLS},
        }
        for name, _ in pilot_tables
    }


def _add_stats(total: dict, update: dict) -> None:
    total["matching_rows"] += update["matching_rows"]
    total["enriched_rows"] += update["enriched_rows"]
    for column in PATH_COLS:
        total["filled_cells"][column] += update["filled_cells"][column]


def audit_output(parquet_path: Path) -> dict:
    parquet_file = pq.ParquetFile(parquet_path)
    columns = parquet_file.schema_arrow.names
    _require_columns(columns, ["modType"] + PATH_COLS, parquet_path)
    result = {"rows": parquet_file.metadata.num_rows, "res_with_paths": 0, "rel_with_paths": 0}

    for batch in parquet_file.iter_batches(columns=["modType"] + PATH_COLS):
        mod_type = pc.utf8_lower(pc.utf8_trim_whitespace(batch.column(0)))
        any_path = pa.array([False] * len(batch))
        for column_index in range(1, len(PATH_COLS) + 1):
            values = batch.column(column_index)
            if pa.types.is_null(values.type):
                present = pa.array([False] * len(batch))
            else:
                # Arrow's regular boolean kernels propagate nulls. Explicitly
                # turn null path comparisons into False so a later empty
                # column cannot erase a True found in an earlier column.
                present = pc.fill_null(pc.not_equal(values, ""), False)
            any_path = pc.or_(any_path, present)
        for mode, result_key in (("res", "res_with_paths"), ("rel", "rel_with_paths")):
            matching = pc.and_(pc.equal(mod_type, mode), any_path)
            result[result_key] += pc.sum(pc.cast(matching, pa.int64())).as_py() or 0
    return result


def _validate_args(args: argparse.Namespace) -> list[Path]:
    args.euregio_file = args.euregio_file.expanduser().resolve()
    args.output = args.output.expanduser().resolve()
    configured_pilots = args.pilot_files or DEFAULT_PILOT_FILES
    pilot_files = [path.expanduser().resolve() for path in configured_pilots]

    if not args.euregio_file.is_file():
        raise FileNotFoundError(args.euregio_file)
    for pilot_file in pilot_files:
        if not pilot_file.is_file():
            raise FileNotFoundError(pilot_file)
    if args.output == args.euregio_file:
        raise ValueError("Output must differ from the EUREGIO input file.")
    if not args.dry_run and args.output.exists() and not args.overwrite:
        raise FileExistsError(f"Output exists; pass --overwrite to replace it: {args.output}")
    return pilot_files


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    pilot_files = _validate_args(args)

    print_title("LOAD PILOT TABLES")
    pilot_tables = []
    for pilot_file in pilot_files:
        print(f"loading pilot minimal: {pilot_file}")
        pilot_idx = load_pilot_minimal(pilot_file)
        pilot_tables.append((pilot_file.stem, pilot_idx))
        print(f"pilot loaded: {pilot_file.stem}, res rows={len(pilot_idx):,}")

    parquet_file = pq.ParquetFile(args.euregio_file)
    _require_columns(parquet_file.schema_arrow.names, KEY_COLS, args.euregio_file)
    print_title("EUREGIO INPUT")
    print(f"file       : {args.euregio_file}")
    print(f"row groups : {parquet_file.num_row_groups}")
    print(f"rows total : {parquet_file.metadata.num_rows:,}")
    print(f"mode       : {'dry run' if args.dry_run else 'write'}")

    part_root = None
    part_files = []
    totals = _empty_totals(pilot_tables)
    if not args.dry_run:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        part_root = Path(
            tempfile.mkdtemp(prefix=f".{args.output.stem}_parts_", dir=args.output.parent)
        )
    if args.checkpoint_dir:
        args.checkpoint_dir = args.checkpoint_dir.expanduser().resolve()
        args.checkpoint_dir.mkdir(parents=True, exist_ok=True)

    try:
        for row_group_index in range(parquet_file.num_row_groups):
            print_title(
                f"EUREGIO ROW GROUP {row_group_index + 1}/{parquet_file.num_row_groups}"
            )
            base_chunk = read_geo_row_group(args.euregio_file, row_group_index)
            _require_columns(base_chunk.columns, KEY_COLS, args.euregio_file)
            base_chunk = normalize_path_cols(base_chunk)
            check_key_uniqueness(base_chunk, f"EUREGIO row group {row_group_index + 1}")

            for pilot_name, pilot_idx in pilot_tables:
                base_chunk, stats = merge_paths_into_base_chunk(
                    base_chunk, pilot_idx, pilot_name
                )
                _add_stats(totals[pilot_name], stats)

            if not args.dry_run:
                part_path = part_root / f"part-{row_group_index:05d}.parquet"
                write_geo_chunk(base_chunk, part_path)
                part_files.append(part_path)
                print(f"temporary part: {part_path}")
                if args.checkpoint_dir:
                    checkpoint = (
                        args.checkpoint_dir
                        / f"checkpoint-rowgroup-{row_group_index:05d}.parquet"
                    )
                    if checkpoint.exists() and not args.overwrite:
                        raise FileExistsError(
                            f"Checkpoint exists; pass --overwrite: {checkpoint}"
                        )
                    write_geo_chunk(base_chunk, checkpoint)
                    print(f"checkpoint: {checkpoint}")
            del base_chunk
            gc.collect()

        print_title("MERGE SUMMARY")
        for pilot_name, stats in totals.items():
            print(
                f"{pilot_name}: matching={stats['matching_rows']:,}, "
                f"enriched={stats['enriched_rows']:,}"
            )
            for column, count in stats["filled_cells"].items():
                print(f"  {column:28s}: {count:,}")

        if args.dry_run:
            print("Dry run complete; no files written.")
            return 0

        print_title("COMBINE PARTS")
        input_metadata = parquet_file.schema_arrow.metadata or {}
        combine_geo_parquet_parts(
            part_files,
            args.output,
            geo_metadata=input_metadata.get(b"geo"),
        )
        print(f"written: {args.output}")
        audit = audit_output(args.output)
        print_title("FINAL AUDIT")
        print(f"rows total                 : {audit['rows']:,}")
        print(f"res rows with any tif path : {audit['res_with_paths']:,}")
        print(f"rel rows with any tif path : {audit['rel_with_paths']:,}")
        return 0
    finally:
        if part_root and part_root.exists() and not args.keep_temp_parts:
            shutil.rmtree(part_root)
            print(f"removed temporary parts: {part_root}")


if __name__ == "__main__":
    raise SystemExit(main())
