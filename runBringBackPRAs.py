#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import logging
from pathlib import Path

import pandas as pd
import geopandas as gpd


log = logging.getLogger(__name__)


# ------------------ Input / output ------------------ #
inputParquet = Path(
    "/media/christoph/SSD 500 GB/Cairos/ModelChainResults/Euregio/cairosAvaMaps/12_avaDirectory/pilotSella/avaDirectoryResults_pilotSella.parquet"
)

# Existing clipped outputs created by your clipping script
clippedParquet = Path(
    "/media/christoph/SSD 500 GB/Cairos/ModelChainResults/Euregio/cairosAvaMaps/12_avaDirectory/pilotSella/avaDirectoryResults_pilotSella_clipped.parquet"
)

clippedCsv = Path(
    "/media/christoph/SSD 500 GB/Cairos/ModelChainResults/Euregio/cairosAvaMaps/12_avaDirectory/pilotSella/avaDirectoryResults_pilotSella_clipped.csv"
)

clippedGpkg = Path(
    "/media/christoph/SSD 500 GB/Cairos/ModelChainResults/Euregio/cairosAvaMaps/12_avaDirectory/pilotSella/avaDirectoryResults_pilotSella_clipped.gpkg"
)

# Optional: write to new files instead of overwriting the existing clipped outputs
writeToNewFiles = True

# Optional suffix for new files
mergeSuffix = "_withRel"


# ------------------ Optional subset handling ------------------ #
avaDirSubset = False
avaDirSubsetPath = Path("/home/christoph/Documents/JuliaMA/catchment_stubai_UTM32N.geojson")

# Allowed: "off", "mask", "clip"
subsetMode = "mask"


# ------------------ Path columns ------------------ #
pathCols = [
    "pathCellcounts",
    "pathInputpra",
    "pathTravelanglemax",
    "pathTravelanglemax_sized",
    "pathTravellengthmax",
    "pathTravellengthmax_sized",
    "pathZdelta",
    "pathZdelta_sized",
]


# ------------------ Schema helpers ------------------ #
def _ensureSchema(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    if "praID" not in gdf.columns:
        raise ValueError("Missing required column: praID")
    if "resultID" not in gdf.columns:
        raise ValueError("Missing required column: resultID")
    if gdf.geometry is None:
        raise ValueError("Missing geometry column")

    for col in pathCols:
        if col not in gdf.columns:
            gdf[col] = None

    gdf["praID"] = pd.to_numeric(gdf["praID"], errors="coerce").astype("Int64")
    gdf["resultID"] = gdf["resultID"].astype("string")

    if "LKRegion" in gdf.columns:
        gdf["LKRegion"] = gdf["LKRegion"].astype("string").str.strip()

    if "modType" in gdf.columns:
        gdf["modType"] = gdf["modType"].astype("string").str.strip()

    before = len(gdf)
    gdf = gdf[gdf["praID"].notna() & gdf["resultID"].notna()].copy()
    dropped = before - len(gdf)
    if dropped:
        log.info("Dropped %d rows missing praID/resultID", dropped)

    return gdf


def _getKeyCols(gdf: gpd.GeoDataFrame) -> list[str]:
    keyCols = ["praID", "resultID"]
    if "LKRegion" in gdf.columns:
        keyCols.append("LKRegion")
    if "modType" in gdf.columns:
        keyCols.append("modType")
    return keyCols


def _alignColumns(sourceGdf: gpd.GeoDataFrame, targetCols: list[str], targetCrs) -> gpd.GeoDataFrame:
    gdf = sourceGdf.copy()

    if gdf.crs != targetCrs:
        log.info("Reproject rel rows: %s -> %s", gdf.crs, targetCrs)
        gdf = gdf.to_crs(targetCrs)

    for col in targetCols:
        if col not in gdf.columns and col != "geometry":
            gdf[col] = None

    keepCols = [col for col in targetCols if col in gdf.columns or col == "geometry"]
    gdf = gdf[keepCols].copy()

    missingCols = [col for col in targetCols if col not in gdf.columns and col != "geometry"]
    for col in missingCols:
        gdf[col] = None

    orderedCols = [col for col in targetCols if col != "geometry"] + ["geometry"]
    orderedCols = [col for col in orderedCols if col in gdf.columns]

    return gdf[orderedCols].copy()


# ------------------ Subset helpers ------------------ #
def _validateSubsetMode(subsetModeLocal: str) -> str:
    allowed = {"off", "mask", "clip"}
    if subsetModeLocal not in allowed:
        raise ValueError(f"Invalid subsetMode: {subsetModeLocal}. Allowed: {sorted(allowed)}")
    return subsetModeLocal


def _loadSubsetUnion(subsetPath: Path, targetCrs):
    if not subsetPath.exists():
        raise FileNotFoundError(subsetPath)

    subsetGdf = gpd.read_file(subsetPath)

    if subsetGdf.empty:
        raise ValueError(f"Subset file is empty: {subsetPath}")
    if subsetGdf.geometry is None:
        raise ValueError(f"Subset file has no geometry: {subsetPath}")
    if subsetGdf.crs is None:
        raise ValueError(f"Subset file has no CRS: {subsetPath}")
    if targetCrs is None:
        raise ValueError("Target CRS is missing")

    subsetGdf = subsetGdf[subsetGdf.geometry.notna()].copy()
    subsetGdf = subsetGdf[~subsetGdf.geometry.is_empty].copy()

    if subsetGdf.empty:
        raise ValueError(f"Subset file has no valid geometry: {subsetPath}")

    if subsetGdf.crs != targetCrs:
        log.info("Reproject subset: %s -> %s", subsetGdf.crs, targetCrs)
        subsetGdf = subsetGdf.to_crs(targetCrs)

    return subsetGdf.geometry.union_all()


def _applySubsetMask(gdf: gpd.GeoDataFrame, subsetUnion) -> gpd.GeoDataFrame:
    before = len(gdf)
    gdf = gdf[gdf.geometry.notna()].copy()
    gdf = gdf[~gdf.geometry.is_empty].copy()
    gdf = gdf[gdf.geometry.intersects(subsetUnion)].copy()
    log.info("Subset mask: %d -> %d rows", before, len(gdf))
    return gdf


def _applySubsetClip(gdf: gpd.GeoDataFrame, subsetUnion) -> gpd.GeoDataFrame:
    before = len(gdf)
    gdf = gdf[gdf.geometry.notna()].copy()
    gdf = gdf[~gdf.geometry.is_empty].copy()
    gdf = gdf[gdf.geometry.intersects(subsetUnion)].copy()
    log.info("Subset clip filter: %d -> %d rows", before, len(gdf))

    if gdf.empty:
        return gdf

    gdf["geometry"] = gdf.geometry.intersection(subsetUnion)
    gdf = gdf[gdf.geometry.notna()].copy()
    gdf = gdf[~gdf.geometry.is_empty].copy()
    log.info("Subset clip geometry complete: %d rows remain", len(gdf))
    return gdf


def _applySubsetSelection(
    gdf: gpd.GeoDataFrame,
    subsetPath: Path,
    subsetModeLocal: str,
) -> gpd.GeoDataFrame:
    subsetModeLocal = _validateSubsetMode(subsetModeLocal)

    if subsetModeLocal == "off":
        return gdf

    subsetUnion = _loadSubsetUnion(subsetPath, gdf.crs)

    if subsetModeLocal == "mask":
        return _applySubsetMask(gdf, subsetUnion)

    if subsetModeLocal == "clip":
        return _applySubsetClip(gdf, subsetUnion)

    raise ValueError(f"Unhandled subsetMode: {subsetModeLocal}")


# ------------------ Output helpers ------------------ #
def _buildOutPaths() -> tuple[Path, Path, Path]:
    if not writeToNewFiles:
        return clippedParquet, clippedCsv, clippedGpkg

    outParquet = clippedParquet.with_name(f"{clippedParquet.stem}{mergeSuffix}{clippedParquet.suffix}")
    outCsv = clippedCsv.with_name(f"{clippedCsv.stem}{mergeSuffix}{clippedCsv.suffix}")
    outGpkg = clippedGpkg.with_name(f"{clippedGpkg.stem}{mergeSuffix}{clippedGpkg.suffix}")
    return outParquet, outCsv, outGpkg


def _writeResults(gdf: gpd.GeoDataFrame, outParquet: Path, outCsv: Path, outGpkg: Path) -> None:
    gdf.to_parquet(outParquet, index=False)
    log.info("Wrote parquet: %s", outParquet)

    gdf.drop(columns="geometry", errors="ignore").to_csv(outCsv, index=False)
    log.info("Wrote csv: %s", outCsv)

    gdf.to_file(outGpkg, layer="avaScenClip", driver="GPKG")
    log.info("Wrote gpkg: %s", outGpkg)


# ------------------ Main ------------------ #
def main() -> int:
    subsetModeLocal = "off"
    if avaDirSubset:
        subsetModeLocal = _validateSubsetMode(subsetMode)

    log.info("mergeRelIntoClipped started")
    log.info("Input parquet: %s", inputParquet)
    log.info("Clipped parquet: %s", clippedParquet)
    log.info("Clipped csv: %s", clippedCsv)
    log.info("Clipped gpkg: %s", clippedGpkg)
    log.info("writeToNewFiles: %s", writeToNewFiles)
    log.info("avaDirSubset: %s", avaDirSubset)
    log.info("subsetMode: %s", subsetModeLocal)

    if not inputParquet.exists():
        raise FileNotFoundError(inputParquet)
    if not clippedParquet.exists():
        raise FileNotFoundError(clippedParquet)

    outParquet, outCsv, outGpkg = _buildOutPaths()

    clippedGdf = gpd.read_parquet(clippedParquet)
    clippedGdf = _ensureSchema(clippedGdf)

    inputGdf = gpd.read_parquet(inputParquet)
    inputGdf = _ensureSchema(inputGdf)

    if "modType" not in inputGdf.columns:
        raise ValueError("Input parquet has no modType column")

    relGdf = inputGdf[inputGdf["modType"] == "rel"].copy()
    log.info("Found %d rel rows in input parquet", len(relGdf))

    if relGdf.empty:
        log.warning("No rel rows found in input parquet")
        _writeResults(clippedGdf, outParquet, outCsv, outGpkg)
        return 0

    if avaDirSubset:
        relGdf = _applySubsetSelection(relGdf, avaDirSubsetPath, subsetModeLocal)

    relGdf = _alignColumns(relGdf, list(clippedGdf.columns), clippedGdf.crs)

    keyCols = _getKeyCols(clippedGdf)

    beforeRel = len(relGdf)
    relGdf = relGdf.drop_duplicates(subset=keyCols, keep="first").copy()
    droppedRel = beforeRel - len(relGdf)
    if droppedRel:
        log.info("Dropped %d duplicate rel rows before merge", droppedRel)

    combinedGdf = pd.concat([clippedGdf, relGdf], ignore_index=True)
    combinedGdf = gpd.GeoDataFrame(combinedGdf, geometry="geometry", crs=clippedGdf.crs)

    beforeCombined = len(combinedGdf)
    combinedGdf = combinedGdf.drop_duplicates(subset=keyCols, keep="first").copy()
    droppedCombined = beforeCombined - len(combinedGdf)
    if droppedCombined:
        log.info("Dropped %d duplicate rows after merge", droppedCombined)

    addedRows = len(combinedGdf) - len(clippedGdf)
    log.info("Added %d rel rows into clipped outputs", addedRows)
    log.info("Final row count: %d -> %d", len(clippedGdf), len(combinedGdf))

    _writeResults(combinedGdf, outParquet, outCsv, outGpkg)

    log.info("DONE")
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    raise SystemExit(main())