#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from pathlib import Path
import pandas as pd


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

RESOLVE_RELATIVE_PATHS = True
N_EXAMPLES = 5

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

def norm(x):
    if pd.isna(x):
        return pd.NA
    s = str(x).strip()
    if s == "" or s.lower() in {"none", "nan", "<na>"}:
        return pd.NA
    return s


def prep_df(df, parquet_path=None):
    df = df.copy()

    for col in PATH_COLS:
        if col not in df.columns:
            df[col] = pd.NA
        df[col] = df[col].map(norm)

    if RESOLVE_RELATIVE_PATHS and parquet_path is not None:
        base = parquet_path.parent
        for col in PATH_COLS:
            def _resolve(x):
                if pd.isna(x):
                    return pd.NA
                p = Path(str(x))
                return str(p if p.is_absolute() else (base / p).resolve())
            df[col] = df[col].map(_resolve)

    return df


def print_title(txt):
    print("\n" + "=" * 90)
    print(txt)
    print("=" * 90)


def print_basic_info(name, df):
    print_title(name)
    print(f"rows: {len(df):,}")
    print(f"cols: {len(df.columns):,}")
    print("\ncolumns:")
    print(", ".join(df.columns))

    if all(c in df.columns for c in KEY_COLS):
        n_unique = df[KEY_COLS].drop_duplicates().shape[0]
        n_dup = len(df) - n_unique
        print(f"\nunique key rows ({KEY_COLS}): {n_unique:,}")
        print(f"duplicate key rows          : {n_dup:,}")

    if "modType" in df.columns:
        print("\nmodType counts:")
        print(df["modType"].value_counts(dropna=False).to_string())

    print("\npath completeness:")
    for mt in ["res", "rel"]:
        if "modType" in df.columns:
            sub = df[df["modType"] == mt]
        else:
            sub = df
        if len(sub) == 0:
            continue
        any_path = sub[PATH_COLS].notna().any(axis=1).sum()
        print(f"  modType={mt:3s} | rows={len(sub):,} | rows with any tif path={any_path:,}")


def compare_to_base(base, pilot, pilot_name):
    print_title(f"COMPARE EUREGIO vs {pilot_name}")

    base_keys = base[KEY_COLS].drop_duplicates()
    pilot_keys = pilot[KEY_COLS].drop_duplicates()

    overlap = base_keys.merge(pilot_keys, on=KEY_COLS, how="inner")
    print(f"base unique rows   : {len(base_keys):,}")
    print(f"pilot unique rows  : {len(pilot_keys):,}")
    print(f"overlap unique rows: {len(overlap):,}")

    merged = base[KEY_COLS + PATH_COLS].merge(
        pilot[KEY_COLS + PATH_COLS],
        on=KEY_COLS,
        how="outer",
        suffixes=("_base", "_pilot"),
        indicator=True
    )

    def classify(row):
        if row["_merge"] == "left_only":
            return "base_only"
        if row["_merge"] == "right_only":
            return "pilot_only"

        gain = 0
        conflict = 0
        for col in PATH_COLS:
            b = row[f"{col}_base"]
            p = row[f"{col}_pilot"]
            if pd.isna(b) and not pd.isna(p):
                gain += 1
            elif not pd.isna(b) and not pd.isna(p) and str(b) != str(p):
                conflict += 1

        if gain > 0 and conflict == 0:
            return "candidate_enrich"
        if conflict > 0:
            return "conflict"
        return "same_or_no_gain"

    merged["status"] = merged.apply(classify, axis=1)

    print("\nmerge status:")
    print(merged["status"].value_counts().to_string())

    # examples
    meta_cols = [c for c in ["LKGebiet", "LKGebietID", "LKRegion", "LWDGebietID", "flow", "sector", "subC"] if c in base.columns]
    meta = base[KEY_COLS + meta_cols].drop_duplicates(KEY_COLS)
    merged = merged.merge(meta, on=KEY_COLS, how="left")

    for status in ["candidate_enrich", "conflict", "pilot_only"]:
        ex = merged[merged["status"] == status].head(N_EXAMPLES)
        print(f"\n--- examples: {status} ---")
        if ex.empty:
            print("none")
            continue

        for _, row in ex.iterrows():
            print(f"\nkey: praID={row['praID']} | resultID={row['resultID']} | modType={row['modType']}")
            for c in meta_cols:
                print(f"  {c}: {row.get(c)}")
            for col in PATH_COLS:
                b = row.get(f"{col}_base")
                p = row.get(f"{col}_pilot")
                if (pd.isna(b) and pd.isna(p)):
                    continue
                print(f"  {col}")
                print(f"    base : {b}")
                print(f"    pilot: {p}")


# =============================================================================
# RUNNER
# =============================================================================

def main():
    base = prep_df(pd.read_parquet(EUREGIO_FILE), EUREGIO_FILE)
    print_basic_info("EUREGIO", base)

    for pilot_file in PILOT_FILES:
        pilot = prep_df(pd.read_parquet(pilot_file), pilot_file)
        print_basic_info(pilot_file.stem, pilot)
        compare_to_base(base, pilot, pilot_file.stem)

    print_title("DONE")
    print("Dry run only. No merge written.")


if __name__ == "__main__":
    main()