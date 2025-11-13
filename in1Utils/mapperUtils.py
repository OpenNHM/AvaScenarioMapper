# --------------------------- in1Utils/mapperUtils.py --------------------------- #
#
# Purpose :
#   Shared utility functions for the Avalanche Scenario Mapper.
#
#   Provides common methods for path resolution, GeoDataFrame I/O,
#   attribute diagnostics, data normalization, and scenario configuration
#   parsing consistent with the Avalanche Scenario Model Chain conventions.
#
# Author :
#   Christoph Hesselbach
#
# Institution :
#   Austrian Research Centre for Forests (BFW)
#   Department of Natural Hazards | Snow and Avalanche Unit
#
# Version :
#   2025-11
#
# ------------------------------------------------------------------------------ #

import os
import logging
from pathlib import Path
import pandas as pd
import geopandas as gpd

from in1Utils.cfgUtils import relPath

log = logging.getLogger(__name__)


# ------------------ Path resolution ------------------ #
def resolvePaths(cfg) -> dict:
    """
    Resolve all input/output paths depending on mapperPathMode.
      - AvaScenDirectory : assumes standard Avalanche Scenario Model Chain
                           directory hierarchy under baseDir
      - customPaths      : reads explicit paths from [PATHS]
    """
    paths = {}
    mode = cfg.get("WORKFLOW", "mapperPathMode", fallback="AvaScenDirectory").lower()
    baseDir = Path(os.path.expandvars(cfg.get("PATHS", "baseDir", fallback=os.getcwd())))

    if mode == "custompaths":
        log.info("Path mode: customPaths (using explicit [PATHS] entries)")
        paths["avaDirectoryResultsParquet"] = Path(cfg.get("PATHS", "avaDirectoryResults"))
        paths["avaScenMapsDir"] = Path(cfg.get("PATHS", "avaScenMapsDir"))
        paths["refTif"] = Path(cfg.get("PATHS", "refTif", fallback=""))
    else:
        log.info("Path mode: AvaScenDirectory (auto-resolved under baseDir)")
        paths["baseDir"] = baseDir
        avaDirRoot = baseDir / "12_avaDirectory"

        candidates = list(avaDirRoot.glob("*/avaDirectoryResults.parquet"))
        if candidates:
            paths["avaDirectoryResultsParquet"] = candidates[0]
            log.info("Detected AvaDirectoryResults in subfolder: %s",
                     relPath(paths["avaDirectoryResultsParquet"].parent, baseDir))
        else:
            paths["avaDirectoryResultsParquet"] = avaDirRoot / "avaDirectoryResults.parquet"

        paths["avaScenMapsDir"] = baseDir / "13_avaScenMaps"
        refCandidates = list((baseDir / "00_input").glob("10DTM_*.tif"))
        paths["refTif"] = refCandidates[0] if refCandidates else baseDir / "00_input" / "refDTM.tif"

    paths["avaScenMapsDir"].mkdir(parents=True, exist_ok=True)
    log.info("Resolved AvaDirectoryResults : %s",
             relPath(paths["avaDirectoryResultsParquet"], baseDir))
    log.info("Resolved AvaScenMaps output  : %s",
             relPath(paths["avaScenMapsDir"], baseDir))
    return paths


# ------------------ I/O Helpers ------------------ #
def readGdf(parquetPath: Path) -> gpd.GeoDataFrame:
    """Read GeoDataFrame from Parquet or GeoJSON."""
    if not parquetPath.exists():
        log.error("Input file not found: %s", parquetPath)
        raise FileNotFoundError(parquetPath)

    gdf = gpd.read_file(parquetPath) if parquetPath.suffix.lower() == ".geojson" \
          else gpd.read_parquet(parquetPath)
    log.info("Loaded %d rows from %s", len(gdf), parquetPath.name)
    return gdf


def writeScenarioOutputs(filteredGdf: gpd.GeoDataFrame,
                         outParquet: Path,
                         outGeoJson: Path = None):
    """Save scenario results to Parquet and optionally GeoJSON."""
    if filteredGdf.empty:
        log.warning("No filtered results to write for %s", outParquet.name)
        return

    outParquet.parent.mkdir(parents=True, exist_ok=True)
    try:
        filteredGdf.to_parquet(outParquet, index=False)
        log.info("Wrote Parquet: %s", outParquet.name)
    except Exception:
        log.exception("Failed to write Parquet for %s", outParquet)
        return

    if outGeoJson:
        try:
            filteredGdf.to_file(outGeoJson, driver="GeoJSON")
            log.info("Wrote GeoJSON: %s", outGeoJson.name)
        except Exception:
            log.warning("GeoJSON write warning for %s", outGeoJson.name)


# ------------------ Data integrity check ------------------ #
def checkInputData(gdf: gpd.GeoDataFrame, parquetPath: Path, cfg=None) -> bool:
    """Validate that the input avaDirectoryResults dataset contains all required columns."""
    requiredCols = [
        "praID", "flow", "sector", "subC",
        "elevMin", "elevMax", "rSize",
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
    if not checkFlag:
        printAvailableOptions(parquetPath)

    log.info("Input data integrity check passed (%d rows, %d columns).",
             len(gdf), len(gdf.columns))
    return True


# ------------------ Diagnostic Mode ------------------ #
def handleAvaDirCheckMode(cfg, parquetPath: Path) -> bool:
    """Diagnostic mode: list attributes and exit early if requested."""
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
    """List available filterable attributes in avaDirectoryResults.parquet."""
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
def normalizeAvaCols(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Ensure numeric and categorical consistency across columns."""
    gdf = gdf.rename(columns={c.lower(): c.upper() for c in gdf.columns if c.lower() in ["ppm", "pem"]})
    for col in ["subC", "elevMin", "elevMax", "rSize", "PEM", "PPM"]:
        if col in gdf.columns:
            gdf[col] = pd.to_numeric(gdf[col], errors="coerce")
    if "flow" in gdf.columns:
        gdf["flow"] = gdf["flow"].astype(str).str.lower().str.strip()
    if "modType" in gdf.columns:
        gdf["modType"] = gdf["modType"].astype(str).str.lower().str.strip()
    return gdf


# ------------------ Scenario summary ------------------ #
def logScenarioSummary(gdf: gpd.GeoDataFrame, name: str = ""):
    """Log quick summary of scenario result counts."""
    if gdf.empty:
        log.warning("Scenario %s: no results", name)
        return
    resCount = (gdf["modType"] == "res").sum() if "modType" in gdf.columns else 0
    relCount = (gdf["modType"] == "rel").sum() if "modType" in gdf.columns else 0
    uniquePra = gdf["praID"].nunique() if "praID" in gdf.columns else None
    log.info("Scenario %s: total=%d (res=%d, rel=%d), uniquePRAs=%s",
             name, len(gdf), resCount, relCount, uniquePra)


# ------------------ Parse filter config ------------------ #
def parseFilterConfig(cfg) -> list[dict]:
    """
    Parse [FILTER] and [FILTER.*] sections into scenario dictionaries.

    Each [FILTER.<name>] can define:
      name, LwdRegionID, LKRegionID, regionMode,
      subC, sector, flow,
      elevMin, elevMax,
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

    def _getList(section: str, key: str):
        if not cfg.has_option(section, key):
            return None
        vals = [v.strip() for v in cfg.get(section, key, fallback="").split(",") if v.strip()]
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

    for shortName in filterNames:
        section = f"FILTER.{shortName}"
        if not cfg.has_section(section):
            log.warning("Missing section [%s]; skipping", section)
            continue

        crit: dict = {"name": cfg.get(section, "name", fallback=shortName)}
        crit["LwdRegionID"] = _getList(section, "LwdRegionID")
        crit["LKRegionID"] = _getList(section, "LKRegionID")
        crit["regionMode"] = cfg.get(section, "regionMode", fallback="or")

        subC = _getInt(section, "subC")
        if subC is not None:
            crit["subCs"] = [subC]

        crit["sectors"] = _getList(section, "sector")
        crit["flows"] = _getList(section, "flow")
        crit["elevMin"] = _getInt(section, "elevMin")
        crit["elevMax"] = _getInt(section, "elevMax")
        crit["AvaDistributionPotential"] = _getList(section, "AvaDistributionPotential")
        crit["AvaSizePotential"] = _getInt(section, "AvaSizePotential")
        crit["applySingleRsizeRule"] = cfg.getboolean(section, "applySingleRsizeRule", fallback=True)

        criteriaList.append(crit)
        log.info("Configured scenario '%s' from [%s]", crit["name"], section)
    return criteriaList
