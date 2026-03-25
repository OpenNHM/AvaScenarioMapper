#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import logging
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import geopandas as gpd
import rasterio
from rasterio.mask import mask


log = logging.getLogger(__name__)


# ------------------ Input / output ------------------ #
inputParquet = Path(
    "/media/christoph/SSD 500 GB/Cairos/ModelChainResults/Euregio/cairosAvaMaps/12_avaDirectory/pilotSella/avaDirectoryResults_pilotSella.parquet"
)

clipOutRoot = Path(
    "/media/christoph/SSD 500 GB/Cairos/ModelChainResults/Euregio/cairosAvaMaps/11_avaDirectoryData/pilotSella/"
)

maxClipWorkers = min(16, (os.cpu_count() or 8))

logEverySec = 30
checkpointEveryDone = 50_000


# ------------------ FlowPy roots by region ------------------ #
# Keys must match values in the LKRegion column.
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

# Optional fallback if LKRegion is missing or not mapped.
useFallbackRoot = False
fallbackFlowPyBigDataRoot = Path(
    "/media/christoph/SSD 500 GB/Cairos/ModelChainResults/Euregio/NTirol/251023/"
    "alpha32_3_umax8_18_maxS5/09_flowPyBigDataStructure"
)


# ------------------ Subset handling ------------------ #
avaDirSubset = False
avaDirSubsetPath = Path("/home/christoph/Documents/JuliaMA/catchment_stubai_UTM32N.geojson")

# Allowed: "off", "mask", "clip"
subsetMode = "mask"

# Repair mode for an already-created old clipped subset parquet.
repairFromOldSubsetRun = False
oldSubsetParquet = Path(
    "/media/christoph/SSD 500 GB/Cairos/ModelChainResults/cairosAvaMaps/12_avaDirectory/pilotStubai/"
    "avaDirectoryResults_pilotStubaiExtended_clipped_subset_clip_catchment_stubai_UTM32N.parquet"
)


# ------------------ Raster patterns ------------------ #
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


# ------------------ Schema helpers ------------------ #
def _ensureSchema(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    if "praID" not in gdf.columns:
        raise ValueError("Missing required column: praID")
    if "resultID" not in gdf.columns:
        raise ValueError("Missing required column: resultID")
    if "LKRegion" not in gdf.columns:
        log.warning("No LKRegion column found")
    if gdf.geometry is None:
        raise ValueError("Missing geometry column")

    for col in typeToPathCol.values():
        if col not in gdf.columns:
            gdf[col] = None

    gdf["praID"] = pd.to_numeric(gdf["praID"], errors="coerce").astype("Int64")
    gdf["resultID"] = gdf["resultID"].astype("string")

    if "LKRegion" in gdf.columns:
        gdf["LKRegion"] = gdf["LKRegion"].astype("string").str.strip()

    before = len(gdf)
    gdf = gdf[gdf["praID"].notna() & gdf["resultID"].notna()].copy()
    dropped = before - len(gdf)
    if dropped:
        log.info("Dropped %d rows missing praID/resultID", dropped)

    return gdf


def _filterResOnlyAndDedup(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    if "modType" in gdf.columns:
        before = len(gdf)
        gdf = gdf[gdf["modType"] == "res"].copy()
        log.info("Filtered modType=res: %d -> %d rows", before, len(gdf))
    else:
        log.warning("No modType column found -> cannot filter rel/res (continuing unchanged)")

    before = len(gdf)
    dedupCols = ["praID", "resultID"]
    if "LKRegion" in gdf.columns:
        dedupCols.append("LKRegion")

    gdf = gdf.drop_duplicates(subset=dedupCols, keep="first").copy()
    dropped = before - len(gdf)
    if dropped:
        log.info("Dropped %d duplicate rows on %s", dropped, ",".join(dedupCols))

    return gdf


# ------------------ Subset helpers ------------------ #
def _validateSubsetMode(subsetModeLocal: str) -> str:
    allowed = {"off", "mask", "clip"}
    if subsetModeLocal not in allowed:
        raise ValueError(f"Invalid subsetMode: {subsetModeLocal}. Allowed: {sorted(allowed)}")
    return subsetModeLocal


def _loadSubsetUnion(subsetPath: Path, targetCrs) -> object:
    if not subsetPath.exists():
        raise FileNotFoundError(subsetPath)

    subsetGdf = gpd.read_file(subsetPath)

    if subsetGdf.empty:
        raise ValueError(f"Subset file is empty: {subsetPath}")
    if subsetGdf.geometry is None:
        raise ValueError(f"Subset file has no geometry: {subsetPath}")
    if targetCrs is None:
        raise ValueError("Input parquet has no CRS; cannot subset safely")
    if subsetGdf.crs is None:
        raise ValueError(f"Subset file has no CRS: {subsetPath}")

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

    dropped = before - len(gdf)
    log.info("Subset mask: %d -> %d rows", before, len(gdf))
    if dropped:
        log.info("Subset removed %d non-intersecting rows", dropped)

    return gdf


def _applySubsetClip(gdf: gpd.GeoDataFrame, subsetUnion) -> gpd.GeoDataFrame:
    before = len(gdf)
    gdf = gdf[gdf.geometry.notna()].copy()
    gdf = gdf[~gdf.geometry.is_empty].copy()
    gdf = gdf[gdf.geometry.intersects(subsetUnion)].copy()

    dropped = before - len(gdf)
    log.info("Subset clip filter: %d -> %d rows", before, len(gdf))
    if dropped:
        log.info("Subset removed %d non-intersecting rows", dropped)

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
        log.info("Subset disabled")
        return gdf

    log.info("Subset enabled: %s", subsetPath)
    log.info("Subset mode: %s", subsetModeLocal)

    subsetUnion = _loadSubsetUnion(subsetPath, gdf.crs)

    if subsetModeLocal == "mask":
        return _applySubsetMask(gdf, subsetUnion)

    if subsetModeLocal == "clip":
        return _applySubsetClip(gdf, subsetUnion)

    raise ValueError(f"Unhandled subsetMode: {subsetModeLocal}")


# ------------------ Repair helpers ------------------ #
def _prepareRepairFromOldSubsetRun(
    fullGdf: gpd.GeoDataFrame,
    subsetPath: Path,
    oldSubsetPath: Path,
) -> gpd.GeoDataFrame:
    if not oldSubsetPath.exists():
        raise FileNotFoundError(oldSubsetPath)

    log.info("Repair mode enabled")
    log.info("Old subset parquet: %s", oldSubsetPath)

    subsetUnion = _loadSubsetUnion(subsetPath, fullGdf.crs)

    oldGdf = gpd.read_parquet(oldSubsetPath)
    oldGdf = _ensureSchema(oldGdf)
    oldGdf = _filterResOnlyAndDedup(oldGdf)

    oldBefore = len(oldGdf)
    oldGdf = oldGdf[oldGdf.geometry.notna()].copy()
    oldGdf = oldGdf[~oldGdf.geometry.is_empty].copy()
    oldGdf = oldGdf[oldGdf.geometry.intersects(subsetUnion)].copy()
    log.info("Repair old subset filtered by mask rule: %d -> %d rows", oldBefore, len(oldGdf))

    if oldGdf.empty:
        return oldGdf

    keyCols = ["praID", "resultID"]
    if "LKRegion" in fullGdf.columns and "LKRegion" in oldGdf.columns:
        keyCols.append("LKRegion")

    oldKeep = oldGdf[keyCols].copy()
    oldKeep["_oldGeometry"] = oldGdf.geometry

    for col in typeToPathCol.values():
        oldKeep[f"old_{col}"] = oldGdf[col]

    fullGdf = fullGdf.merge(oldKeep, on=keyCols, how="inner")
    log.info("Repair mode selected %d rows from full input", len(fullGdf))

    if fullGdf.empty:
        return fullGdf

    oldGeom = gpd.GeoSeries(fullGdf["_oldGeometry"], crs=fullGdf.crs)
    newGeom = gpd.GeoSeries(fullGdf.geometry, crs=fullGdf.crs)

    fullGdf["_needsReclip"] = ~newGeom.geom_equals(oldGeom)

    changed = int(fullGdf["_needsReclip"].sum())
    unchanged = len(fullGdf) - changed
    log.info("Repair geometry compare: unchanged=%d | needsReclip=%d", unchanged, changed)

    for col in typeToPathCol.values():
        oldCol = f"old_{col}"
        fullGdf.loc[~fullGdf["_needsReclip"], col] = fullGdf.loc[~fullGdf["_needsReclip"], oldCol]
        fullGdf.loc[fullGdf["_needsReclip"], col] = None

    dropCols = ["_oldGeometry"] + [f"old_{col}" for col in typeToPathCol.values()]
    fullGdf = fullGdf.drop(columns=dropCols, errors="ignore")

    return fullGdf


# ------------------ Region helpers ------------------ #
def _normalizeRegion(value) -> str | None:
    if pd.isna(value):
        return None
    text = str(value).strip()
    return text if text else None


def _validateFlowPyRoots(rootsByRegion: dict[str, Path]) -> dict[str, Path]:
    validRoots: dict[str, Path] = {}

    for regionName, rootPath in rootsByRegion.items():
        if not rootPath.exists():
            log.warning("FlowPy root for region %s does not exist: %s", regionName, rootPath)
            continue
        validRoots[regionName] = rootPath

    if useFallbackRoot and not fallbackFlowPyBigDataRoot.exists():
        raise FileNotFoundError(fallbackFlowPyBigDataRoot)

    if not validRoots and not useFallbackRoot:
        raise ValueError("No valid flowPyBigDataRoots found")

    return validRoots


def _resolveRegionRoot(regionName: str | None, rootsByRegion: dict[str, Path]) -> Path | None:
    if regionName in rootsByRegion:
        return rootsByRegion[regionName]

    if useFallbackRoot:
        return fallbackFlowPyBigDataRoot

    return None


# ------------------ Raster source helpers ------------------ #
def _buildResDirIndices(root: Path) -> tuple[dict[str, Path], dict[str, Path]]:
    t0 = time.perf_counter()
    peakIndex: dict[str, Path] = {}
    sizeIndex: dict[str, Path] = {}

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


def _pickLatestBySuffix(folder: Path, suffix: str) -> Path | None:
    candidates = sorted(folder.glob(f"*{suffix}"))
    if not candidates:
        return None

    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return candidates[0]


def _findRelAreaRaster(resDir: Path) -> Path | None:
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


def _buildRidFileCacheForRoot(
    peakIndex: dict[str, Path],
    sizeIndex: dict[str, Path],
) -> dict[str, dict[str, Path]]:
    t0 = time.perf_counter()
    cache: dict[str, dict[str, Path]] = {}
    allRids = set(peakIndex.keys()) | set(sizeIndex.keys())

    for rid in allRids:
        peakResDir = peakIndex.get(rid)
        sizeResDir = sizeIndex.get(rid)
        entry: dict[str, Path] = {}

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

    log.info("Built rid→file cache for %d rids in %.2fs", len(cache), time.perf_counter() - t0)
    return cache


def _buildRegionCaches(
    rootsByRegion: dict[str, Path],
) -> dict[str, dict[str, dict[str, Path]]]:
    regionCaches: dict[str, dict[str, dict[str, Path]]] = {}

    for regionName, rootPath in rootsByRegion.items():
        log.info("Build cache for region %s: %s", regionName, rootPath)
        peakIndex, sizeIndex = _buildResDirIndices(rootPath)

        if not peakIndex and not sizeIndex:
            log.warning("No peakFiles/sizeFiles res_* folders found under %s", rootPath)
            regionCaches[regionName] = {}
            continue

        regionCaches[regionName] = _buildRidFileCacheForRoot(peakIndex, sizeIndex)

    if useFallbackRoot:
        log.info("Build cache for fallback root: %s", fallbackFlowPyBigDataRoot)
        peakIndex, sizeIndex = _buildResDirIndices(fallbackFlowPyBigDataRoot)
        regionCaches["__fallback__"] = _buildRidFileCacheForRoot(peakIndex, sizeIndex)

    return regionCaches


def _getRidSourceMap(
    row,
    regionCaches: dict[str, dict[str, dict[str, Path]]],
    rootsByRegion: dict[str, Path],
) -> dict[str, Path] | None:
    regionName = _normalizeRegion(row["LKRegion"]) if "LKRegion" in row.index else None

    if regionName in regionCaches:
        return regionCaches[regionName].get(str(row["resultID"]))

    if useFallbackRoot and "__fallback__" in regionCaches:
        return regionCaches["__fallback__"].get(str(row["resultID"]))

    return None


# ------------------ Resume / audit helpers ------------------ #
def _expectedOutPath(outRoot: Path, praId: int, rid: str, srcPath: Path) -> Path:
    outDir = outRoot / f"com4_{rid}"
    outName = f"praID{praId}_{srcPath.name}"
    return outDir / outName


def _auditExistingOutputsForRow(
    row,
    srcMap: dict[str, Path] | None,
    outRoot: Path,
    outBaseDir: Path,
) -> tuple[dict[str, str], bool]:
    """
    Check whether expected clipped rasters for this row already exist on disk.
    """
    if not srcMap:
        return {}, False

    praId = int(row["praID"])
    rid = str(row["resultID"])

    foundRelPaths: dict[str, str] = {}
    missingAny = False

    for typeKey, srcPath in srcMap.items():
        outPath = _expectedOutPath(outRoot, praId, rid, srcPath)

        if outPath.exists():
            foundRelPaths[typeToPathCol[typeKey]] = os.path.relpath(outPath, start=outBaseDir)
        else:
            missingAny = True

    rowComplete = (not missingAny) and (len(foundRelPaths) == len(srcMap))
    return foundRelPaths, rowComplete


def _auditExistingOutputs(
    gdf: gpd.GeoDataFrame,
    regionCaches: dict[str, dict[str, dict[str, Path]]],
    rootsByRegion: dict[str, Path],
    outRoot: Path,
    outBaseDir: Path,
) -> tuple[gpd.GeoDataFrame, int, int]:
    """
    Rebuild missing path columns from already-clipped files on disk.
    """
    restoredRows = 0
    completeRows = 0

    for i, row in gdf.iterrows():
        srcMap = _getRidSourceMap(row, regionCaches, rootsByRegion)
        foundRelPaths, rowComplete = _auditExistingOutputsForRow(row, srcMap, outRoot, outBaseDir)

        if foundRelPaths:
            for colName, relPathValue in foundRelPaths.items():
                gdf.at[i, colName] = relPathValue
            restoredRows += 1

        if rowComplete:
            completeRows += 1

    return gdf, restoredRows, completeRows


def _isFinalOutputComplete(
    gdf: gpd.GeoDataFrame,
    regionCaches: dict[str, dict[str, dict[str, Path]]],
    rootsByRegion: dict[str, Path],
    outRoot: Path,
    outBaseDir: Path,
) -> bool:
    if gdf.empty:
        return True

    for _, row in gdf.iterrows():
        srcMap = _getRidSourceMap(row, regionCaches, rootsByRegion)
        _, rowComplete = _auditExistingOutputsForRow(row, srcMap, outRoot, outBaseDir)
        if not rowComplete:
            return False

    return True


# ------------------ Raster clipping helpers ------------------ #
def _writeTifAtomic(outPath: Path, data, meta: dict) -> None:
    outPath.parent.mkdir(parents=True, exist_ok=True)
    tmpPath = outPath.with_suffix(outPath.suffix + ".tmp")

    if tmpPath.exists():
        try:
            tmpPath.unlink()
        except Exception:
            pass

    with rasterio.open(tmpPath, "w", **meta) as dst:
        dst.write(data)

    os.replace(tmpPath, outPath)


def _clipRaster(srcPath: Path, geomGeoJson: dict, outPath: Path, overwrite: bool = False) -> bool:
    if outPath.exists() and not overwrite:
        return True

    try:
        if overwrite and outPath.exists():
            try:
                outPath.unlink()
            except Exception:
                log.warning("Could not remove existing file before overwrite: %s", outPath)

        with rasterio.Env(GDAL_CACHEMAX=512):
            with rasterio.open(srcPath) as src:
                outImg, outTransform = mask(src, [geomGeoJson], crop=True)
                outMeta = src.meta.copy()
                outMeta.update(
                    driver="GTiff",
                    height=outImg.shape[1],
                    width=outImg.shape[2],
                    transform=outTransform,
                )

                if outPath.name.endswith("_lzw.tif"):
                    outMeta.update(compress="LZW")

                _writeTifAtomic(outPath, outImg, outMeta)

        return True

    except Exception:
        log.exception("Clip failed: %s -> %s", srcPath, outPath)
        try:
            tmpPath = outPath.with_suffix(outPath.suffix + ".tmp")
            if tmpPath.exists():
                tmpPath.unlink()
        except Exception:
            pass
        return False


# ------------------ Output helpers ------------------ #
def _writeResults(gdf: gpd.GeoDataFrame, outParquet: Path, outCsv: Path, outGpkg: Path) -> None:
    cleanGdf = gdf.drop(columns=["_needsReclip"], errors="ignore").copy()

    cleanGdf.to_parquet(outParquet, index=False)
    log.info("Wrote parquet: %s", outParquet)

    cleanGdf.drop(columns="geometry", errors="ignore").to_csv(outCsv, index=False)
    log.info("Wrote csv : %s", outCsv)

    cleanGdf.to_file(outGpkg, layer="avaScenClip", driver="GPKG")
    log.info("Wrote gpkg : %s", outGpkg)


def _buildOutputPaths(
    inputParquetLocal: Path,
    subsetEnabled: bool,
    subsetPath: Path,
    subsetModeLocal: str,
) -> tuple[Path, Path, Path, Path]:
    suffix = ""

    if subsetEnabled and subsetModeLocal != "off":
        suffix = f"_subset_{subsetModeLocal}_{subsetPath.stem}"

    outParquet = inputParquetLocal.with_name(f"{inputParquetLocal.stem}_clipped{suffix}.parquet")
    outCsv = inputParquetLocal.with_name(f"{inputParquetLocal.stem}_clipped{suffix}.csv")
    outGpkg = inputParquetLocal.with_name(f"{inputParquetLocal.stem}_clipped{suffix}.gpkg")
    outPartial = inputParquetLocal.with_name(f"{inputParquetLocal.stem}_clipped{suffix}_partial.parquet")

    return outParquet, outCsv, outGpkg, outPartial


# ------------------ Main ------------------ #
def main() -> int:
    subsetModeLocal = "off"
    if avaDirSubset:
        subsetModeLocal = _validateSubsetMode(subsetMode)

    rootsByRegion = _validateFlowPyRoots(flowPyBigDataRoots)

    log.info("runClipPilotRegion started")
    log.info("Input parquet: %s", inputParquet)
    log.info("Clip out root: %s", clipOutRoot)
    log.info("maxClipWorkers: %d", maxClipWorkers)
    log.info("avaDirSubset: %s", avaDirSubset)
    log.info("subsetMode: %s", subsetModeLocal)
    log.info("repairFromOldSubsetRun: %s", repairFromOldSubsetRun)
    log.info("Configured FlowPy regions: %s", ", ".join(sorted(rootsByRegion.keys())))

    if avaDirSubset:
        log.info("avaDirSubsetPath: %s", avaDirSubsetPath)
    if repairFromOldSubsetRun:
        log.info("oldSubsetParquet: %s", oldSubsetParquet)

    if not inputParquet.exists():
        raise FileNotFoundError(inputParquet)

    clipOutRoot.mkdir(parents=True, exist_ok=True)

    outParquet, outCsv, outGpkg, outPartial = _buildOutputPaths(
        inputParquet,
        avaDirSubset,
        avaDirSubsetPath,
        subsetModeLocal,
    )

    log.info("Output parquet: %s", outParquet)
    log.info("Output csv : %s", outCsv)
    log.info("Output gpkg : %s", outGpkg)
    log.info("Partial parquet (resume): %s", outPartial)

    # --- Build region caches early for audit and resume --- #
    regionCaches = _buildRegionCaches(rootsByRegion)

    if not any(regionCaches.values()):
        log.error("All region caches are empty -> nothing to clip.")
        return 2

    # --- Load existing state or start fresh --- #
    if outParquet.exists():
        log.info("Existing final parquet found: %s", outParquet)
        gdf = gpd.read_parquet(outParquet)
        gdf = _ensureSchema(gdf)
        gdf = _filterResOnlyAndDedup(gdf)

        gdf, restoredRows, completeRows = _auditExistingOutputs(
            gdf, regionCaches, rootsByRegion, clipOutRoot, outParquet.parent
        )
        log.info(
            "Existing final parquet audit: restoredRows=%d | completeRows=%d/%d",
            restoredRows,
            completeRows,
            len(gdf),
        )

        if _isFinalOutputComplete(gdf, regionCaches, rootsByRegion, clipOutRoot, outParquet.parent):
            log.info("Final parquet is complete and all clipped rasters exist. Nothing to do.")
            _writeResults(gdf, outParquet, outCsv, outGpkg)

            if outPartial.exists():
                try:
                    outPartial.unlink()
                    log.info("Removed stale partial parquet: %s", outPartial)
                except Exception:
                    log.warning("Could not remove stale partial parquet: %s", outPartial)

            return 0

        log.warning("Final parquet exists but is incomplete. Resuming audit/repair from it.")

    elif outPartial.exists():
        log.info("Resume detected: loading partial parquet...")
        gdf = gpd.read_parquet(outPartial)
        gdf = _ensureSchema(gdf)
        gdf = _filterResOnlyAndDedup(gdf)

        gdf, restoredRows, completeRows = _auditExistingOutputs(
            gdf, regionCaches, rootsByRegion, clipOutRoot, outParquet.parent
        )
        log.info(
            "Partial parquet audit: restoredRows=%d | completeRows=%d/%d",
            restoredRows,
            completeRows,
            len(gdf),
        )

    else:
        gdf = gpd.read_parquet(inputParquet)
        gdf = _ensureSchema(gdf)
        gdf = _filterResOnlyAndDedup(gdf)

        if repairFromOldSubsetRun:
            if not avaDirSubset:
                raise ValueError("repairFromOldSubsetRun requires avaDirSubset=True")
            if subsetModeLocal != "mask":
                raise ValueError("repairFromOldSubsetRun only supports subsetMode='mask'")
            gdf = _prepareRepairFromOldSubsetRun(gdf, avaDirSubsetPath, oldSubsetParquet)
        else:
            gdf = _applySubsetSelection(gdf, avaDirSubsetPath, subsetModeLocal)

        gdf, restoredRows, completeRows = _auditExistingOutputs(
            gdf, regionCaches, rootsByRegion, clipOutRoot, outParquet.parent
        )
        log.info(
            "Fresh-start audit: restoredRows=%d | completeRows=%d/%d",
            restoredRows,
            completeRows,
            len(gdf),
        )

    if "_needsReclip" not in gdf.columns:
        gdf["_needsReclip"] = False

    if gdf.empty:
        log.warning("No rows left after preprocessing/subset. Writing empty outputs.")
        _writeResults(gdf, outParquet, outCsv, outGpkg)
        if outPartial.exists():
            try:
                outPartial.unlink()
            except Exception:
                pass
        return 0

    if "LKRegion" in gdf.columns:
        regionCounts = gdf["LKRegion"].fillna("<NA>").value_counts(dropna=False)
        for regionName, count in regionCounts.items():
            log.info("Rows for LKRegion %s: %d", regionName, count)

        unknownRegions = sorted(
            {
                _normalizeRegion(val)
                for val in gdf["LKRegion"].dropna().unique()
                if _normalizeRegion(val) not in rootsByRegion
            }
        )
        if unknownRegions and not useFallbackRoot:
            raise ValueError(f"Unmapped LKRegion values found: {unknownRegions}")
        if unknownRegions:
            log.warning("Unmapped LKRegion values will use fallback root: %s", unknownRegions)

    jobs = []
    alreadyDone = 0
    alreadyDoneRows = 0
    missingRegionOrRid = 0

    for i, row in gdf.iterrows():
        srcMap = _getRidSourceMap(row, regionCaches, rootsByRegion)

        if not srcMap:
            missingRegionOrRid += 1
            continue

        foundRelPaths, rowComplete = _auditExistingOutputsForRow(
            row, srcMap, clipOutRoot, outParquet.parent
        )

        if foundRelPaths:
            for colName, relPathValue in foundRelPaths.items():
                gdf.at[i, colName] = relPathValue
            alreadyDone += len(foundRelPaths)

        if rowComplete and not bool(row.get("_needsReclip", False)):
            alreadyDoneRows += 1
            continue

        praId = int(row["praID"])
        rid = str(row["resultID"])
        geomGeoJson = row.geometry.__geo_interface__
        needsReclip = bool(row.get("_needsReclip", False))

        for typeKey, srcPath in srcMap.items():
            outPath = _expectedOutPath(clipOutRoot, praId, rid, srcPath)

            if outPath.exists() and not needsReclip:
                continue

            jobs.append((i, typeKey, srcPath, outPath, geomGeoJson, needsReclip))

    if missingRegionOrRid:
        log.warning(
            "Skipped %d rows with missing LKRegion mapping or missing resultID in region cache",
            missingRegionOrRid,
        )

    log.info(
        "Prepared %d clip jobs (to do) | %d already present (skipped files) | %d complete rows already finished",
        len(jobs),
        alreadyDone,
        alreadyDoneRows,
    )

    def _runJob(job):
        rowIndex, typeKey, srcPath, outPath, geomGeoJson, overwrite = job
        ok = _clipRaster(srcPath, geomGeoJson, outPath, overwrite=overwrite)
        return rowIndex, typeKey, str(outPath) if ok else None

    tStart = time.perf_counter()
    lastLog = tStart
    doneTotal = 0
    doneOk = 0
    doneFail = 0

    if jobs:
        with ThreadPoolExecutor(max_workers=maxClipWorkers) as executor:
            futures = [executor.submit(_runJob, job) for job in jobs]

            for future in as_completed(futures):
                rowIndex, typeKey, outAbs = future.result()
                doneTotal += 1

                if outAbs:
                    relPath = os.path.relpath(outAbs, start=outParquet.parent)
                    gdf.at[rowIndex, typeToPathCol[typeKey]] = relPath
                    doneOk += 1
                else:
                    doneFail += 1

                now = time.perf_counter()
                if now - lastLog >= logEverySec:
                    rate = doneTotal / max(1e-9, (now - tStart))
                    log.info(
                        "Progress: %d/%d done | ok=%d fail=%d | %.1f jobs/s",
                        doneTotal,
                        len(jobs),
                        doneOk,
                        doneFail,
                        rate,
                    )
                    lastLog = now

                if doneTotal % checkpointEveryDone == 0:
                    gdf.to_parquet(outPartial, index=False)
                    log.info("Checkpoint wrote: %s", outPartial)

    log.info(
        "Finished clipping. Jobs: %d | ok=%d fail=%d | skippedExisting=%d | skippedCompleteRows=%d",
        len(jobs),
        doneOk,
        doneFail,
        alreadyDone,
        alreadyDoneRows,
    )

    _writeResults(gdf, outParquet, outCsv, outGpkg)

    if outPartial.exists():
        try:
            outPartial.unlink()
            log.info("Removed partial parquet (success): %s", outPartial)
        except Exception:
            log.warning("Could not remove partial parquet: %s", outPartial)

    log.info("DONE")
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    raise SystemExit(main())