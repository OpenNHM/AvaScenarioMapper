#!/usr/bin/env python3
# runMakeAvaScenTif.py

import logging
import math
import os
import sys
import time
from pathlib import Path

import geopandas as gpd
import numpy as np
import pyarrow.parquet as pq
import rasterio
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from rasterio.vrt import WarpedVRT
from rasterio.warp import transform_bounds
from rasterio.windows import Window
from rasterio.windows import from_bounds as windowFromBounds
from rasterio.windows import intersect as windowsIntersect


# ------------------ Environment ------------------ #

envPrefix = Path(sys.prefix)
os.environ["PROJ_LIB"] = str(envPrefix / "share/proj")
os.environ["GDAL_DATA"] = str(envPrefix / "share/gdal")
os.environ["PROJ_NETWORK"] = "OFF"
os.environ["GDAL_PAM_ENABLED"] = "NO"


# ------------------ CFG ------------------ #

inputTable = Path(
    "/media/christoph/SSD 500 GB/Cairos/ModelChainResults/Euregio/cairosAvaMaps/13_avaScenMaps/"
    "pilotStubai/FruehlingWet/avaScen_pilotStubai_FruehlingWet.parquet"
)

dataRoot = Path(
    "/media/christoph/SSD 500 GB/Cairos/ModelChainResults/Euregio/cairosAvaMaps"
)

outDir = Path(
    "/media/christoph/SSD 500 GB/Cairos/ModelChainResults/Euregio/"
    "cairosAvaMaps/13_avaScenMaps/pilotStubai/FruehlingWet"
)

mapType = "all"
# mapType = "pathTravelanglemax"
# mapType = "pathZdelta_sized"
# mapType = "pathCellcounts,pathZdelta"

resolution = 10.0
noData = -9999.0

extent = None
# extent = (658800, 5203800, 661000, 5212300)

relTo100 = False
ignoreZero = False
logLevel = "INFO"


# ------------------ Constants ------------------ #

log = logging.getLogger(__name__)

pathColumns = [
    "pathCellcounts",
    "pathTravelanglemax",
    "pathTravelanglemax_sized",
    "pathTravellengthmax",
    "pathTravellengthmax_sized",
    "pathZdelta",
    "pathZdelta_sized",
]

# pathInputpra is intentionally excluded
minMergeColumns = {"pathTravelanglemax"}

knownDataFolders = [
    "11_avaDirectoryData",
    "12_avaScenFiles",
    "13_avaScenMaps",
]


# ------------------ Helpers ------------------ #

def setupLogging():
    logging.basicConfig(
        level=getattr(logging, logLevel.upper(), logging.INFO),
        format="%(levelname)s:%(name)s: %(message)s",
    )


def normalizeMapType(mapTypeValue):
    if mapTypeValue == "all":
        return pathColumns.copy()

    selected = [item.strip() for item in mapTypeValue.split(",") if item.strip()]
    invalid = [item for item in selected if item not in pathColumns]
    if invalid:
        raise ValueError(f"Invalid mapType: {invalid}")

    return selected


def snapBounds(bounds, cellSize):
    xmin, ymin, xmax, ymax = bounds
    xmin = math.floor(xmin / cellSize) * cellSize
    ymin = math.floor(ymin / cellSize) * cellSize
    xmax = math.ceil(xmax / cellSize) * cellSize
    ymax = math.ceil(ymax / cellSize) * cellSize
    return xmin, ymin, xmax, ymax


def resolveRasterPath(pathValue, baseDir):
    tifPath = Path(str(pathValue).strip())

    if tifPath.is_absolute():
        return tifPath.resolve()

    # 1) try relative to table location
    p1 = (baseDir / tifPath).resolve()
    if p1.exists():
        return p1

    # 2) try rebuilding from dataRoot at known folder anchor
    parts = tifPath.parts
    for folderName in knownDataFolders:
        if folderName in parts:
            folderIdx = parts.index(folderName)
            rebuilt = dataRoot.joinpath(*parts[folderIdx:]).resolve()
            if rebuilt.exists():
                return rebuilt

    # 3) try stripping ../ and joining remaining parts to dataRoot
    strippedParts = [part for part in parts if part not in ("..", ".", "")]
    if strippedParts:
        p3 = dataRoot.joinpath(*strippedParts).resolve()
        if p3.exists():
            return p3

    # fallback for debugging
    return p1


def readInputTable(tablePath, selectedColumns):
    suffix = tablePath.suffix.lower()
    readColumns = selectedColumns.copy()

    if suffix == ".parquet":
        schema = pq.read_schema(tablePath)
        available = set(schema.names)
        if "modType" in available:
            readColumns.append("modType")
        table = pq.read_table(tablePath, columns=readColumns)
        df = table.to_pandas()

    elif suffix == ".gpkg":
        gdf = gpd.read_file(tablePath)
        keepColumns = [col for col in readColumns if col in gdf.columns]
        if "modType" in gdf.columns and "modType" not in keepColumns:
            keepColumns.append("modType")
        df = gdf[keepColumns].copy()

    else:
        raise ValueError("inputTable must be .parquet or .gpkg")

    return df


def collectRasterItems(tablePath, selectedColumns):
    df = readInputTable(tablePath, selectedColumns)
    baseDir = tablePath.parent

    items = []
    seen = set()

    for _, row in df.iterrows():
        modTypeValue = ""
        if "modType" in df.columns and row.get("modType") is not None:
            modTypeValue = str(row["modType"]).strip().lower()

        for colName in selectedColumns:
            if colName not in df.columns:
                continue

            value = row[colName]
            if value is None:
                continue

            valueStr = str(value).strip()
            if valueStr == "" or valueStr.lower() == "nan":
                continue

            tifPath = resolveRasterPath(valueStr, baseDir)
            key = (str(tifPath), colName, modTypeValue)

            if key in seen:
                continue
            seen.add(key)

            items.append(
                {
                    "path": tifPath,
                    "column": colName,
                    "modType": modTypeValue,
                    "rawPath": valueStr,
                }
            )

    return items


def getTargetGrid(items, cellSize, fixedExtent):
    targetCrs = None
    unionBounds = None

    for item in items:
        tifPath = item["path"]
        if not tifPath.exists():
            continue

        with rasterio.open(tifPath) as src:
            if targetCrs is None:
                targetCrs = src.crs

            srcBounds = src.bounds
            if src.crs != targetCrs:
                srcBounds = transform_bounds(src.crs, targetCrs, *srcBounds, densify_pts=21)

            if unionBounds is None:
                unionBounds = srcBounds
            else:
                unionBounds = (
                    min(unionBounds[0], srcBounds[0]),
                    min(unionBounds[1], srcBounds[1]),
                    max(unionBounds[2], srcBounds[2]),
                    max(unionBounds[3], srcBounds[3]),
                )

    if targetCrs is None or unionBounds is None:
        raise FileNotFoundError("No valid input TIFFs found.")

    outBounds = fixedExtent if fixedExtent is not None else unionBounds
    outBounds = snapBounds(outBounds, cellSize)

    xmin, ymin, xmax, ymax = outBounds
    width = int(round((xmax - xmin) / cellSize))
    height = int(round((ymax - ymin) / cellSize))

    if width <= 0 or height <= 0:
        raise ValueError("Invalid output extent.")

    return {
        "crs": targetCrs,
        "bounds": outBounds,
        "transform": from_origin(xmin, ymax, cellSize, cellSize),
        "width": width,
        "height": height,
    }


def buildProfile(grid):
    return {
        "driver": "GTiff",
        "dtype": "float32",
        "nodata": noData,
        "count": 1,
        "width": grid["width"],
        "height": grid["height"],
        "crs": grid["crs"],
        "transform": grid["transform"],
        "compress": "LZW",
        "tiled": True,
        "blockxsize": 512,
        "blockysize": 512,
        "bigtiff": "IF_SAFER",
    }


def fillNoData(dst):
    block = np.full((512, 512), noData, dtype=np.float32)

    for _, window in dst.block_windows(1):
        h = int(window.height)
        w = int(window.width)
        dst.write(block[:h, :w], 1, window=window)


def buildOutputName(columnName):
    return f"{inputTable.stem}_{columnName}.tif"


def mergeOneRaster(item, dst):
    tifPath = item["path"]
    mergeMode = "min" if item["column"] in minMergeColumns else "max"

    with rasterio.open(tifPath) as src:
        srcBounds = src.bounds
        if src.crs != dst.crs:
            srcBounds = transform_bounds(src.crs, dst.crs, *srcBounds, densify_pts=21)

        dstFullWindow = windowFromBounds(*srcBounds, transform=dst.transform)
        dstFullWindow = dstFullWindow.round_offsets().round_lengths()

        fullRasterWindow = Window(0, 0, dst.width, dst.height)
        if not windowsIntersect(dstFullWindow, fullRasterWindow):
            return

        colOff = max(0, int(dstFullWindow.col_off))
        rowOff = max(0, int(dstFullWindow.row_off))
        colMax = min(dst.width, int(dstFullWindow.col_off + dstFullWindow.width))
        rowMax = min(dst.height, int(dstFullWindow.row_off + dstFullWindow.height))

        width = colMax - colOff
        height = rowMax - rowOff

        if width <= 0 or height <= 0:
            return

        dstWindow = Window(colOff, rowOff, width, height)
        dstTransform = rasterio.windows.transform(dstWindow, dst.transform)

        with WarpedVRT(
            src,
            crs=dst.crs,
            transform=dstTransform,
            width=width,
            height=height,
            resampling=Resampling.nearest,
            nodata=noData,
        ) as vrt:
            srcArray = vrt.read(1)

        validMask = np.isfinite(srcArray) & (srcArray != noData)
        if ignoreZero:
            validMask &= (srcArray != 0)

        if not np.any(validMask):
            return

        srcArray = srcArray.astype(np.float32, copy=False)

        if relTo100 and item["modType"] == "rel":
            srcArray = srcArray.copy()
            srcArray[validMask] = 100.0

        dstArray = dst.read(1, window=dstWindow)

        if mergeMode == "min":
            replaceMask = validMask & ((dstArray == noData) | (srcArray < dstArray))
        else:
            replaceMask = validMask & ((dstArray == noData) | (srcArray > dstArray))

        if not np.any(replaceMask):
            return

        dstArray = dstArray.astype(np.float32, copy=False)
        dstArray[replaceMask] = srcArray[replaceMask]
        dst.write(dstArray, 1, window=dstWindow)


# ------------------ Main ------------------ #

def runMakeAvaScenTif():
    t0 = time.perf_counter()

    selectedColumns = normalizeMapType(mapType)

    log.info("Step 13: Start avalanche scenario TIFF merge...")
    log.info("Step 13: Input table = %s", inputTable)
    log.info("Step 13: Data root = %s", dataRoot)
    log.info("Step 13: Output directory = %s", outDir)
    log.info("Step 13: Selected raster columns = %s", ", ".join(selectedColumns))

    items = collectRasterItems(inputTable, selectedColumns)
    if not items:
        raise ValueError("No raster paths found in input table.")

    existingItems = [item for item in items if item["path"].exists()]
    missingItems = [item for item in items if not item["path"].exists()]

    log.info("Step 13: Raster paths in table = %d", len(items))
    log.info("Step 13: Existing raster paths = %d", len(existingItems))

    if missingItems:
        log.warning("Step 13: Missing raster paths skipped = %d", len(missingItems))
        for item in missingItems[:10]:
            log.warning("Missing raw=%s", item["rawPath"])
            log.warning("Missing resolved=%s", item["path"])

    if not existingItems:
        raise FileNotFoundError("None of the raster paths from input table exist on disk.")

    grid = getTargetGrid(existingItems, resolution, extent)
    profile = buildProfile(grid)

    log.info(
        "Step 13: Output grid bounds=(%.2f, %.2f, %.2f, %.2f), size=%d x %d",
        grid["bounds"][0],
        grid["bounds"][1],
        grid["bounds"][2],
        grid["bounds"][3],
        grid["width"],
        grid["height"],
    )

    outDir.mkdir(parents=True, exist_ok=True)

    for columnName in selectedColumns:
        columnItems = [item for item in existingItems if item["column"] == columnName]
        if not columnItems:
            log.warning("Step 13: No input rasters found for %s", columnName)
            continue

        mergeMode = "minimum" if columnName in minMergeColumns else "maximum"
        outPath = outDir / buildOutputName(columnName)

        log.info(
            "Step 13: Start %s with %s merge (%d rasters)",
            columnName,
            mergeMode,
            len(columnItems),
        )

        with rasterio.open(outPath, "w", **profile) as dst:
            fillNoData(dst)

        with rasterio.open(outPath, "r+") as dst:
            for idx, item in enumerate(columnItems, start=1):
                try:
                    mergeOneRaster(item, dst)

                    if idx % 1000 == 0 or idx == len(columnItems):
                        log.info(
                            "Step 13: %s merged %d / %d rasters",
                            columnName,
                            idx,
                            len(columnItems),
                        )

                except Exception:
                    log.exception("Step 13: Exception during processing")

        log.info("Step 13: Finished %s -> %s", columnName, outPath)

    log.info("Step 13: Finished in %.2fs", time.perf_counter() - t0)


if __name__ == "__main__":
    setupLogging()
    runMakeAvaScenTif()