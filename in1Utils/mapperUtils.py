# --------------------------- in1Utils/mapperUtils.py --------------------------- #
#
# Purpose :
#   Shared utility functions for the Avalanche Scenario Mapper.
#
#   Provides common methods for path resolution, GeoDataFrame I/O,
#   attribute diagnostics, data normalization, scenario configuration
#   parsing, and shared output helpers consistent with the
#   Avalanche Scenario Model Chain conventions.
#
# Author :
#   Christoph Hesselbach
#
# Institution :
#   Austrian Research Centre for Forests (BFW)
#   Department of Natural Hazards | Snow and Avalanche Unit
#
# Date & Version :
#   2026-03 - 1.1
#
# ------------------------------------------------------------------------------ #

import os
import json
import logging
from pathlib import Path
from typing import Optional, Sequence, Iterable

import pandas as pd
import geopandas as gpd
import pyarrow.parquet as pq

from in1Utils.cfgUtils import relPath

log = logging.getLogger(__name__)


# ------------------ Small generic helpers ------------------ #

def sanitizeScenarioName(scenName: str) -> str:
    """
    Keep only alnum, dash, underscore. Never return empty.
    """
    scenName = str(scenName or "unnamed")
    scenNameClean = "".join(ch for ch in scenName if ch.isalnum() or ch in "-_")
    return scenNameClean or "unnamed"


def batched(seq: Sequence, batchSize: int) -> Iterable[Sequence]:
    """
    Yield sequence slices in fixed-size batches.
    """
    for i in range(0, len(seq), batchSize):
        yield seq[i:i + batchSize]


# ------------------ Path resolution ------------------ #

def deriveRegionName(baseDir: Path) -> str:
    """
    Derive a stable region name for master outputs.

    Priority:
      1) If path contains .../Euregio/<region>/...
      2) Else use baseDir.name
    """
    parts = list(baseDir.parts)
    if "Euregio" in parts:
        idx = parts.index("Euregio")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return baseDir.name


def getMasterName(cfg, baseDir: Path) -> str:
    """
    Master name format:
      avaScen_<Region><prefix>

    Example:
      avaScen_NTirol_report20260223
    """
    region = deriveRegionName(baseDir)
    prefix = cfg.get("WORKFLOW", "mapperMasterPrefix", fallback="").strip()
    return f"avaScen_{region}{prefix}"


def resolvePaths(cfg) -> dict:
    """Resolve the explicitly configured Mapper input and output paths."""
    mode = cfg.get("WORKFLOW", "mapperPathMode", fallback="customPaths").strip().lower()
    if mode != "custompaths":
        raise ValueError("Only mapperPathMode=customPaths is supported.")

    avaResultsRaw = cfg.get("PATHS", "avaDirectoryResults", fallback="").strip()
    scenMapsRaw = cfg.get("PATHS", "avaScenMapsDir", fallback="").strip()
    if not avaResultsRaw:
        raise ValueError("Missing [PATHS] avaDirectoryResults.")
    if not scenMapsRaw:
        raise ValueError("Missing [PATHS] avaScenMapsDir.")

    avaResultsPath = Path(os.path.expandvars(avaResultsRaw)).expanduser()
    scenMapsDir = Path(os.path.expandvars(scenMapsRaw)).expanduser()
    try:
        baseDir = Path(os.path.commonpath([str(avaResultsPath.parent), str(scenMapsDir)]))
    except ValueError:
        baseDir = scenMapsDir.parent

    paths = {
        "baseDir": baseDir,
        "avaDirectoryResultsParquet": avaResultsPath,
        "avaScenMapsDir": scenMapsDir,
    }

    if not paths["avaDirectoryResultsParquet"].is_file():
        raise FileNotFoundError(paths["avaDirectoryResultsParquet"])

    paths["avaScenMapsDir"].mkdir(parents=True, exist_ok=True)

    log.info("Resolved AvaDirectoryResults : %s", relPath(paths["avaDirectoryResultsParquet"], paths["baseDir"]))
    log.info("Resolved AvaScenMaps output  : %s", relPath(paths["avaScenMapsDir"], paths["baseDir"]))
    return paths


def resolveRasterPath(
    pathValue,
    relativeTo: Path,
    dataRoot: Optional[Path] = None,
) -> Path:
    """Resolve raster paths stored by both AvaDirectory layouts.

    Relative paths are first interpreted relative to the table containing them.
    For copied scenario tables, ``dataRoot`` can be supplied to rebuild paths
    from a known model-chain directory anchor.
    """
    rasterPath = Path(str(pathValue).strip())
    if rasterPath.is_absolute():
        return rasterPath.resolve()

    directPath = (relativeTo / rasterPath).resolve()
    if directPath.exists() or dataRoot is None:
        return directPath

    dataRoot = Path(dataRoot)
    knownAnchors = {"11_avaDirectoryData", "12_avaDirectory", "13_avaScenMaps"}
    parts = rasterPath.parts

    for index, part in enumerate(parts):
        if part in knownAnchors:
            anchoredPath = dataRoot.joinpath(*parts[index:]).resolve()
            if anchoredPath.exists():
                return anchoredPath

    strippedParts = [part for part in parts if part not in {"", ".", ".."}]
    if strippedParts:
        fallbackPath = dataRoot.joinpath(*strippedParts).resolve()
        if fallbackPath.exists():
            return fallbackPath

    return directPath


# ------------------ Output helpers ------------------ #

def parseDeleteColumns(cfg) -> list[str]:
    """
    Parse columns to delete from outputs.

    Current compatibility:
      reads from [WORKFLOW] deleteColumns
    """
    raw = cfg.get("WORKFLOW", "deleteColumns", fallback="").strip()
    if not raw:
        return []

    cols = [c.strip() for c in raw.split(",") if c.strip()]
    cols = list(dict.fromkeys(cols))

    protected = {"scenarioName", "geometry"}
    filtered = [c for c in cols if c not in protected]

    removed = [c for c in cols if c in protected]
    if removed:
        log.warning(
            "Ignoring protected column(s) in deleteColumns: %s",
            ", ".join(removed),
        )

    return filtered


def dropConfiguredColumns(
    gdf: gpd.GeoDataFrame,
    deleteCols: Sequence[str],
) -> gpd.GeoDataFrame:
    """
    Drop configured columns if present. Silent for missing columns.
    """
    if gdf is None or not deleteCols:
        return gdf

    existing = [c for c in deleteCols if c in gdf.columns]
    if existing:
        gdf = gdf.drop(columns=existing).copy()

    return gdf


def addScenarioColumns(
    gdf: gpd.GeoDataFrame,
    scenarioName: str,
) -> gpd.GeoDataFrame:
    """
    Add scenario-related output columns.
    """
    if gdf is None:
        return gdf

    gdf = gdf.copy()
    gdf["scenarioName"] = scenarioName
    return gdf


def prepareScenarioOutput(
    gdf: gpd.GeoDataFrame,
    scenarioName: str,
    deleteColumns: Sequence[str],
) -> gpd.GeoDataFrame:
    """
    Prepare final scenario output table:
      - drop configured removable columns
      - remove helper column 'scenario' if present
      - add scenarioName
    """
    if gdf is None or gdf.empty:
        return gdf

    gdf = dropConfiguredColumns(gdf, deleteColumns)
    gdf = gdf.drop(columns=["scenario"], errors="ignore")
    gdf = addScenarioColumns(gdf, scenarioName)
    return gdf


# ------------------ I/O Helpers ------------------ #

def readGdf(path: Path) -> gpd.GeoDataFrame:
    """
    Read GeoDataFrame from supported formats.
    """
    if not path.exists():
        log.error("Input file not found: %s", path)
        raise FileNotFoundError(path)

    suffix = path.suffix.lower()

    if suffix in {".geojson", ".gpkg"}:
        gdf = gpd.read_file(path)
    else:
        gdf = gpd.read_parquet(path)

    log.info("Loaded %d rows from %s", len(gdf), path.name)
    return gdf


def writeScenarioOutputs(
    filteredGdf,
    outParquet=None,
    outGeoJson=None,
    outGpkg=None,
    outCsv=None,
    csvWkt=False,
):
    """
    Save scenario or master results to one or more formats.

    - Parquet: main artifact
    - GeoJSON: optional
    - GPKG: GIS-friendly
    - CSV: optional, no geometry unless csvWkt=True
    """
    if filteredGdf is None or filteredGdf.empty:
        log.warning("No filtered results to write.")
        return

    for p in (outParquet, outGeoJson, outGpkg, outCsv):
        if p is not None:
            p.parent.mkdir(parents=True, exist_ok=True)
            break

    if outParquet is not None:
        try:
            filteredGdf.to_parquet(outParquet, index=False)
            log.info("Wrote Parquet: %s", outParquet.name)
        except Exception:
            log.exception("Failed to write Parquet for %s", outParquet)

    if outGeoJson is not None:
        try:
            filteredGdf.to_file(outGeoJson, driver="GeoJSON")
            log.info("Wrote GeoJSON: %s", outGeoJson.name)
        except Exception:
            log.warning("GeoJSON write warning for %s", outGeoJson.name)

    if outGpkg is not None:
        try:
            layerName = outGpkg.stem
            filteredGdf.to_file(outGpkg, layer=layerName, driver="GPKG")
            log.info("Wrote GPKG: %s (layer=%s)", outGpkg.name, layerName)
        except Exception:
            log.exception("Failed to write GPKG for %s", outGpkg)

    if outCsv is not None:
        try:
            if csvWkt:
                df = filteredGdf.copy()
                df["geometry"] = df.geometry.to_wkt()
                pd.DataFrame(df).to_csv(outCsv, index=False)
            else:
                df = pd.DataFrame(filteredGdf.drop(columns=["geometry"], errors="ignore"))
                df.to_csv(outCsv, index=False)

            log.info("Wrote CSV: %s (wkt=%s)", outCsv.name, csvWkt)
        except Exception:
            log.exception("Failed to write CSV for %s", outCsv)


def readGdfRowGroup(
    parquetPath: Path,
    rowGroup: int,
    dropCols: Optional[Sequence[str]] = None,
) -> gpd.GeoDataFrame:
    """
    Read a single GeoParquet row group as GeoDataFrame.

    Supports GeoParquet geometry encoding (WKB/WKT),
    preserves CRS if available, and can drop configured
    columns immediately after loading.
    """
    if not parquetPath.exists():
        log.error("Input file not found: %s", parquetPath)
        raise FileNotFoundError(parquetPath)

    pf = pq.ParquetFile(parquetPath)
    table = pf.read_row_group(rowGroup)

    metadata = table.schema.metadata or {}
    if b"geo" not in metadata:
        raise ValueError(f"Missing GeoParquet metadata in {parquetPath}")

    geo = json.loads(metadata[b"geo"].decode("utf-8"))
    geomCol = geo.get("primary_column", "geometry")
    geomMeta = geo.get("columns", {}).get(geomCol, {})
    encoding = str(geomMeta.get("encoding", "")).lower()

    df = table.to_pandas()

    if dropCols:
        colsToDrop = [c for c in dropCols if c in df.columns and c != geomCol]
        if colsToDrop:
            df = df.drop(columns=colsToDrop)

    if geomCol not in df.columns:
        raise ValueError(f"Geometry column '{geomCol}' not found in row group {rowGroup}")

    if encoding == "wkb":
        geometry = gpd.GeoSeries.from_wkb(df[geomCol], crs=None)
    elif encoding == "wkt":
        geometry = gpd.GeoSeries.from_wkt(df[geomCol], crs=None)
    else:
        raise ValueError(
            f"Unsupported geometry encoding '{encoding}' in {parquetPath}. "
            f"Expected 'WKB' or 'WKT'."
        )

    df = df.drop(columns=[geomCol])
    gdf = gpd.GeoDataFrame(df, geometry=geometry)

    crs = geomMeta.get("crs")
    if crs:
        gdf.set_crs(crs, inplace=True, allow_override=True)

    return gdf


# ------------------ Data integrity check ------------------ #

def checkInputData(gdf: gpd.GeoDataFrame, parquetPath: Path, cfg=None) -> bool:
    """
    Validate that the input avaDirectoryResults dataset contains
    all required columns.
    """
    requiredCols = [
        "praID", "resultID", "modType", "flow", "sector", "subC",
        "elevMin", "elevMax", "PPM", "PEM", "rSize",
        "LKGebietID", "LWDGebietID"
    ]

    log.info("Checking input data integrity for: %s", parquetPath.name)

    missing = [c for c in requiredCols if c not in gdf.columns]
    if missing:
        log.error("Missing required columns in %s: %s", parquetPath.name, ", ".join(missing))
        log.error("Cannot continue mapping — verify upstream outputs (Step 15).")
        printAvailableOptions(parquetPath)
        return False

    if gdf.empty:
        log.error("Input dataset %s is empty — nothing to process.", parquetPath.name)
        return False

    checkFlag = cfg and cfg.getboolean("WORKFLOW", "checkAvaDirResult", fallback=False)
    if checkFlag:
        printAvailableOptions(parquetPath)

    log.info(
        "Input data integrity check passed (%d rows, %d columns).",
        len(gdf), len(gdf.columns)
    )
    return True


# ------------------ Diagnostic Mode ------------------ #

def handleAvaDirCheckMode(cfg, parquetPath: Path) -> bool:
    """
    Diagnostic mode: list attributes and exit early if requested.
    """
    if not cfg.getboolean("WORKFLOW", "checkAvaDirResult", fallback=False):
        return True

    log.info("------------------------------------------------------------")
    log.info("Diagnostic mode enabled: checkAvaDirResult = True")
    log.info("Inspecting available attributes in AvaDirectoryResults...")
    printAvailableOptions(parquetPath)
    log.warning("------------------------------------------------------------")
    log.warning("Set your scenarios in avaScenMapperCfg.ini and run again!")
    log.warning("Mapper workflow terminated by user request.")
    log.info("------------------------------------------------------------")
    return False


# ------------------ Diagnostics ------------------ #

def printAvailableOptions(parquetPath: Path):
    """
    List available filterable attributes in avaDirectoryResults.parquet.
    """
    if not parquetPath.exists():
        log.warning("File not found for diagnostics: %s", parquetPath)
        return

    df = pd.read_parquet(parquetPath)
    log.info("Available attributes in: %s", parquetPath.name)

    for col in [
        "praAreaM", "praAreaSized", "praAreaVol", "praElevMin",
        "praElevMax", "praElevMean", "LKGebietID", "subC",
        "elevMin", "elevMax", "PPM", "PEM", "rSize"
    ]:
        if col in df.columns and df[col].notna().any():
            cmin, cmax = df[col].min(), df[col].max()
            log.info("   %-15s: %.0f → %.0f", col, cmin, cmax)


# ------------------ Normalization ------------------ #

def normalizeAvaCols(df):
    """
    Ensure numeric and categorical consistency across columns.

    Works for both pandas.DataFrame and geopandas.GeoDataFrame.
    """
    renameMap = {c: str(c).upper() for c in df.columns if str(c).lower() in ("ppm", "pem")}
    if renameMap:
        df = df.rename(columns=renameMap)

    for col in ["subC", "elevMin", "elevMax", "praElevMean", "rSize", "PEM", "PPM"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    if "flow" in df.columns:
        df["flow"] = df["flow"].astype(str).str.lower().str.strip()

    if "modType" in df.columns:
        df["modType"] = df["modType"].astype(str).str.lower().str.strip()

    return df


# ------------------ Scenario summary ------------------ #

def logScenarioSummary(gdf: gpd.GeoDataFrame, name: str = ""):
    """
    Log quick summary of scenario result counts.
    """
    if gdf.empty:
        log.warning("Scenario %s: no results", name)
        return

    resCount = (gdf["modType"] == "res").sum() if "modType" in gdf.columns else 0
    relCount = (gdf["modType"] == "rel").sum() if "modType" in gdf.columns else 0
    uniquePra = gdf["praID"].nunique() if "praID" in gdf.columns else None

    log.info(
        "Scenario %s: total=%d (res=%d, rel=%d), uniquePRAs=%s",
        name, len(gdf), resCount, relCount, uniquePra
    )


# ------------------ Parse filter config ------------------ #

def parseFilterConfig(cfg) -> list[dict]:
    """
    Parse [FILTER] and [FILTER.*] sections into scenario dictionaries.

    Uses SAME key names as the attribute table / INI:
      - LKGebiet (optional, string; may be empty)
      - LKGebietID (optional, int or comma-list of ints; may be empty)
      - LWDGebietID (optional, string or comma-list of strings; may be empty)
      - regionMode (or/and)

    Other keys:
      subC, sector, flow, filterElevBand, filterElevMean, elevMin, elevMax,
      AvaDistributionPotential, AvaSizePotential,
      applySingleRsizeRule
    """
    criteriaList: list[dict] = []

    if not cfg.has_section("FILTER"):
        log.warning("No [FILTER] section found; no scenarios defined.")
        return criteriaList

    filterNames = [f.strip() for f in cfg.get("FILTER", "filters", fallback="").split(",") if f.strip()]
    if not filterNames:
        log.warning("[FILTER].filters is empty; no scenarios defined.")
        return criteriaList

    def _getStr(section: str, key: str) -> str:
        if not cfg.has_option(section, key):
            return ""
        return cfg.get(section, key, fallback="").strip()

    def _getList(section: str, key: str):
        if not cfg.has_option(section, key):
            return None
        raw = cfg.get(section, key, fallback="").strip()
        if not raw:
            return None
        vals = [v.strip() for v in raw.split(",") if v.strip()]
        return vals or None

    def _getInt(section: str, key: str):
        if not cfg.has_option(section, key):
            return None
        raw = cfg.get(section, key, fallback="").strip()
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            log.warning("Cannot parse integer for %s.%s = %r", section, key, raw)
            return None

    def _getIntList(section: str, key: str):
        vals = _getList(section, key)
        if not vals:
            return None

        out: list[int] = []
        for v in vals:
            try:
                out.append(int(v))
            except ValueError:
                log.warning("Cannot parse int in list for %s.%s token=%r", section, key, v)
        return out or None

    for shortName in filterNames:
        section = f"FILTER.{shortName}"
        if not cfg.has_section(section):
            log.warning("Missing section [%s]; skipping", section)
            continue

        crit: dict = {"name": cfg.get(section, "name", fallback=shortName)}
        crit["_filterSection"] = section

        crit["LKGebiet"] = _getList(section, "LKGebiet")
        crit["LKGebietID"] = _getIntList(section, "LKGebietID")
        crit["LWDGebietID"] = _getList(section, "LWDGebietID")
        crit["regionMode"] = _getStr(section, "regionMode").lower() or "or"

        subC = _getInt(section, "subC")
        if subC is not None:
            crit["subC"] = [subC]

        crit["sector"] = _getList(section, "sector")
        crit["flow"] = _getList(section, "flow")
        crit["elevMin"] = _getInt(section, "elevMin")
        crit["elevMax"] = _getInt(section, "elevMax")
        globalFilterElevBand = cfg.getboolean("FILTER", "filterElevBand", fallback=True)
        globalFilterElevMean = cfg.getboolean("FILTER", "filterElevMean", fallback=False)
        crit["filterElevBand"] = cfg.getboolean(
            section, "filterElevBand", fallback=globalFilterElevBand
        )
        crit["filterElevMean"] = cfg.getboolean(
            section, "filterElevMean", fallback=globalFilterElevMean
        )

        if crit["filterElevBand"] and crit["filterElevMean"]:
            raise ValueError(
                f"[{section}] filterElevBand and filterElevMean cannot both be True."
            )

        crit["AvaDistributionPotential"] = _getList(section, "AvaDistributionPotential")
        crit["AvaSizePotential"] = _getInt(section, "AvaSizePotential")
        crit["applySingleRsizeRule"] = cfg.getboolean(section, "applySingleRsizeRule", fallback=True)

        criteriaList.append(crit)
        log.info("Configured scenario '%s' from [%s]", crit["name"], section)

    return criteriaList
