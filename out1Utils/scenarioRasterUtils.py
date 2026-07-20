"""Create merged raster products for filtered avalanche scenarios."""

import logging
import math
from pathlib import Path
from typing import Optional, Sequence

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from rasterio.vrt import WarpedVRT
from rasterio.warp import transform_bounds
from rasterio.windows import Window
from rasterio.windows import from_bounds as windowFromBounds
from rasterio.windows import intersect as windowsIntersect

import in1Utils.mapperUtils as mapperUtils

log = logging.getLogger(__name__)

PATH_COLUMNS = [
    "pathCellcounts",
    "pathTravelanglemax",
    "pathTravelanglemax_sized",
    "pathTravellengthmax",
    "pathTravellengthmax_sized",
    "pathZdelta",
    "pathZdelta_sized",
]
MIN_MERGE_COLUMNS = {"pathTravelanglemax"}


def parseMapTypes(raw: str) -> list[str]:
    """Parse and validate configured raster path columns."""
    if str(raw).strip().lower() == "all":
        return PATH_COLUMNS.copy()

    selected = [item.strip() for item in str(raw).split(",") if item.strip()]
    invalid = [item for item in selected if item not in PATH_COLUMNS]
    if invalid:
        raise ValueError(f"Invalid SCENARIORASTERS.mapTypes: {invalid}")
    if not selected:
        raise ValueError("SCENARIORASTERS.mapTypes is empty.")
    return list(dict.fromkeys(selected))


def parseExtent(raw: str) -> Optional[tuple[float, float, float, float]]:
    """Parse optional xmin,ymin,xmax,ymax output extent."""
    raw = str(raw or "").strip()
    if not raw:
        return None
    values = [float(value.strip()) for value in raw.split(",")]
    if len(values) != 4:
        raise ValueError("SCENARIORASTERS.extent requires xmin,ymin,xmax,ymax.")
    xmin, ymin, xmax, ymax = values
    if xmin >= xmax or ymin >= ymax:
        raise ValueError("SCENARIORASTERS.extent has invalid bounds.")
    return xmin, ymin, xmax, ymax


def parseRasterConfig(cfg) -> dict:
    """Read scenario-raster settings with stable defaults."""
    section = "SCENARIORASTERS"
    resolution = cfg.getfloat(section, "resolution", fallback=10.0)
    if resolution <= 0:
        raise ValueError("SCENARIORASTERS.resolution must be positive.")

    return {
        "mapTypes": parseMapTypes(
            cfg.get(
                section,
                "mapTypes",
                fallback="pathZdelta,pathTravelanglemax,pathTravellengthmax",
            )
        ),
        "resolution": resolution,
        "nodata": cfg.getfloat(section, "nodata", fallback=-9999.0),
        "extent": parseExtent(cfg.get(section, "extent", fallback="")),
        "relTo100": cfg.getboolean(section, "relTo100", fallback=False),
        "ignoreZero": cfg.getboolean(section, "ignoreZero", fallback=False),
        "overwrite": cfg.getboolean(section, "overwrite", fallback=False),
    }


def deriveDataRoot(avaResultsPath: Path) -> Path:
    """Derive the model-chain root from a flat or nested AvaDirectory path."""
    path = Path(avaResultsPath).resolve()
    for parent in (path.parent, *path.parents):
        if parent.name in {"12_avaDirectory", "13_avaScenMaps"}:
            return parent.parent
    return path.parent


def _collectRasterItems(
    scenarioGdf: gpd.GeoDataFrame,
    selectedColumns: Sequence[str],
    pathBaseDir: Path,
    dataRoot: Path,
) -> list[dict]:
    items = []
    seen = set()

    for _, row in scenarioGdf.iterrows():
        modType = str(row.get("modType", "")).strip().lower()
        for column in selectedColumns:
            if column not in scenarioGdf.columns:
                continue
            value = row.get(column)
            if value is None:
                continue
            valueText = str(value).strip()
            if not valueText or valueText.lower() in {"nan", "none", "<na>"}:
                continue

            rasterPath = mapperUtils.resolveRasterPath(
                valueText,
                pathBaseDir,
                dataRoot=dataRoot,
            )
            key = (str(rasterPath), column, modType)
            if key in seen:
                continue
            seen.add(key)
            items.append(
                {
                    "path": rasterPath,
                    "column": column,
                    "modType": modType,
                    "rawPath": valueText,
                }
            )

    return items


def _snapBounds(bounds, cellSize):
    xmin, ymin, xmax, ymax = bounds
    return (
        math.floor(xmin / cellSize) * cellSize,
        math.floor(ymin / cellSize) * cellSize,
        math.ceil(xmax / cellSize) * cellSize,
        math.ceil(ymax / cellSize) * cellSize,
    )


def _getTargetGrid(items, cellSize, fixedExtent):
    targetCrs = None
    unionBounds = None

    for item in items:
        with rasterio.open(item["path"]) as src:
            if targetCrs is None:
                targetCrs = src.crs
            bounds = src.bounds
            if src.crs != targetCrs:
                bounds = transform_bounds(src.crs, targetCrs, *bounds, densify_pts=21)
            if unionBounds is None:
                unionBounds = tuple(bounds)
            else:
                unionBounds = (
                    min(unionBounds[0], bounds[0]),
                    min(unionBounds[1], bounds[1]),
                    max(unionBounds[2], bounds[2]),
                    max(unionBounds[3], bounds[3]),
                )

    if targetCrs is None or unionBounds is None:
        raise FileNotFoundError("No valid input TIFFs found.")

    outputBounds = _snapBounds(fixedExtent or unionBounds, cellSize)
    xmin, ymin, xmax, ymax = outputBounds
    width = int(round((xmax - xmin) / cellSize))
    height = int(round((ymax - ymin) / cellSize))
    if width <= 0 or height <= 0:
        raise ValueError("Invalid scenario-raster output extent.")

    return {
        "crs": targetCrs,
        "bounds": outputBounds,
        "transform": from_origin(xmin, ymax, cellSize, cellSize),
        "width": width,
        "height": height,
    }


def _buildProfile(grid, nodata):
    return {
        "driver": "GTiff",
        "dtype": "float32",
        "nodata": nodata,
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


def _fillNodata(dst, nodata):
    block = np.full((512, 512), nodata, dtype=np.float32)
    for _, window in dst.block_windows(1):
        dst.write(block[: int(window.height), : int(window.width)], 1, window=window)


def _mergeOneRaster(item, dst, nodata, relTo100, ignoreZero):
    mergeMode = "min" if item["column"] in MIN_MERGE_COLUMNS else "max"
    with rasterio.open(item["path"]) as src:
        bounds = src.bounds
        if src.crs != dst.crs:
            bounds = transform_bounds(src.crs, dst.crs, *bounds, densify_pts=21)

        destinationWindow = windowFromBounds(*bounds, transform=dst.transform)
        destinationWindow = destinationWindow.round_offsets().round_lengths()
        fullWindow = Window(0, 0, dst.width, dst.height)
        if not windowsIntersect(destinationWindow, fullWindow):
            return

        colOff = max(0, int(destinationWindow.col_off))
        rowOff = max(0, int(destinationWindow.row_off))
        colMax = min(dst.width, int(destinationWindow.col_off + destinationWindow.width))
        rowMax = min(dst.height, int(destinationWindow.row_off + destinationWindow.height))
        width = colMax - colOff
        height = rowMax - rowOff
        if width <= 0 or height <= 0:
            return

        window = Window(colOff, rowOff, width, height)
        transform = rasterio.windows.transform(window, dst.transform)
        with WarpedVRT(
            src,
            crs=dst.crs,
            transform=transform,
            width=width,
            height=height,
            resampling=Resampling.nearest,
            nodata=nodata,
        ) as vrt:
            source = vrt.read(1)

        valid = np.isfinite(source) & (source != nodata)
        if ignoreZero:
            valid &= source != 0
        if not np.any(valid):
            return

        source = source.astype(np.float32, copy=False)
        if relTo100 and item["modType"] == "rel":
            source = source.copy()
            source[valid] = 100.0

        destination = dst.read(1, window=window)
        if mergeMode == "min":
            replace = valid & ((destination == nodata) | (source < destination))
        else:
            replace = valid & ((destination == nodata) | (source > destination))
        if np.any(replace):
            destination[replace] = source[replace]
            dst.write(destination, 1, window=window)


def makeScenarioRasters(
    scenarioGdf: gpd.GeoDataFrame,
    scenarioName: str,
    scenMapsDir: Path,
    pathBaseDir: Path,
    dataRoot: Path,
    rasterConfig: dict,
) -> list[Path]:
    """Write requested merged TIFFs into one directory per scenario."""
    outputDir = Path(scenMapsDir) / scenarioName
    outputDir.mkdir(parents=True, exist_ok=True)

    requestedOutputs = [
        outputDir / f"{scenarioName}_{column}.tif"
        for column in rasterConfig["mapTypes"]
    ]
    if not rasterConfig["overwrite"] and all(path.exists() for path in requestedOutputs):
        log.info("Scenario raster outputs already complete, skipping: %s", scenarioName)
        return []

    items = _collectRasterItems(
        scenarioGdf,
        rasterConfig["mapTypes"],
        Path(pathBaseDir),
        Path(dataRoot),
    )
    existingItems = [item for item in items if item["path"].is_file()]
    missingItems = [item for item in items if not item["path"].is_file()]
    if missingItems:
        log.warning("Scenario %s: missing raster paths=%d", scenarioName, len(missingItems))
        for item in missingItems[:10]:
            log.warning("Missing raster: %s", item["rawPath"])
    if not existingItems:
        raise FileNotFoundError(f"Scenario {scenarioName}: no valid input TIFFs found.")

    grid = _getTargetGrid(
        existingItems,
        rasterConfig["resolution"],
        rasterConfig["extent"],
    )
    profile = _buildProfile(grid, rasterConfig["nodata"])
    written = []

    log.info(
        "Scenario %s raster grid: bounds=%s size=%dx%d",
        scenarioName,
        grid["bounds"],
        grid["width"],
        grid["height"],
    )

    for column in rasterConfig["mapTypes"]:
        columnItems = [item for item in existingItems if item["column"] == column]
        if not columnItems:
            log.warning("Scenario %s: no rasters for %s", scenarioName, column)
            continue

        outputPath = outputDir / f"{scenarioName}_{column}.tif"
        if outputPath.exists() and not rasterConfig["overwrite"]:
            log.info("Scenario raster exists, skipping: %s", outputPath)
            continue

        with rasterio.open(outputPath, "w", **profile) as dst:
            _fillNodata(dst, rasterConfig["nodata"])
        with rasterio.open(outputPath, "r+") as dst:
            for item in columnItems:
                _mergeOneRaster(
                    item,
                    dst,
                    rasterConfig["nodata"],
                    rasterConfig["relTo100"],
                    rasterConfig["ignoreZero"],
                )

        written.append(outputPath)
        log.info("Wrote scenario raster: %s", outputPath)

    return written
