#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import logging
import time
from pathlib import Path

import pandas as pd
import geopandas as gpd


log = logging.getLogger(__name__)


# ------------------ Inputs ------------------ #
inputParquet = Path(
    "/media/christoph/SSD 500 GB/Cairos/ModelChainResults/Euregio/cairosAvaMaps/12_avaDirectory/pilotStubai/avaDirectoryResults_pilotStubai_noPaths.parquet"
)

clipOutRoot = Path(
    "/media/christoph/SSD 500 GB/Cairos/ModelChainResults/Euregio/cairosAvaMaps/11_avaDirectoryData/pilotStubai/"
)

flowPyBigDataRoots = {
    "Tirol": Path(
        "/media/christoph/SSD 500 GB/Cairos/ModelChainResults/Euregio/NTirol/251023/"
        "alpha32_3_umax8_18_maxS5/09_flowPyBigDataStructure"
    ),
    "Südtirol": Path(
        "/media/christoph/SSD 500 GB/Cairos/ModelChainResults/Euregio/STirol/251023/"
        "alpha32_3_umax8_18_maxS5/09_flowPyBigDataStructure"
    ),
    "Trentino": Path(
        "/media/christoph/SSD 500 GB/Cairos/ModelChainResults/Euregio/Trentino/251023/"
        "alpha32_3_umax8_18_maxS5/09_flowPyBigDataStructure"
    ),
}

useFallbackRoot = False
fallbackFlowPyBigDataRoot = Path(
    "/media/christoph/SSD 500 GB/Cairos/ModelChainResults/Euregio/NTirol/251023/"
    "alpha32_3_umax8_18_maxS5/09_flowPyBigDataStructure"
)

# allowed:
# "prefer_clipped" -> use clipped file if it exists, else fallback to FlowPy source
# "clipped_only"   -> only use clipped file
# "source_only"    -> only use FlowPy source
rasterPathMode = "clipped_only"

# output name suffix
outputSuffix = ""

# optional outputs
writeCsv = True
writeGpkg = True


typePatterns = {
    "inputPRA": "-area_m.tif",
    "cellCounts": "_cellCounts_lzw.tif",
    "zDelta": "_zdelta_lzw.tif",
    "zDelta_sized": "_zdelta_sized_lzw.tif",
    "travelLengthMax": "_travelLengthMax_lzw.tif",
    "travelLengthMax_sized": "_travelLengthMax_sized_lzw.tif",
    "travelAngleMax": "_fpTravelAngleMax_lzw.tif",
    "travelAngleMax_sized": "_fpTravelAngleMax_sized_lzw.tif",
}

typeToPathCol = {
    "cellCounts": "pathCellcounts",
    "inputPRA": "pathInputpra",
    "travelAngleMax": "pathTravelanglemax",
    "travelAngleMax_sized": "pathTravelanglemax_sized",
    "travelLengthMax": "pathTravellengthmax",
    "travelLengthMax_sized": "pathTravellengthmax_sized",
    "zDelta": "pathZdelta",
    "zDelta_sized": "pathZdelta_sized",
}


def _ensureSchema(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    if "praID" not in gdf.columns:
        raise ValueError("Missing required column: praID")
    if "resultID" not in gdf.columns:
        raise ValueError("Missing required column: resultID")
    if gdf.geometry is None:
        raise ValueError("Missing geometry column")

    for col in typeToPathCol.values():
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


def _dedupKeepRelAndRes(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    before = len(gdf)

    dedupCols = ["praID", "resultID"]
    if "modType" in gdf.columns:
        dedupCols.append("modType")
    if "LKRegion" in gdf.columns:
        dedupCols.append("LKRegion")

    gdf = gdf.drop_duplicates(subset=dedupCols, keep="first").copy()
    dropped = before - len(gdf)
    if dropped:
        log.info("Dropped %d duplicate rows on %s", dropped, ",".join(dedupCols))

    if "modType" in gdf.columns:
        counts = gdf["modType"].fillna("<NA>").value_counts(dropna=False)
        for modTypeValue, count in counts.items():
            log.info("Rows for modType %s: %d", modTypeValue, count)

    return gdf


def _normalizeRegion(value):
    if pd.isna(value):
        return None
    text = str(value).strip()
    return text if text else None


def _validateRasterPathMode(mode: str) -> str:
    allowed = {"prefer_clipped", "clipped_only", "source_only"}
    if mode not in allowed:
        raise ValueError(f"Invalid rasterPathMode: {mode}. Allowed: {sorted(allowed)}")
    return mode


def _validateFlowPyRoots(rootsByRegion):
    validRoots = {}
    for regionName, rootPath in rootsByRegion.items():
        if rootPath.exists():
            validRoots[regionName] = rootPath
        else:
            log.warning("Missing FlowPy root for %s: %s", regionName, rootPath)

    if useFallbackRoot and not fallbackFlowPyBigDataRoot.exists():
        raise FileNotFoundError(fallbackFlowPyBigDataRoot)

    if not validRoots and not useFallbackRoot:
        raise ValueError("No valid FlowPy roots found")

    return validRoots


def _buildResDirIndices(root: Path):
    t0 = time.perf_counter()
    peakIndex = {}
    sizeIndex = {}

    for dirPath, dirNames, _ in os.walk(root):
        base = os.path.basename(dirPath)
        if base not in ("peakFiles", "sizeFiles"):
            continue

        for dirName in dirNames:
            if not dirName.startswith("res_"):
                continue

            rid = dirName.replace("res_", "").strip()
            resDir = Path(dirPath) / dirName

            if base == "peakFiles":
                peakIndex[rid] = resDir
            else:
                sizeIndex[rid] = resDir

    log.info(
        "Indexed root %s | peakFiles=%d sizeFiles=%d in %.2fs",
        root,
        len(peakIndex),
        len(sizeIndex),
        time.perf_counter() - t0,
    )
    return peakIndex, sizeIndex


def _pickLatestBySuffix(folder: Path, suffix: str):
    candidates = sorted(folder.glob(f"*{suffix}"))
    if not candidates:
        return None

    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return candidates[0]


def _findRelAreaRaster(resDir: Path):
    try:
        outputsDir = resDir.parent.parent.parent
        flowDir = outputsDir.parent.parent
        relDir = flowDir / "Inputs" / "REL"

        if not relDir.is_dir():
            return None

        candidates = sorted(relDir.glob(f"*{typePatterns['inputPRA']}"))
        return candidates[0] if candidates else None

    except Exception:
        return None


def _buildRidFileCacheForRoot(peakIndex, sizeIndex):
    t0 = time.perf_counter()
    cache = {}
    allRids = set(peakIndex.keys()) | set(sizeIndex.keys())

    for rid in allRids:
        peakResDir = peakIndex.get(rid)
        sizeResDir = sizeIndex.get(rid)
        entry = {}

        for typeKey in [
            "cellCounts",
            "zDelta",
            "zDelta_sized",
            "travelLengthMax",
            "travelLengthMax_sized",
            "travelAngleMax",
            "travelAngleMax_sized",
        ]:
            suffix = typePatterns[typeKey]
            isSized = typeKey.endswith("_sized")
            srcFolder = sizeResDir if isSized else peakResDir

            if srcFolder is None:
                continue

            srcPath = _pickLatestBySuffix(srcFolder, suffix)
            if srcPath:
                entry[typeKey] = srcPath

        baseForRel = peakResDir or sizeResDir
        if baseForRel:
            relArea = _findRelAreaRaster(baseForRel)
            if relArea:
                entry["inputPRA"] = relArea

        if entry:
            cache[rid] = entry

    log.info(
        "Built rid→file cache for %d rids in %.2fs",
        len(cache),
        time.perf_counter() - t0,
    )
    return cache


def _buildRegionCaches(rootsByRegion):
    regionCaches = {}

    for regionName, rootPath in rootsByRegion.items():
        log.info("Build cache for region %s: %s", regionName, rootPath)
        peakIndex, sizeIndex = _buildResDirIndices(rootPath)
        regionCaches[regionName] = _buildRidFileCacheForRoot(peakIndex, sizeIndex)

    if useFallbackRoot:
        log.info("Build cache for fallback root: %s", fallbackFlowPyBigDataRoot)
        peakIndex, sizeIndex = _buildResDirIndices(fallbackFlowPyBigDataRoot)
        regionCaches["__fallback__"] = _buildRidFileCacheForRoot(peakIndex, sizeIndex)

    return regionCaches


def _getRidSourceMap(row, regionCaches):
    regionName = _normalizeRegion(row["LKRegion"]) if "LKRegion" in row.index else None

    if regionName in regionCaches:
        return regionCaches[regionName].get(str(row["resultID"]))

    if useFallbackRoot and "__fallback__" in regionCaches:
        return regionCaches["__fallback__"].get(str(row["resultID"]))

    return None


def _buildClippedFileIndex(clipRoot: Path) -> dict[tuple[str, int, str], Path]:
    t0 = time.perf_counter()
    index: dict[tuple[str, int, str], Path] = {}

    if not clipRoot.exists():
        raise FileNotFoundError(clipRoot)

    comDirs = sorted(
        path for path in clipRoot.iterdir()
        if path.is_dir() and path.name.startswith("com4_")
    )

    for outDir in comDirs:
        rid = outDir.name.replace("com4_", "", 1).strip()
        if not rid:
            continue

        for tifPath in outDir.glob("*.tif"):
            name = tifPath.name

            if not name.startswith("praID"):
                continue

            try:
                praPart = name.split("_", 1)[0]
                praId = int(praPart.replace("praID", ""))
            except Exception:
                continue

            matchedTypeKey = None
            for typeKey, suffix in typePatterns.items():
                if name.endswith(suffix):
                    matchedTypeKey = typeKey
                    break

            if matchedTypeKey is None:
                continue

            index[(rid, praId, matchedTypeKey)] = tifPath

    log.info(
        "Built clipped file index with %d entries from %d com4 folders in %.2fs",
        len(index),
        len(comDirs),
        time.perf_counter() - t0,
    )
    return index


def _resolveClippedOnlyPathForType(
    row,
    typeKey: str,
    inputBaseDir: Path,
    clippedIndex,
):
    praId = int(row["praID"])
    rid = str(row["resultID"])

    clippedPath = clippedIndex.get((rid, praId, typeKey))
    if clippedPath is None:
        return None, "missing_clipped"

    return os.path.relpath(clippedPath, start=inputBaseDir), "clipped"


def _resolveSourcePathForType(
    row,
    typeKey: str,
    inputBaseDir: Path,
    regionCaches,
):
    srcMap = _getRidSourceMap(row, regionCaches)
    if not srcMap:
        return None, "no_src_map"

    srcPath = srcMap.get(typeKey)
    if srcPath is None:
        return None, "no_src_type"

    if not srcPath.exists():
        return None, "missing_source"

    return os.path.relpath(srcPath, start=inputBaseDir), "source"


def _resolvePreferClippedPathForType(
    row,
    typeKey: str,
    inputBaseDir: Path,
    clippedIndex,
    regionCaches,
):
    clippedPath, _ = _resolveClippedOnlyPathForType(
        row=row,
        typeKey=typeKey,
        inputBaseDir=inputBaseDir,
        clippedIndex=clippedIndex,
    )
    if clippedPath is not None:
        return clippedPath, "clipped"

    sourcePath, sourceStatus = _resolveSourcePathForType(
        row=row,
        typeKey=typeKey,
        inputBaseDir=inputBaseDir,
        regionCaches=regionCaches,
    )
    if sourcePath is not None:
        return sourcePath, "source"

    if sourceStatus in {"no_src_map", "no_src_type", "missing_source"}:
        return None, "missing_both"

    return None, sourceStatus


def _rowNeedsResPaths(row) -> bool:
    if "modType" not in row.index:
        return True
    modTypeValue = row["modType"]
    if pd.isna(modTypeValue):
        return True
    return str(modTypeValue).strip().lower() == "res"


def main():
    t0 = time.perf_counter()

    mode = _validateRasterPathMode(rasterPathMode)

    if not inputParquet.exists():
        raise FileNotFoundError(inputParquet)

    log.info("runResDataForSubset started")
    log.info("Input parquet: %s", inputParquet)
    log.info("Clip out root: %s", clipOutRoot)
    log.info("rasterPathMode: %s", mode)

    clippedIndex = {}
    if mode in {"clipped_only", "prefer_clipped"}:
        log.info("Build clipped file index from: %s", clipOutRoot)
        clippedIndex = _buildClippedFileIndex(clipOutRoot)

    regionCaches = {}
    if mode in {"prefer_clipped", "source_only"}:
        rootsByRegion = _validateFlowPyRoots(flowPyBigDataRoots)
        regionCaches = _buildRegionCaches(rootsByRegion)
    else:
        log.info("clipped_only mode -> skip FlowPy cache building")

    gdf = gpd.read_parquet(inputParquet)
    gdf = _ensureSchema(gdf)
    gdf = _dedupKeepRelAndRes(gdf)

    if gdf.empty:
        log.warning("No rows left after schema/filtering.")
        return

    foundClipped = 0
    foundSource = 0
    missingClipped = 0
    missingSource = 0
    missingBoth = 0
    noSrcMap = 0
    noSrcType = 0
    updatedCells = 0
    skippedRelRows = 0

    totalRows = len(gdf)

    for n, (i, row) in enumerate(gdf.iterrows(), start=1):
        if n % 1000 == 0 or n == totalRows:
            log.info("Processed %d / %d rows", n, totalRows)

        if not _rowNeedsResPaths(row):
            skippedRelRows += 1
            continue

        for typeKey, colName in typeToPathCol.items():
            if mode == "clipped_only":
                bestPath, status = _resolveClippedOnlyPathForType(
                    row=row,
                    typeKey=typeKey,
                    inputBaseDir=inputParquet.parent,
                    clippedIndex=clippedIndex,
                )

            elif mode == "source_only":
                bestPath, status = _resolveSourcePathForType(
                    row=row,
                    typeKey=typeKey,
                    inputBaseDir=inputParquet.parent,
                    regionCaches=regionCaches,
                )

            else:
                bestPath, status = _resolvePreferClippedPathForType(
                    row=row,
                    typeKey=typeKey,
                    inputBaseDir=inputParquet.parent,
                    clippedIndex=clippedIndex,
                    regionCaches=regionCaches,
                )

            if bestPath is not None:
                gdf.at[i, colName] = bestPath
                updatedCells += 1

            if status == "clipped":
                foundClipped += 1
            elif status == "source":
                foundSource += 1
            elif status == "missing_clipped":
                missingClipped += 1
            elif status == "missing_source":
                missingSource += 1
            elif status == "missing_both":
                missingBoth += 1
            elif status == "no_src_map":
                noSrcMap += 1
            elif status == "no_src_type":
                noSrcType += 1

    outParquet = inputParquet.with_name(f"{inputParquet.stem}{outputSuffix}.parquet")
    outCsv = inputParquet.with_name(f"{inputParquet.stem}{outputSuffix}.csv")
    outGpkg = inputParquet.with_name(f"{inputParquet.stem}{outputSuffix}.gpkg")

    gdf.to_parquet(outParquet, index=False)
    log.info("Wrote parquet: %s", outParquet)

    if writeCsv:
        gdf.drop(columns="geometry", errors="ignore").to_csv(outCsv, index=False)
        log.info("Wrote csv: %s", outCsv)

    if writeGpkg:
        gdf.to_file(outGpkg, layer="avaDirectory", driver="GPKG")
        log.info("Wrote gpkg: %s", outGpkg)

    log.info("Updated path cells: %d", updatedCells)
    log.info("Found clipped paths: %d", foundClipped)
    log.info("Found source paths: %d", foundSource)
    log.info("Missing clipped paths: %d", missingClipped)
    log.info("Missing source paths: %d", missingSource)
    log.info("Missing both: %d", missingBoth)
    log.info("Rows/types without source map: %d", noSrcMap)
    log.info("Rows/types without type in source map: %d", noSrcType)
    log.info("Skipped rel rows (left with empty path columns): %d", skippedRelRows)
    log.info("Done in %.2fs", time.perf_counter() - t0)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    main()