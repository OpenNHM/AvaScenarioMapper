# ------------------ in1Utils/mapperUtils.py ------------------ #
# Shared utility functions for CAIROS AvaScenario Mapper
#
# Consistent with cairosModelChain conventions.
# Handles:
#   - Path resolution
#   - GeoDataFrame I/O
#   - Attribute diagnostics
#   - Data normalization
#   - Scenario configuration parsing
#
# Author : CAIROS Project Team
# Version: 2025-11
# -------------------------------------------------------------------------

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
      - cairosPaths : assumes standard CAIROS directory hierarchy under baseDir
      - customPaths : reads explicit paths from [PATHS]
    """
    paths = {}
    mode = cfg.get("WORKFLOW", "mapperPathMode", fallback="cairosPaths").lower()
    baseDir = Path(os.path.expandvars(cfg.get("PATHS", "baseDir", fallback=os.getcwd())))

    if mode == "custompaths":
        log.info("Path mode: customPaths (using explicit [PATHS] entries)")
        paths["avaDirectoryResultsParquet"] = Path(cfg.get("PATHS", "avaDirectoryResults"))
        paths["avaScenMapsDir"] = Path(cfg.get("PATHS", "avaScenMapsDir"))
        paths["refTif"] = Path(cfg.get("PATHS", "refTif", fallback=""))
    else:
        log.info("Path mode: cairosPaths (auto-resolved under baseDir)")
        paths["baseDir"] = baseDir
        avaDirRoot = baseDir / "12_avaDirectory"

        # --- Try to locate avaDirectoryResults.parquet dynamically ---
        candidates = list(avaDirRoot.glob("*/avaDirectoryResults.parquet"))
        if candidates:
            paths["avaDirectoryResultsParquet"] = candidates[0]
            log.info("Detected AvaDirectoryResults in subfolder: %s",
                     relPath(paths["avaDirectoryResultsParquet"].parent, baseDir))
        else:
            # fallback to top level (legacy flat structure)
            paths["avaDirectoryResultsParquet"] = avaDirRoot / "avaDirectoryResults.parquet"

        # Output path
        paths["avaScenMapsDir"] = baseDir / "13_avaScenMaps"

        # Auto-detect DTM reference (10DTM_<project>.tif)
        refCandidates = list((baseDir / "00_input").glob("10DTM_*.tif"))
        if refCandidates:
            paths["refTif"] = refCandidates[0]
        else:
            paths["refTif"] = baseDir / "00_input" / "refDTM.tif"

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

    if parquetPath.suffix.lower() == ".geojson":
        gdf = gpd.read_file(parquetPath)
    else:
        gdf = gpd.read_parquet(parquetPath)

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

def checkInputData(
    gdf: gpd.GeoDataFrame,
    parquetPath: Path,
    cfg=None
) -> bool:
    """
    Validate that the input avaDirectoryResults dataset contains
    all required columns for filtering.
    If checkAvaDirResult=True in cfg, skip attribute print (handled separately).
    """
    requiredCols = [
        "praID", "flow", "sector", "subC",
        "elevMin", "elevMax", "rSize",
        "LKGebietID", "LWDGebietID"
    ]

    log.info("Checking input data integrity for: %s", parquetPath.name)
    missing = [c for c in requiredCols if c not in gdf.columns]
    if missing:
        log.error("Missing required columns in %s: %s", parquetPath.name, ", ".join(missing))
        log.error("Cannot continue mapping — please verify upstream steps (Step 15 outputs).")
        printAvailableOptions(parquetPath)
        return False

    if gdf.empty:
        log.error("Input dataset %s is empty — nothing to process.", parquetPath.name)
        return False

    # Only show available attributes if not running diagnostic mode
    checkFlag = cfg and cfg.getboolean("WORKFLOW", "checkAvaDirResult", fallback=False)
    if not checkFlag:
        printAvailableOptions(parquetPath)

    log.info(
        "Input data integrity check passed (%d rows, %d columns).",
        len(gdf), len(gdf.columns)
    )
    return True




# ------------------ Pre-run Attribute Check Mode ------------------ #

def handleAvaDirCheckMode(cfg, parquetPath: Path) -> bool:
    """
    Handle pre-run diagnostic mode for avaDirectoryResults.
    If [WORKFLOW].checkAvaDirResult = True, print available
    attributes and exit early after logging a message.

    Returns
    -------
    bool
        True  → continue workflow
        False → abort after showing diagnostics
    """
    checkFlag = cfg.getboolean("WORKFLOW", "checkAvaDirResult", fallback=False)
    if not checkFlag:
        return True  # proceed normally

    log.info("------------------------------------------------------------")
    log.info("Diagnostic mode enabled: checkAvaDirResult = True")
    log.info("Inspecting available attributes in AvaDirectoryResults...")
    printAvailableOptions(parquetPath)
    log.warning("------------------------------------------------------------")
    log.warning("Set your scenarios in avaScenMapperCfg.ini and run again!")
    log.warning("Mapper workflow terminated by user request (checkAvaDirResult=True).")
    log.info("------------------------------------------------------------")

    # return False to stop further execution
    return False




# ------------------ Diagnostics ------------------ #

def printAvailableOptions(parquetPath: Path):
    """List available filterable attributes in avaDirectoryResults.parquet."""
    if not parquetPath.exists():
        log.warning("File not found for diagnostics: %s", parquetPath)
        return

    df = pd.read_parquet(parquetPath)
    log.info("Available attributes in: %s", parquetPath.name)

    numCols = [
        "praAreaM", "praAreaSized", "praAreaVol",
        "praElevMin", "praElevMax", "praElevMean",
        "LKGebietID", "subC", "elevMin", "elevMax",
        "ppm", "pem", "rSize"
    ]
    for col in numCols:
        if col in df.columns and df[col].notna().any():
            cmin, cmax = df[col].min(), df[col].max()
            log.info("   %-15s: %.0f → %.0f", col, cmin, cmax)

    catCols = [
        "sector", "flow", "modType", "praElevBand", "praElevBandRule",
        "LKGebiet", "LKRegion", "LWDGebietID"
    ]
    for col in catCols:
        if col in df.columns:
            uniq = sorted(df[col].dropna().unique().tolist())
            shown = uniq if len(uniq) <= 20 else uniq[:20] + ["..."]
            log.info("   %-15s: %s", col, shown)

    if "praID" in df.columns:
        log.info("   praID count: %d unique IDs", df["praID"].nunique())

    pathCols = [c for c in df.columns if c.startswith("path")]
    if pathCols:
        log.info("   Raster product types: %s",
                 [c.replace('path', '').lower() for c in pathCols])
    log.info("")


# ------------------ Normalization ------------------ #

def normalizeAvaCols(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Ensure numeric and categorical consistency across columns."""
    if "PEM" not in gdf.columns and "pem" in gdf.columns:
        gdf = gdf.rename(columns={"pem": "PEM"})
    if "PPM" not in gdf.columns and "ppm" in gdf.columns:
        gdf = gdf.rename(columns={"ppm": "PPM"})

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
    """Print a quick summary of scenario result counts."""
    if gdf.empty:
        log.warning("Scenario %s: no results", name)
        return

    total = len(gdf)
    resCount = (gdf["modType"] == "res").sum() if "modType" in gdf.columns else 0
    relCount = (gdf["modType"] == "rel").sum() if "modType" in gdf.columns else 0
    uniquePra = gdf["praID"].nunique() if "praID" in gdf.columns else None

    log.info("Scenario %s: total=%d (res=%d, rel=%d), uniquePRAs=%s",
             name, total, resCount, relCount, uniquePra)


# ------------------ Parse filter cfg ------------------ #

def parseFilterConfig(cfg) -> list[dict]:
    """
    Parse [FILTER] and [FILTER.*] sections into a list of scenario
    definition dictionaries.

    Each [FILTER.<name>] entry can define:
      name, LwdRegionID, LKRegionID, regionMode,
      subC, sector, flow,
      elevMin, elevMax,
      avaPotential, avaSize,
      applySingleRsizeRule
    """
    criteriaList: list[dict] = []

    if not cfg.has_section("FILTER"):
        log.warning("No [FILTER] section found; no scenarios defined.")
        return criteriaList

    filterNames = [
        f.strip()
        for f in cfg.get("FILTER", "filters", fallback="").split(",")
        if f.strip()
    ]
    if not filterNames:
        log.warning("[FILTER].filters is empty; no scenarios defined.")
        return criteriaList

    def _getList(section: str, key: str):
        if not cfg.has_option(section, key):
            return None
        raw = cfg.get(section, key, fallback="")
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

    for shortName in filterNames:
        section = f"FILTER.{shortName}"
        if not cfg.has_section(section):
            log.warning("Missing section [%s]; skipping", section)
            continue

        crit: dict = {}
        crit["name"] = cfg.get(section, "name", fallback=shortName)

        # Region filters
        crit["LwdRegionID"] = _getList(section, "LwdRegionID")
        crit["LKRegionID"] = _getList(section, "LKRegionID")
        crit["regionMode"] = cfg.get(section, "regionMode", fallback="or")

        # Scenario filters
        subC = _getInt(section, "subC")
        if subC is not None:
            crit["subCs"] = [subC]

        crit["sectors"] = _getList(section, "sector")
        crit["flows"] = _getList(section, "flow")

        elevMin = _getInt(section, "elevMin")
        elevMax = _getInt(section, "elevMax")
        if elevMin is not None:
            crit["elevMin"] = elevMin
        if elevMax is not None:
            crit["elevMax"] = elevMax

        # Avalanche legend filters
        crit["avaPotential"] = _getList(section, "avaPotential")
        crit["avaSize"] = _getInt(section, "avaSize")
        crit["applySingleRsizeRule"] = cfg.getboolean(
            section, "applySingleRsizeRule", fallback=True
        )

        criteriaList.append(crit)
        log.info("Configured scenario '%s' from [%s]", crit["name"], section)

    return criteriaList
