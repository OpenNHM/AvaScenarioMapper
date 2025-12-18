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
# Date & Version :
#   2025-11 - 1.0
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
    paths: dict = {}

    mode = cfg.get("WORKFLOW", "mapperPathMode", fallback="AvaScenDirectory").strip().lower()

    # baseDir: always define (even for customPaths, used for relPath logging)
    baseDir = Path(os.path.expandvars(cfg.get("PATHS", "baseDir", fallback=os.getcwd()))).expanduser()
    paths["baseDir"] = baseDir

    if mode == "custompaths":
        log.info("Path mode: customPaths (using explicit [PATHS] entries)")
        paths["avaDirectoryResultsParquet"] = Path(cfg.get("PATHS", "avaDirectoryResults")).expanduser()
        paths["avaScenMapsDir"] = Path(cfg.get("PATHS", "avaScenMapsDir")).expanduser()
        paths["refTif"] = Path(cfg.get("PATHS", "refTif", fallback="")).expanduser()

    else:
        log.info("Path mode: AvaScenDirectory (auto-resolved under baseDir)")

        avaDirRoot = baseDir / "12_avaDirectory"
        paths["avaScenMapsDir"] = baseDir / "13_avaScenMaps"

        # pick avaDirectoryResults.parquet deterministically
        candidates = sorted(avaDirRoot.glob("*/avaDirectoryResults.parquet"))

        if len(candidates) == 1:
            paths["avaDirectoryResultsParquet"] = candidates[0]
            log.info(
                "Detected AvaDirectoryResults in subfolder: %s",
                relPath(paths["avaDirectoryResultsParquet"].parent, baseDir),
            )
        elif len(candidates) > 1:
            raise RuntimeError(
                f"Multiple avaDirectoryResults.parquet found under {avaDirRoot}:\n"
                + "\n".join([f"  - {c}" for c in candidates])
                + "\n\nPlease set mapperPathMode=customPaths and provide [PATHS].avaDirectoryResults."
            )
        else:
            paths["avaDirectoryResultsParquet"] = avaDirRoot / "avaDirectoryResults.parquet"

        refCandidates = sorted((baseDir / "00_input").glob("10DTM_*.tif"))
        paths["refTif"] = refCandidates[0] if refCandidates else baseDir / "00_input" / "refDTM.tif"

    # ensure output dir exists (now guaranteed key exists)
    paths["avaScenMapsDir"].mkdir(parents=True, exist_ok=True)

    log.info("Resolved AvaDirectoryResults : %s", relPath(paths["avaDirectoryResultsParquet"], baseDir))
    log.info("Resolved AvaScenMaps output  : %s", relPath(paths["avaScenMapsDir"], baseDir))
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


def writeScenarioOutputs(filteredGdf, outParquet=None, outGeoJson=None, outGpkg=None, outCsv=None, csvWkt=False):
    """
    Save scenario results to one or more formats.

    - Parquet: main artifact (fast, compact)
    - GeoJSON: optional (can get huge)
    - GPKG: GIS-friendly, compact
    - CSV: optional (no geometry unless csvWkt=True)
    """
    if filteredGdf.empty:
        log.warning("No filtered results to write.")
        return

    # Ensure parent folder exists (use first defined output)
    for p in (outParquet, outGeoJson, outGpkg, outCsv):
        if p is not None:
            p.parent.mkdir(parents=True, exist_ok=True)
            break

    # --- Parquet ---
    if outParquet is not None:
        try:
            filteredGdf.to_parquet(outParquet, index=False)
            log.info("Wrote Parquet: %s", outParquet.name)
        except Exception:
            log.exception("Failed to write Parquet for %s", outParquet)

    # --- GeoJSON (expensive) ---
    if outGeoJson is not None:
        try:
            filteredGdf.to_file(outGeoJson, driver="GeoJSON")
            log.info("Wrote GeoJSON: %s", outGeoJson.name)
        except Exception:
            log.warning("GeoJSON write warning for %s", outGeoJson.name)

    # --- GeoPackage ---
    if outGpkg is not None:
        try:
            layerName = outGpkg.stem  # e.g. "avaScen_WinterForNTirol"
            filteredGdf.to_file(outGpkg, layer=layerName, driver="GPKG")
            log.info("Wrote GPKG: %s (layer=%s)", outGpkg.name, layerName)
        except Exception:
            log.exception("Failed to write GPKG for %s", outGpkg)

    # --- CSV (attribute table export) ---
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
    if checkFlag:
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
    """
    Ensure numeric and categorical consistency across columns.

    Key fix:
    - Normalize any PPM/PEM casing reliably (e.g. 'ppm', 'Ppm', 'PEM' -> 'PPM'/'PEM').
    """
    # --- Fix PPM/PEM casing robustly ---
    rename_map = {c: c.upper() for c in gdf.columns if str(c).lower() in ("ppm", "pem")}
    if rename_map:
        gdf = gdf.rename(columns=rename_map)

    # --- Numeric conversions ---
    for col in ["subC", "elevMin", "elevMax", "rSize", "PEM", "PPM"]:
        if col in gdf.columns:
            gdf[col] = pd.to_numeric(gdf[col], errors="coerce")

    # --- Categorical normalization ---
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
