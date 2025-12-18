

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
#   2025-11 - 1.0
#
# ---------------------------------------------------------------------------------- #


# ------------------ System imports ------------------ #
import sys
import time
import logging
import configparser
from pathlib import Path
from typing import List, Dict, Optional

import pandas as pd
import geopandas as gpd

# ------------------ Core utilities ------------------ #
import in1Utils.cfgUtils as cfgUtils
import in1Utils.mapperUtils as mapperUtils
from in1Utils.cfgUtils import relPath

# ------------------ Components ------------------ #
import com3AvaScenFilter.avaScenFilter as avaScenFilter
import in2Matrix.avaPotMatrix as avaPotMatrix
import in1Utils.caamlUtils as caamlUtils  # placeholder for future CAAML v6 integration

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
    baseDir = paths.get("baseDir", scenMapsDir.parent)

    log.info("Input AvaDirectoryResults : %s", relPath(avaResultsPath, baseDir))
    log.info("Output AvaScenMaps folder : %s", relPath(scenMapsDir, baseDir))

    # ------------------ Load and validate input data ------------------ #
    gdf = mapperUtils.readGdf(avaResultsPath)
    gdf = mapperUtils.normalizeAvaCols(gdf)

    if not mapperUtils.checkInputData(gdf, avaResultsPath, cfg):
        log.error("Step 17 aborted: input dataset incomplete or invalid.")
        return

    # Optional pre-run diagnostic mode
    if not mapperUtils.handleAvaDirCheckMode(cfg, avaResultsPath):
        return

    # ------------------ Load Avalanche Distribution–Size matrix ------------------ #
    avaLegend = avaPotMatrix.avaPotMatrix()
    log.info("Step 17: Avalanche Distribution–Size matrix loaded (%d entries)", len(avaLegend))

    # ------------------ Parse scenario definitions ------------------ #
    if areaCriteriaList is None:
        useCaaml = cfg.getboolean("WORKFLOW", "mapperUseCaaml", fallback=False)
        if useCaaml:
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
    writeGpkg = cfg.getboolean("WORKFLOW", "writeScenarioGpkg", fallback=False)
    writeCsv = cfg.getboolean("WORKFLOW", "writeScenarioCsv", fallback=False)
    csvWkt = cfg.getboolean("WORKFLOW", "writeScenarioCsvWkt", fallback=False)

    # ------------------ Pre-skip: scenario outputs already exist ------------------ #
    criteriaToRun: List[Dict] = []
    skipped = 0

    for crit in areaCriteriaList:
        scenName = crit.get("name", "unnamed")
        scenNameClean = "".join(ch for ch in scenName if ch.isalnum() or ch in "-_").strip() or "unnamed"

        outParquet = (scenMapsDir / f"avaScen_{scenNameClean}.parquet") if writeParquet else None
        outGeoJson = (scenMapsDir / f"avaScen_{scenNameClean}.geojson") if writeGeoJson else None
        outGpkg = (scenMapsDir / f"avaScen_{scenNameClean}.gpkg") if writeGpkg else None
        outCsv = (scenMapsDir / f"avaScen_{scenNameClean}.csv") if writeCsv else None

        enabledOuts = [p for p in (outParquet, outGpkg, outGeoJson, outCsv) if p is not None]
        existing = next((p for p in enabledOuts if p.exists()), None)

        if existing is not None:
            skipped += 1
            log.info(
                "Step 17: Skipping scenario '%s' because output already exists: %s",
                scenName,
                relPath(existing, baseDir),
            )
            continue

        criteriaToRun.append(crit)

    if not criteriaToRun:
        log.warning("Step 17: All %d scenario(s) already exist → nothing to do.", len(areaCriteriaList))
        return

    log.info(
        "Step 17: Running %d scenario(s) (skipped %d already existing) --------------------------------",
        len(criteriaToRun),
        skipped,
    )

    # ------------------ Run scenario filtering ------------------ #
    # IMPORTANT: runScenarioFilters must return List[(crit, gdf)]
    results = avaScenFilter.runScenarioFilters(gdf, criteriaToRun, avaLegend)

    if not results:
        log.warning("Step 17: No scenario produced output; nothing to export.")
        return

    # ------------------ Write per-scenario outputs ------------------ #
    for crit, df in results:
        scenName = crit.get("name", "unnamed")
        scenNameClean = "".join(ch for ch in scenName if ch.isalnum() or ch in "-_").strip() or "unnamed"

        outParquet = (scenMapsDir / f"avaScen_{scenNameClean}.parquet") if writeParquet else None
        outGeoJson = (scenMapsDir / f"avaScen_{scenNameClean}.geojson") if writeGeoJson else None
        outGpkg = (scenMapsDir / f"avaScen_{scenNameClean}.gpkg") if writeGpkg else None
        outCsv = (scenMapsDir / f"avaScen_{scenNameClean}.csv") if writeCsv else None

        mainOut = outParquet or outGpkg or outGeoJson or outCsv
        if mainOut is None:
            log.warning("Step 17: No output format enabled for scenario '%s' (skipping write).", scenName)
            continue

        log.info("Step 17: Writing scenario '%s' → %s", scenName, relPath(mainOut, baseDir))

        mapperUtils.writeScenarioOutputs(
            df,
            outParquet=outParquet,
            outGeoJson=outGeoJson,
            outGpkg=outGpkg,
            outCsv=outCsv,
            csvWkt=csvWkt,
        )

    # ------------------ Combine master file (optional) ------------------ #
    makeMaster = cfg.getboolean("WORKFLOW", "mapperMakeMaster", fallback=False)
    if makeMaster:
        log.info("Step 17: Combining all scenarios into avaScen_Master --------------------------------")

        dfs = [df for _, df in results]
        master = gpd.GeoDataFrame(pd.concat(dfs, ignore_index=True), crs=dfs[0].crs)

        outParquet = (scenMapsDir / "avaScen_Master.parquet") if writeParquet else None
        outGeoJson = (scenMapsDir / "avaScen_Master.geojson") if writeGeoJson else None
        outGpkg = (scenMapsDir / "avaScen_Master.gpkg") if writeGpkg else None
        outCsv = (scenMapsDir / "avaScen_Master.csv") if writeCsv else None

        mainOut = outParquet or outGpkg or outGeoJson or outCsv
        if mainOut is None:
            log.warning("Step 17: No output format enabled for master (skipping write).")
        else:
            log.info("Step 17: Writing master → %s", relPath(mainOut, baseDir))
            mapperUtils.writeScenarioOutputs(
                master,
                outParquet=outParquet,
                outGeoJson=outGeoJson,
                outGpkg=outGpkg,
                outCsv=outCsv,
                csvWkt=csvWkt,
            )

        mapperUtils.logScenarioSummary(master, "avaScen_Master")
        log.info("Master file CRS inherited from first scenario for consistency.")


    # ------------------ Completion ------------------ #
    dt = time.perf_counter() - t0
    log.info(
        "\n\n       ============================================================================\n"
        f"          ... Step 17: Avalanche Scenario Mapper finished successfully in {dt:.2f}s ...\n"
        "       ============================================================================\n"
    )


# --------------------------- MAIN ENTRYPOINT --------------------------- #
def main(argv: Optional[list] = None) -> int:
    """Command-line entry point for standalone execution."""
    if argv is None:
        argv = sys.argv[1:]

    cfgPath = Path("avaScenMapperCfg.ini")
    if len(argv) >= 2 and argv[0] == "--cfg":
        cfgPath = Path(argv[1])

    if not cfgPath.exists():
        # logging may not be configured yet here, so use stderr
        sys.stderr.write(f"Configuration file not found: {cfgPath}\n")
        return 1

    cfg = cfgUtils.readCfg(cfgPath)
    log_path = cfgUtils.setupMapperLogging(cfg)

    try:
        runAvaScenMapper(cfg)
    except Exception:
        log.exception("Step 17: Avalanche Scenario Mapper failed.")
        return 1

    baseDir = Path(cfg.get("PATHS", "baseDir", fallback=str(Path.cwd())))
    log.info("Avalanche Scenario Mapper log saved at: %s\n", relPath(log_path, baseDir))
    return 0


# --------------------------- MAIN RUNNER --------------------------- #
if __name__ == "__main__":
    raise SystemExit(main())