#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Robust converter:
Parquet dataset / single Parquet / GeoPackage -> GeoPackage

Main idea for huge parquet datasets:
- if input is a folder with part-*.parquet files, convert each part one by one
- create the GPKG from the first part
- append all following parts
- avoid loading the whole dataset into memory at once

Requires:
- ogr2ogr available in PATH
- GDAL parquet support available in your environment
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path


# =============================================================================
# USER SETTINGS
# =============================================================================

inputFile = Path(
    "/media/christoph/SSD 500 GB/Cairos/ModelChainResults/Euregio/"
    "cairosAvaMaps/13_avaScenMaps/EUREGIO/avaScen_cairosAvaMapsDVT"
)

outputFile = Path(
    "/media/christoph/SSD 500 GB/Cairos/ModelChainResults/Euregio/"
    "cairosAvaMaps/13_avaScenMaps/EUREGIO/avaScen_cairosAvaMapsDVT/"
    "avaScen_cairosAvaMapsDVT.gpkg"
)

# target CRS
outputEpsg = 25832

# output layer name inside GPKG
outputLayer = "avaScen_cairosAvaMapsDVT"

# overwrite existing output gpkg
overwrite = True

# safer = False / faster = True
# fast mode may be less crash-safe on flaky external disks
fastMode = False

# if True, attempt to repair invalid geometries during conversion
makeValid = False

# if True, promote polygons to multipolygons / mixed to multi where possible
promoteToMulti = True

# transaction group size for ogr2ogr
# smaller = slower but often safer on unstable disks
groupTransactions = 20000

# stop immediately on first failed part
stopOnError = True


# =============================================================================
# LOGGING
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

log = logging.getLogger(__name__)


# =============================================================================
# HELPERS
# =============================================================================

def find_ogr2ogr() -> str:
    exe = shutil.which("ogr2ogr")
    if not exe:
        raise RuntimeError("ogr2ogr not found in PATH")
    return exe


def is_parquet_dataset_dir(path: Path) -> bool:
    return path.is_dir() and any(path.glob("part-*.parquet"))


def list_dataset_parts(path: Path) -> list[Path]:
    return sorted(path.glob("part-*.parquet"))


def validate_paths():
    if not inputFile.exists():
        raise FileNotFoundError(inputFile)

    if outputFile.suffix.lower() != ".gpkg":
        raise ValueError("Output must end with .gpkg")

    if inputFile.is_file():
        if inputFile.suffix.lower() not in [".parquet", ".gpkg"]:
            raise ValueError("Input file must be .parquet or .gpkg")
    elif inputFile.is_dir():
        if not is_parquet_dataset_dir(inputFile):
            raise ValueError(
                f"Input folder is not a parquet dataset with part-*.parquet files: {inputFile}"
            )
    else:
        raise ValueError(f"Unsupported input path: {inputFile}")

    outputFile.parent.mkdir(parents=True, exist_ok=True)


def build_common_cmd(ogr2ogr_exe: str) -> list[str]:
    cmd = [ogr2ogr_exe, "-f", "GPKG"]

    if outputEpsg:
        cmd += ["-t_srs", f"EPSG:{outputEpsg}"]

    if outputLayer:
        cmd += ["-nln", outputLayer]

    if promoteToMulti:
        cmd += ["-nlt", "PROMOTE_TO_MULTI"]

    if makeValid:
        cmd += ["-makevalid"]

    if groupTransactions:
        cmd += ["-gt", str(groupTransactions)]

    # GDAL / SQLite tuning
    # keep default safe mode unless explicitly opting into faster behavior
    if fastMode:
        cmd += ["--config", "OGR_SQLITE_SYNCHRONOUS", "OFF"]
        cmd += ["--config", "OGR_GPKG_FOREIGN_KEY_CHECK", "NO"]

    return cmd


def run_cmd(cmd: list[str], label: str):
    log.info("Running: %s", label)
    log.debug("Command: %s", " ".join(str(c) for c in cmd))

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True
    )

    if result.stdout.strip():
        log.debug(result.stdout.strip())

    if result.returncode != 0:
        if result.stderr.strip():
            log.error(result.stderr.strip())
        raise RuntimeError(f"ogr2ogr failed during: {label}")

    if result.stderr.strip():
        # ogr2ogr often prints progress/info on stderr
        log.info(result.stderr.strip())


def convert_single_file_to_gpkg(src: Path):
    ogr2ogr_exe = find_ogr2ogr()

    if overwrite and outputFile.exists():
        log.info("Removing existing output: %s", outputFile)
        outputFile.unlink()

    cmd = build_common_cmd(ogr2ogr_exe)

    if overwrite:
        cmd.append("-overwrite")

    cmd += [str(outputFile), str(src)]

    run_cmd(cmd, f"single file conversion: {src.name}")


def convert_parquet_dataset_dir_to_gpkg(src_dir: Path):
    ogr2ogr_exe = find_ogr2ogr()
    parts = list_dataset_parts(src_dir)

    if not parts:
        raise RuntimeError(f"No part-*.parquet files found in {src_dir}")

    log.info("Found %s parquet part(s)", len(parts))

    if overwrite and outputFile.exists():
        log.info("Removing existing output: %s", outputFile)
        outputFile.unlink()

    for i, part in enumerate(parts, start=1):
        label = f"part {i}/{len(parts)} :: {part.name}"
        log.info("Converting %s", label)

        cmd = build_common_cmd(ogr2ogr_exe)

        if i == 1:
            cmd.append("-overwrite")
        else:
            cmd.append("-append")
            # for append, keep the same target layer
            if outputLayer:
                cmd += ["-nln", outputLayer]

        cmd += [str(outputFile), str(part)]

        try:
            run_cmd(cmd, label)
        except Exception:
            log.exception("Failed on %s", label)
            if stopOnError:
                raise

    log.info("All parts appended successfully.")


# =============================================================================
# RUNNER
# =============================================================================

def main():
    validate_paths()

    log.info("Input : %s", inputFile)
    log.info("Output: %s", outputFile)
    log.info("Layer : %s", outputLayer)
    log.info("EPSG  : %s", outputEpsg)
    log.info("Mode  : %s", "FAST" if fastMode else "SAFE")

    if inputFile.is_dir():
        convert_parquet_dataset_dir_to_gpkg(inputFile)
    else:
        convert_single_file_to_gpkg(inputFile)

    log.info("Finished successfully.")
    log.info("GeoPackage created: %s", outputFile)


if __name__ == "__main__":
    main()