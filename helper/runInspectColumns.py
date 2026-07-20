#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from pathlib import Path
import subprocess
import shutil
import sqlite3
import pandas as pd

try:
    import pyarrow.parquet as pq
except ImportError:
    pq = None

try:
    import fiona
except ImportError:
    fiona = None

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None


# -----------------------------------------------------------
# SETTINGS
# -----------------------------------------------------------

BASE_DIR = Path(
    "/media/christoph/SSD 500 GB/Cairos/ModelChainResults/Euregio/cairosAvaMaps/13_avaScenMaps/EUREGIO/"
)

FILE_PREFIX = "ava"
ALLOWED_SUFFIXES = {".csv", ".parquet", ".gpkg", ".geojson"}

# False = inspect only
# True  = remove selected columns and rewrite
DELETE_COLUMNS = False

# False = write *_cleaned beside original
# True  = write temp file and replace original after success
OVERWRITE_FILES = True

COLUMNS_TO_DELETE = [
    "sourceRegion",
    "sourceFile",
    "scenario",
]

CSV_CHUNKSIZE = 200_000


# -----------------------------------------------------------
# HELPERS
# -----------------------------------------------------------

def progress(iterable=None, total=None, desc="Progress", unit="it"):
    if tqdm is None:
        return iterable
    return tqdm(iterable=iterable, total=total, desc=desc, unit=unit)


def run_cmd(cmd):
    print("\nRUN:", " ".join(str(c) for c in cmd))
    subprocess.run(cmd, check=True)


def quote_sql_name(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def print_free_space(path: Path):
    usage = shutil.disk_usage(path)
    free_gb = usage.free / (1024 ** 3)
    total_gb = usage.total / (1024 ** 3)
    used_gb = usage.used / (1024 ** 3)
    print(f"Disk usage at {path}")
    print(f"  Total: {total_gb:.1f} GB")
    print(f"  Used : {used_gb:.1f} GB")
    print(f"  Free : {free_gb:.1f} GB")


def is_cleaned_file(path: Path) -> bool:
    return path.stem.endswith("_cleaned")


def final_output_path(path: Path) -> Path:
    if OVERWRITE_FILES:
        return path
    return path.with_name(f"{path.stem}_cleaned{path.suffix}")


def temp_output_path(path: Path) -> Path:
    # temp file on same filesystem so replace is safer/faster
    return path.with_name(f"{path.stem}.__tmp__{path.suffix}")


def safe_replace(temp_path: Path, final_path: Path):
    if final_path.exists():
        final_path.unlink()
    temp_path.replace(final_path)


def cleanup_temp_file(path: Path):
    try:
        if path.exists():
            path.unlink()
    except Exception:
        pass


# -----------------------------------------------------------
# FILE SEARCH
# -----------------------------------------------------------

def find_matching_files(base_dir: Path):
    files = []

    for suffix in ALLOWED_SUFFIXES:
        pattern = f"{FILE_PREFIX}*{suffix}"
        for p in base_dir.glob(pattern):
            if p.is_file() and not is_cleaned_file(p):
                files.append(p)

    return sorted(set(files))


# -----------------------------------------------------------
# INSPECT
# -----------------------------------------------------------

def inspect_csv(path: Path):
    cols = pd.read_csv(path, nrows=0).columns.tolist()

    print("\n" + "=" * 90)
    print(f"CSV : {path}")
    print(f"COLUMNS : {len(cols)}")
    for i, col in enumerate(cols, 1):
        print(f"{i:>3} | {col}")
    print("=" * 90)


def inspect_parquet(path: Path):
    if pq is None:
        raise RuntimeError("pyarrow is required for parquet inspection")

    pf = pq.ParquetFile(path)
    schema = pf.schema_arrow

    print("\n" + "=" * 90)
    print(f"PARQUET : {path}")
    print(f"ROW GROUPS : {pf.num_row_groups}")
    print(f"COLUMNS    : {len(schema.names)}")
    for i, field in enumerate(schema, 1):
        print(f"{i:>3} | {field.name:<35} | {field.type}")
    print("=" * 90)


def get_gpkg_layers(path: Path):
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.execute("SELECT table_name FROM gpkg_contents;")
    layers = [row[0] for row in cur.fetchall()]
    conn.close()
    return layers


def inspect_gpkg(path: Path):
    conn = sqlite3.connect(path)
    cur = conn.cursor()

    print("\n" + "=" * 90)
    print(f"GPKG : {path}")

    cur.execute("SELECT table_name FROM gpkg_contents;")
    layers = [row[0] for row in cur.fetchall()]

    for layer in layers:
        print("-" * 90)
        print(f"LAYER : {layer}")
        cur.execute(f"PRAGMA table_info({quote_sql_name(layer)});")
        rows = cur.fetchall()
        for i, row in enumerate(rows, 1):
            _, name, coltype, _, _, _ = row
            print(f"{i:>3} | {name:<35} | {coltype}")

    print("=" * 90)
    conn.close()


def inspect_geojson(path: Path):
    print("\n" + "=" * 90)
    print(f"GEOJSON : {path}")

    if fiona is None:
        print("Fiona not installed.")
        print("=" * 90)
        return

    with fiona.open(path) as src:
        print(f"DRIVER : {src.driver}")
        print(f"CRS    : {src.crs}")
        print(f"GEOM   : {src.schema.get('geometry')}")
        props = src.schema.get("properties", {})
        print(f"COLUMNS: {len(props)}")
        for i, (name, typ) in enumerate(props.items(), 1):
            print(f"{i:>3} | {name:<35} | {typ}")

    print("=" * 90)


# -----------------------------------------------------------
# CLEAN / REWRITE
# -----------------------------------------------------------

def clean_csv(path: Path):
    print(f"\nCleaning CSV: {path}")
    print_free_space(path.parent)

    header = pd.read_csv(path, nrows=0)
    all_cols = header.columns.tolist()
    keep_cols = [c for c in all_cols if c not in COLUMNS_TO_DELETE]

    print(f"Columns before: {len(all_cols)}")
    print(f"Columns after : {len(keep_cols)}")

    if len(keep_cols) == len(all_cols):
        print("No matching columns to delete.")
        if not OVERWRITE_FILES:
            shutil.copy2(path, final_output_path(path))
        return

    target = temp_output_path(path) if OVERWRITE_FILES else final_output_path(path)
    cleanup_temp_file(target)

    first = True
    try:
        for chunk in progress(
            pd.read_csv(path, chunksize=CSV_CHUNKSIZE, usecols=keep_cols),
            desc="CSV chunks",
            unit="chunk",
        ):
            chunk.to_csv(
                target,
                mode="w" if first else "a",
                index=False,
                header=first,
            )
            first = False

        if OVERWRITE_FILES:
            safe_replace(target, path)

        print(f"Written: {path if OVERWRITE_FILES else target}")

    except Exception:
        cleanup_temp_file(target)
        raise


def clean_parquet(path: Path):
    if pq is None:
        raise RuntimeError("pyarrow is required for parquet rewrite")

    print(f"\nCleaning Parquet: {path}")
    print_free_space(path.parent)

    pf = pq.ParquetFile(path)
    all_cols = pf.schema_arrow.names
    keep_cols = [c for c in all_cols if c not in COLUMNS_TO_DELETE]

    print(f"Columns before: {len(all_cols)}")
    print(f"Columns after : {len(keep_cols)}")
    print(f"Row groups    : {pf.num_row_groups}")

    if len(keep_cols) == len(all_cols):
        print("No matching columns to delete.")
        if not OVERWRITE_FILES:
            shutil.copy2(path, final_output_path(path))
        return

    target = temp_output_path(path) if OVERWRITE_FILES else final_output_path(path)
    cleanup_temp_file(target)

    writer = None
    try:
        for rg in progress(
            range(pf.num_row_groups),
            total=pf.num_row_groups,
            desc="Parquet row groups",
            unit="rg",
        ):
            table = pf.read_row_group(rg, columns=keep_cols)
            if writer is None:
                writer = pq.ParquetWriter(target, table.schema)
            writer.write_table(table)

        if writer is not None:
            writer.close()
            writer = None

        if OVERWRITE_FILES:
            safe_replace(target, path)

        print(f"Written: {path if OVERWRITE_FILES else target}")

    except Exception:
        if writer is not None:
            writer.close()
        cleanup_temp_file(target)
        raise


def get_gpkg_layer_columns(path: Path, layer: str):
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({quote_sql_name(layer)});")
    rows = cur.fetchall()
    conn.close()
    return [row[1] for row in rows]


def clean_gpkg(path: Path):
    print(f"\nCleaning GPKG: {path}")

    SPECIAL_GPKG_COLUMNS = {"fid", "geom"}

    layers = get_gpkg_layers(path)
    if not layers:
        raise RuntimeError("No layers found in GPKG")

    for layer in layers:
        all_cols = get_gpkg_layer_columns(path, layer)

        keep_cols = [
            c for c in all_cols
            if c not in COLUMNS_TO_DELETE
            and c not in SPECIAL_GPKG_COLUMNS
        ]

        print(f"Layer          : {layer}")
        print(f"Columns before : {len(all_cols)}")
        print(f"Columns after  : {len(keep_cols)}")
        print(f"Dropping       : {[c for c in all_cols if c in COLUMNS_TO_DELETE]}")

        if not keep_cols:
            raise RuntimeError(f"No attribute columns left after deletion in layer: {layer}")

        if len(keep_cols) == len([c for c in all_cols if c not in SPECIAL_GPKG_COLUMNS]):
            print("No matching columns to delete.")
            if not OVERWRITE_FILES:
                target = path.with_name(f"{path.stem}_{layer}_cleaned{path.suffix}")
                shutil.copy2(path, target)
            continue

        if OVERWRITE_FILES and len(layers) == 1:
            target = path.with_name(f"{path.stem}.__tmp__{path.suffix}")
        else:
            target = path.with_name(f"{path.stem}_{layer}_cleaned{path.suffix}")

        cmd = [
            "ogr2ogr",
            "-progress",
            "-f", "GPKG",
            str(target),
            str(path),
            layer,
            "-select", ",".join(keep_cols),
        ]
        run_cmd(cmd)
        print(f"Written: {target}")

        if OVERWRITE_FILES and len(layers) == 1:
            backup = path.with_name(f"{path.stem}.__backup__{path.suffix}")

            if backup.exists():
                backup.unlink()

            path.rename(backup)
            target.rename(path)
            backup.unlink()

            print(f"Overwritten original: {path}")


def clean_geojson(path: Path):
    print(f"\nCleaning GeoJSON: {path}")
    print_free_space(path.parent)

    if fiona is None:
        raise RuntimeError("Fiona is required for GeoJSON schema inspection")

    with fiona.open(path) as src:
        props = src.schema.get("properties", {})
        all_cols = list(props.keys())

    keep_cols = [c for c in all_cols if c not in COLUMNS_TO_DELETE]

    print(f"Columns before: {len(all_cols)}")
    print(f"Columns after : {len(keep_cols)}")

    if not keep_cols:
        raise RuntimeError("No columns left after deletion.")

    if len(keep_cols) == len(all_cols):
        print("No matching columns to delete.")
        if not OVERWRITE_FILES:
            shutil.copy2(path, final_output_path(path))
        return

    target = temp_output_path(path) if OVERWRITE_FILES else final_output_path(path)
    cleanup_temp_file(target)

    cmd = [
        "ogr2ogr",
        "-progress",
        "-f", "GeoJSON",
        str(target),
        str(path),
        "-select", ",".join(keep_cols),
    ]

    try:
        run_cmd(cmd)

        if OVERWRITE_FILES:
            safe_replace(target, path)

        print(f"Written: {path if OVERWRITE_FILES else target}")

    except Exception:
        cleanup_temp_file(target)
        raise


# -----------------------------------------------------------
# MAIN
# -----------------------------------------------------------

def process(path: Path):
    suffix = path.suffix.lower()

    if not DELETE_COLUMNS:
        if suffix == ".csv":
            inspect_csv(path)
        elif suffix == ".parquet":
            inspect_parquet(path)
        elif suffix == ".gpkg":
            inspect_gpkg(path)
        elif suffix == ".geojson":
            inspect_geojson(path)
        return

    if suffix == ".csv":
        clean_csv(path)
    elif suffix == ".parquet":
        clean_parquet(path)
    elif suffix == ".gpkg":
        clean_gpkg(path)
    elif suffix == ".geojson":
        clean_geojson(path)


def main():
    print(f"BASE_DIR          : {BASE_DIR}")
    print(f"FILE_PREFIX       : {FILE_PREFIX}")
    print(f"DELETE_COLUMNS    : {DELETE_COLUMNS}")
    print(f"OVERWRITE_FILES   : {OVERWRITE_FILES}")
    print(f"COLUMNS_TO_DELETE : {COLUMNS_TO_DELETE}")

    files = find_matching_files(BASE_DIR)

    if not files:
        print("\nNo matching files found.")
        return

    print(f"\nFound {len(files)} matching file(s):")
    for f in files:
        print(f" - {f}")

    for i, path in enumerate(files, 1):
        print(f"\n[{i}/{len(files)}] Processing: {path}")
        try:
            process(path)
        except Exception as e:
            print(f"\nFAILED: {path}")
            print(f"ERROR : {e}")


if __name__ == "__main__":
    main()
