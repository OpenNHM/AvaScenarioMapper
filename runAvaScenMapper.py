

# ───────────────────────────────────────────────────────────────────────────────────────────────
#    ███████  A V A L A N C H E · S C E N E N A R I O · M A P P E R   ██████████████████
# ───────────────────────────────────────────────────────────────────────────────────────────────
#
#    ██████╗  ██╗  ██╗ ██████╗     ████████╗  ██████╗ ███████╗ ███╗   ██╗
#    ██╔══██╗ ██╗  ██║ ██╔══██╗    ╚██╔════╝ ██╔════╝ ██╔════╝ ████╗  ██║
#    ███████║ ██║ ██╔╝ ███████║     ███████╗ ██║      █████╗   ██╔██╗ ██║           
#    ██╔══██║ ██║██╔╝  ██╔══██║     ╚════██║ ██║      ██╔══╝   ██║╚██╗██║
#    ██║  ██║ ╚███╔╝   ██║  ███╗██╗████████║ ╚██████╗ ███████╗ ██║ ╚████║ █████╗ ███╗██╗
#    ╚═╝  ╚═╝  ╚══╝    ╚═╝  ╚══╝╚═╝╚═══════╝  ╚═════╝ ╚══════╝ ╚═╝  ╚═══╝ ╚════╝ ╚══╝╚═╝
# ───────────────────────────────────────────────────────────────────────────────────────────────
#    ███████  runAvaScenMapper.py   ·  runAvaScenMapper.py  ·  runAvaScenMapper  ███████
# ───────────────────────────────────────────────────────────────────────────────────────────────
#
# Purpose :
#   Step 17 of the Avalanche Scenario Model Chain.
#   Filters avaDirectoryResults.parquet into scenario-specific subsets
#   for visualization, mapping, and publication.
#
# Inputs  :
#   12_avaDirectory/avaDirectoryResults.parquet
# Outputs :
#   13_avaScenMaps/avaScen_<Scenario>.parquet / .geojson
#
# Config  :
#   avaScenMapperCfg.ini + local_avaScenMapperCfg.ini
#   [WORKFLOW], [PATHS], [FILTER], [FILTER.*]
#
# Execution :
#   pixi run -e dev python runAvaScenMapper.py
#   or  python runAvaScenMapper.py --cfg avaScenMapperCfg.ini
#
# Author :
#   Christoph Hesselbach
#
# Institution :
#   Austrian Research Centre for Forests (BFW)
#   Department of Natural Hazards | Snow and Avalanche Unit
#
# Date & Version :
#   2026-02 - 1.1
#
# ---------------------------------------------------------------------------------- #

# ------------------ System imports ------------------ #
import sys
import time
import logging
import configparser
from pathlib import Path
from typing import List, Dict, Optional
from datetime import datetime

import pandas as pd
import geopandas as gpd

# ------------------ Core utilities ------------------ #
import in1Utils.cfgUtils as cfgUtils
import in1Utils.mapperUtils as mapperUtils
from in1Utils.cfgUtils import relPath

# ------------------ Components ------------------ #
import com3AvaScenFilter.avaScenFilter as avaScenFilter
import in2Matrix.avaPotMatrix as avaPotMatrix
import in1Utils.caamlUtils as caamlUtils  

# ------------------ Logger ------------------ #
log = logging.getLogger(__name__)


# --------------------------- MAIN FUNCTION --------------------------- #
def runAvaScenMapper(
    cfg: configparser.ConfigParser,
    paths: Optional[dict] = None,
    areaCriteriaList: Optional[List[Dict]] = None,
) -> None:
    """Main entry point for the Avalanche Scenario Mapper (Step 17)."""
    t0 = time.perf_counter()

    log.info(
        "\n\n"
        "       ==============================================================================\n"
        f"          ... Start Avalanche Scenario Mapper (Step 17)  ({time.strftime('%Y-%m-%d %H:%M:%S')}) ...\n"
        "       ==============================================================================\n"
    )

    # ------------------ Resolve paths ------------------ #
    if paths is None:
        paths = mapperUtils.resolvePaths(cfg)

    avaResultsPath = paths["avaDirectoryResultsParquet"]
    scenMapsDir = paths["avaScenMapsDir"]
    baseDir = Path(paths.get("baseDir", scenMapsDir.parent))

    log.info("Input AvaDirectoryResults : %s", relPath(avaResultsPath, baseDir))
    log.info("Output AvaScenMaps folder : %s", relPath(scenMapsDir, baseDir))

    # ------------------ Load and validate input data ------------------ #
    gdf = mapperUtils.readGdf(avaResultsPath)
    gdf = mapperUtils.normalizeAvaCols(gdf)

    if not mapperUtils.checkInputData(gdf, avaResultsPath, cfg):
        log.error("Step 17 aborted: input dataset incomplete or invalid.")
        return

    if not mapperUtils.handleAvaDirCheckMode(cfg, avaResultsPath):
        return

    # ------------------ Load Avalanche Distribution–Size matrix ------------------ #
    avaLegend = avaPotMatrix.avaPotMatrix()
    log.info("Step 17: Avalanche Distribution–Size matrix loaded (%d entries)", len(avaLegend))

    # ------------------ Parse scenario definitions ------------------ #
    if areaCriteriaList is None:
        if cfg.getboolean("WORKFLOW", "mapperUseCaaml", fallback=False):
            log.info("Step 17: CAAML integration requested (not yet implemented).")
            areaCriteriaList = []
        else:
            areaCriteriaList = mapperUtils.parseFilterConfig(cfg)

    if not areaCriteriaList:
        log.warning("Step 17: No scenarios configured → exiting Mapper.")
        return

    # ------------------ Output format flags ------------------ #
    writeParquet = cfg.getboolean("WORKFLOW", "writeScenarioParquet", fallback=True)
    writeGeoJson = cfg.getboolean("WORKFLOW", "writeScenarioGeoJson", fallback=False)
    writeGpkg    = cfg.getboolean("WORKFLOW", "writeScenarioGpkg", fallback=False)
    writeCsv     = cfg.getboolean("WORKFLOW", "writeScenarioCsv", fallback=False)
    csvWkt       = cfg.getboolean("WORKFLOW", "writeScenarioCsvWkt", fallback=False)

    makeMaster     = cfg.getboolean("WORKFLOW", "mapperMakeMaster", fallback=False)
    makeMasterOnly = cfg.getboolean("WORKFLOW", "mapperOnlyMaster", fallback=False)

    if makeMasterOnly and not makeMaster:
        log.warning("mapperOnlyMaster=True but mapperMakeMaster=False → no output will be produced.")

    # ------------------ Pre-skip logic ------------------ #
    criteriaToRun: List[Dict] = []
    skipped = 0

    if makeMasterOnly:
        # always run all scenarios to build the master
        criteriaToRun = areaCriteriaList
    else:
        for crit in areaCriteriaList:
            scenName = crit.get("name", "unnamed")
            scenNameClean = "".join(ch for ch in scenName if ch.isalnum() or ch in "-_") or "unnamed"

            outputs = [
                (scenMapsDir / f"avaScen_{scenNameClean}.parquet") if writeParquet else None,
                (scenMapsDir / f"avaScen_{scenNameClean}.gpkg")    if writeGpkg else None,
                (scenMapsDir / f"avaScen_{scenNameClean}.geojson") if writeGeoJson else None,
                (scenMapsDir / f"avaScen_{scenNameClean}.csv")     if writeCsv else None,
            ]

            existing = next((p for p in outputs if p and p.exists()), None)
            if existing:
                skipped += 1
                log.info(
                    "Skipping scenario '%s' (already exists: %s)",
                    scenName,
                    relPath(existing, baseDir),
                )
                continue

            criteriaToRun.append(crit)

    if not criteriaToRun:
        log.warning("Nothing to process → exiting Mapper.")
        return

    # ------------------ Run scenario filtering ------------------ #
    results = avaScenFilter.runScenarioFilters(gdf, criteriaToRun, avaLegend)
    if not results:
        log.warning("No scenario produced output.")
        return

    # ------------------ Write per-scenario outputs ------------------ #
    if not makeMasterOnly:
        for crit, df in results:
            scenName = crit.get("name", "unnamed")
            scenNameClean = "".join(ch for ch in scenName if ch.isalnum() or ch in "-_") or "unnamed"

            mapperUtils.writeScenarioOutputs(
                df,
                outParquet=(scenMapsDir / f"avaScen_{scenNameClean}.parquet") if writeParquet else None,
                outGeoJson=(scenMapsDir / f"avaScen_{scenNameClean}.geojson") if writeGeoJson else None,
                outGpkg   =(scenMapsDir / f"avaScen_{scenNameClean}.gpkg")    if writeGpkg else None,
                outCsv    =(scenMapsDir / f"avaScen_{scenNameClean}.csv")     if writeCsv else None,
                csvWkt=csvWkt,
            )
    else:
        log.info("Per-scenario outputs skipped (mapperOnlyMaster = True).")

    # ------------------ Combine master file (optional) ------------------ #
    if makeMaster:
        parts = baseDir.parts

        if "Euregio" in parts and parts.index("Euregio") + 1 < len(parts):
            # .../Euregio/<region>/...
            region = parts[parts.index("Euregio") + 1]

        elif baseDir.parent and baseDir.parent.name.isdigit() and baseDir.parent.parent:
            # .../<project>/<yyyymmdd>/<run>/
            region = baseDir.parent.parent.name

        else:
            # fallback
            region = baseDir.name

        ts = datetime.now().strftime("%y%m%d-%H%M%S")
        masterName = f"avaScen_{region}_{ts}"


        dfs = [df for _, df in results]
        master = gpd.GeoDataFrame(
            pd.concat(dfs, ignore_index=True),
            crs=dfs[0].crs
        )

        mapperUtils.writeScenarioOutputs(
            master,
            outParquet=(scenMapsDir / f"{masterName}.parquet") if writeParquet else None,
            outGeoJson=(scenMapsDir / f"{masterName}.geojson") if writeGeoJson else None,
            outGpkg   =(scenMapsDir / f"{masterName}.gpkg")    if writeGpkg else None,
            outCsv    =(scenMapsDir / f"{masterName}.csv")     if writeCsv else None,
            csvWkt=csvWkt,
        )

        mapperUtils.logScenarioSummary(master, masterName)

    log.info("Step 17 finished in %.2fs", time.perf_counter() - t0)


# --------------------------- MAIN ENTRYPOINT --------------------------- #
def main(argv: Optional[list] = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    cfgPath = Path("avaScenMapperCfg.ini")
    if len(argv) >= 2 and argv[0] == "--cfg":
        cfgPath = Path(argv[1])

    if not cfgPath.exists():
        sys.stderr.write(f"Configuration file not found: {cfgPath}\n")
        return 1

    cfg = cfgUtils.readCfg(cfgPath)
    log_path = cfgUtils.setupMapperLogging(cfg)

    # --------------------------------------------------
    # baseDir may contain ONE or MULTIPLE absolute paths
    # --------------------------------------------------
    baseDir_raw = cfg.get("PATHS", "baseDir", fallback="")
    baseDirs = [Path(b.strip()) for b in baseDir_raw.splitlines() if b.strip()]

    if not baseDirs:
        log.error("No baseDir configured in [PATHS].")
        return 1

    for baseDir in baseDirs:
        if not baseDir.exists():
            log.error("baseDir does not exist: %s", baseDir)
            continue

        log.info("Running Avalanche Scenario Mapper for baseDir:")
        log.info("  %s", baseDir)

        cfg_run = configparser.ConfigParser()
        cfg_run.read_dict({s: dict(cfg[s]) for s in cfg.sections()})
        cfg_run.set("PATHS", "baseDir", str(baseDir))

        try:
            runAvaScenMapper(cfg_run)
        except Exception:
            log.exception("Scenario Mapper failed for baseDir: %s", baseDir)

    log.info("Avalanche Scenario Mapper log saved at: %s\n", log_path)
    return 0


# --------------------------- MAIN RUNNER --------------------------- #
if __name__ == "__main__":
    raise SystemExit(main())