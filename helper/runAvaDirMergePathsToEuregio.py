#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from pathlib import Path
import json
import gc
import shutil

import pandas as pd
import geopandas as gpd
import pyarrow.parquet as pq


# =============================================================================
# CONFIG
# =============================================================================

EUREGIO_FILE = Path(
    "/media/christoph/SSD 500 GB/Cairos/ModelChainResults/Euregio/cairosAvaMaps/12_avaDirectory/EUREGIO/avaDirectoryResults_EUREGIO.parquet"
)

PILOT_FILES = [
    Path("/media/christoph/SSD 500 GB/Cairos/ModelChainResults/Euregio/cairosAvaMaps/12_avaDirectory/pilotSella/avaDirectoryResults_pilotSella_clipped_withRel.parquet"),
    Path("/media/christoph/SSD 500 GB/Cairos/ModelChainResults/Euregio/cairosAvaMaps/12_avaDirectory/pilotBrenner/avaDirectoryResults_pilotBrenner_clipped_withRel.parquet"),
]

OUT_FILE = Path(
    "/media/christoph/SSD 500 GB/Cairos/ModelChainResults/Euregio/cairosAvaMaps/12_avaDirectory/EUREGIO/avaDirectoryResults_EUREGIOxPilotRegions.parquet"
)

WRITE_CHECKPOINTS = True
CHECKPOINT_DIR = OUT_FILE.parent / "_mergeCheckpoints"
TEMP_PART_DIR = OUT_FILE.parent / "_mergeTempParts"
KEEP_TEMP_PARTS = False

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


# =============================================================================
# HELPERS
# =============================================================================

def print_title(txt):
    print("\n" + "=" * 90)
    print(txt)
    print("=" * 90)


def norm(x):
    if pd.isna(x):
        return pd.NA
    s = str(x).strip()
    if s == "" or s.lower() in {"none", "nan", "<na>"}:
        return pd.NA
    return s


def normalize_path_cols(df):
    for col in PATH_COLS:
        if col not in df.columns:
            df[col] = pd.NA
        df[col] = df[col].map(norm)
    return df


def resolve_paths(df, parquet_path):
    base_dir = parquet_path.parent
    for col in PATH_COLS:
        def _resolve(x):
            if pd.isna(x):
                return pd.NA
            p = Path(str(x))
            return str(p if p.is_absolute() else (base_dir / p).resolve())
        df[col] = df[col].map(_resolve)
    return df


def check_key_uniqueness(df, name):
    n_unique = df[KEY_COLS].drop_duplicates().shape[0]
    n_dup = len(df) - n_unique
    print(f"{name}: rows={len(df):,}, unique keys={n_unique:,}, duplicate key rows={n_dup:,}")
    if n_dup > 0:
        raise ValueError(f"{name} has duplicate keys: {KEY_COLS}")


def read_geo_row_group(parquet_path: Path, row_group_idx: int) -> gpd.GeoDataFrame:
    """
    Read one GeoParquet row group as GeoDataFrame.
    """
    pf = pq.ParquetFile(parquet_path)
    table = pf.read_row_group(row_group_idx)

    metadata = table.schema.metadata or {}
    if b"geo" not in metadata:
        raise ValueError(f"Missing GeoParquet metadata in {parquet_path}")

    geo = json.loads(metadata[b"geo"].decode("utf-8"))
    geom_col = geo.get("primary_column", "geometry")
    geom_meta = geo.get("columns", {}).get(geom_col, {})
    encoding = str(geom_meta.get("encoding", "")).lower()

    df = table.to_pandas()

    if geom_col not in df.columns:
        raise ValueError(f"Geometry column '{geom_col}' not found in row group {row_group_idx}")

    if encoding == "wkb":
        geometry = gpd.GeoSeries.from_wkb(df[geom_col], crs=None)
    elif encoding == "wkt":
        geometry = gpd.GeoSeries.from_wkt(df[geom_col], crs=None)
    else:
        raise ValueError(
            f"Unsupported geometry encoding '{encoding}' in {parquet_path}. "
            f"Expected 'WKB' or 'WKT'."
        )

    df = df.drop(columns=[geom_col])
    gdf = gpd.GeoDataFrame(df, geometry=geometry)

    crs = geom_meta.get("crs")
    if crs:
        gdf.set_crs(crs, inplace=True, allow_override=True)

    return gdf


def load_pilot_minimal(pilot_file):
    pilot = pd.read_parquet(pilot_file, columns=KEY_COLS + PATH_COLS)
    pilot = normalize_path_cols(pilot)
    pilot = resolve_paths(pilot, pilot_file)
    check_key_uniqueness(pilot, pilot_file.stem)

    pilot = pilot[pilot["modType"] == "res"].copy()
    pilot = pilot[KEY_COLS + PATH_COLS].copy()
    pilot_idx = pilot.set_index(KEY_COLS)

    del pilot
    gc.collect()

    return pilot_idx


def merge_paths_into_base_chunk(base_chunk, pilot_idx, pilot_name):
    """
    Enrich one base chunk with one pilot table.
    """
    print_title(f"MERGING CHUNK WITH {pilot_name}")

    base_chunk = normalize_path_cols(base_chunk)

    base_res_mask = base_chunk["modType"] == "res"
    if not base_res_mask.any():
        print("chunk has no res rows -> nothing to merge")
        return base_chunk

    base_res = base_chunk.loc[base_res_mask, KEY_COLS + PATH_COLS].copy()
    base_res_idx = base_res.set_index(KEY_COLS)

    common_idx = base_res_idx.index.intersection(pilot_idx.index)
    print(f"matching res rows: {len(common_idx):,}")

    if len(common_idx) == 0:
        del base_res
        del base_res_idx
        gc.collect()
        return base_chunk

    updated_rows = pd.Series(False, index=base_res_idx.index)
    col_fills = {}

    for col in PATH_COLS:
        base_vals = base_res_idx.loc[common_idx, col]
        pilot_vals = pilot_idx.loc[common_idx, col]

        fill_mask = base_vals.isna() & pilot_vals.notna()
        col_fills[col] = int(fill_mask.sum())

        if fill_mask.any():
            fill_idx = common_idx[fill_mask.to_numpy()]
            base_res_idx.loc[fill_idx, col] = pilot_idx.loc[fill_idx, col]
            updated_rows.loc[fill_idx] = True

    n_updated_rows = int(updated_rows.sum())
    print(f"rows enriched: {n_updated_rows:,}")
    for col, n in col_fills.items():
        print(f"  {col:28s}: {n:,}")

    updated_res = base_res_idx.reset_index()
    base_chunk.loc[base_res_mask, PATH_COLS] = updated_res[PATH_COLS].to_numpy()

    del base_res
    del base_res_idx
    del updated_res
    del updated_rows
    gc.collect()

    return base_chunk


def write_geo_chunk(gdf, out_path):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_parquet(out_path, index=False)


def combine_geo_parquet_parts(part_files, out_file):
    """
    Combine multiple GeoParquet chunk files into one single GeoParquet file.
    Keeps schema + geo metadata from first part.
    """
    if not part_files:
        raise ValueError("No part files to combine.")

    out_file.parent.mkdir(parents=True, exist_ok=True)
    if out_file.exists():
        out_file.unlink()

    writer = None
    try:
        for i, part in enumerate(part_files, start=1):
            table = pq.read_table(part)

            if writer is None:
                writer = pq.ParquetWriter(
                    where=out_file,
                    schema=table.schema,
                    compression="snappy",
                )

            writer.write_table(table)
            print(f"appended part {i:02d}/{len(part_files):02d}: {part.name}")

    finally:
        if writer is not None:
            writer.close()


# =============================================================================
# RUNNER
# =============================================================================

def main():
    print_title("LOAD PILOT TABLES")
    pilot_tables = []
    for pilot_file in PILOT_FILES:
        print(f"loading pilot minimal: {pilot_file}")
        pilot_idx = load_pilot_minimal(pilot_file)
        pilot_tables.append((pilot_file.stem, pilot_idx))
        print(f"pilot loaded: {pilot_file.stem}, rows={len(pilot_idx):,}")

    pf = pq.ParquetFile(EUREGIO_FILE)
    num_row_groups = pf.num_row_groups
    total_rows = pf.metadata.num_rows

    print_title("EUREGIO INPUT")
    print(f"file       : {EUREGIO_FILE}")
    print(f"row groups : {num_row_groups}")
    print(f"rows total : {total_rows:,}")

    if WRITE_CHECKPOINTS:
        CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)

    TEMP_PART_DIR.mkdir(parents=True, exist_ok=True)

    # clean temp dir for stable reruns
    for old in TEMP_PART_DIR.glob("part-*.parquet"):
        old.unlink()

    part_files = []

    for rg_idx in range(num_row_groups):
        print_title(f"READ EUREGIO ROW GROUP {rg_idx + 1}/{num_row_groups}")

        base_chunk = read_geo_row_group(EUREGIO_FILE, rg_idx)
        base_chunk = normalize_path_cols(base_chunk)

        print(f"chunk rows: {len(base_chunk):,}")
        print(f"chunk cols: {len(base_chunk.columns):,}")

        # optional chunk-level uniqueness check
        check_key_uniqueness(base_chunk, f"EUREGIO_rowgroup_{rg_idx + 1}")

        for pilot_name, pilot_idx in pilot_tables:
            base_chunk = merge_paths_into_base_chunk(base_chunk, pilot_idx, pilot_name)

        part_path = TEMP_PART_DIR / f"part-{rg_idx:05d}.parquet"
        write_geo_chunk(base_chunk, part_path)
        part_files.append(part_path)
        print(f"temp part written: {part_path}")

        if WRITE_CHECKPOINTS:
            cp = CHECKPOINT_DIR / f"checkpoint_rowgroup_{rg_idx + 1:02d}.parquet"
            write_geo_chunk(base_chunk, cp)
            print(f"checkpoint written: {cp}")

        del base_chunk
        gc.collect()

    print_title("COMBINE TEMP PARTS TO FINAL GEOPARQUET")
    combine_geo_parquet_parts(part_files, OUT_FILE)
    print(f"written: {OUT_FILE}")

    print_title("FINAL SUMMARY")
    final_gdf = gpd.read_parquet(OUT_FILE)
    final_gdf = normalize_path_cols(final_gdf)

    res_mask = final_gdf["modType"] == "res"
    rel_mask = final_gdf["modType"] == "rel"

    print(f"rows total                 : {len(final_gdf):,}")
    print(f"res rows with any tif path : {int(final_gdf.loc[res_mask, PATH_COLS].notna().any(axis=1).sum()):,}")
    print(f"rel rows with any tif path : {int(final_gdf.loc[rel_mask, PATH_COLS].notna().any(axis=1).sum()):,}")
    print(f"geometry col               : {final_gdf.geometry.name}")
    print(f"crs                        : {final_gdf.crs}")

    del final_gdf
    gc.collect()

    if not KEEP_TEMP_PARTS:
        shutil.rmtree(TEMP_PART_DIR, ignore_errors=True)
        print(f"removed temp dir: {TEMP_PART_DIR}")

    print_title("DONE")


if __name__ == "__main__":
    main()