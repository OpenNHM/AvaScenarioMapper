# ───────────────────────────────────────────────────────────────────────────────────────────────
#    ███████  A V A L A N C H E · S C E N E N A R I O · M A P P E R   ██████████████████
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
#   13_avaScenMaps/avaScen_<Scenario>.parquet / .geojson / .gpkg / .csv
#
# Config  :
#   avaScenMapperCfg.ini + local_avaScenMapperCfg.ini
#   [WORKFLOW], [PATHS], [OUTPUT], [FILTER], [FILTER.*]
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
#   2026-03 - 1.3
#
# ---------------------------------------------------------------------------------- #

# ------------------ System imports ------------------ #
import sys
import time
import logging
import configparser
from pathlib import Path
from typing import List, Dict, Optional, Tuple

import geopandas as gpd

# ------------------ Core utilities ------------------ #
import in1Utils.cfgUtils as cfgUtils
import in1Utils.mapperUtils as mapperUtils
from in1Utils.cfgUtils import relPath

# ------------------ Components ------------------ #
import com3AvaScenFilter.avaScenFilter as avaScenFilter
import in2Matrix.avaPotMatrix as avaPotMatrix
#import in1Utils.caamlUtils as caamlUtils  # noqa: F401
import out1Utils.mapperOutUtils as mapperOutUtils

# ------------------ Logger ------------------ #
log = logging.getLogger(__name__)


# ------------------ Small runner helpers ------------------ #
def parseBaseDirs(cfg: configparser.ConfigParser) -> List[Path]:
    """
    Parse baseDir config; supports one or multiple absolute paths (newline separated).
    """
    baseDirRaw = cfg.get("PATHS", "baseDir", fallback="").strip()
    if not baseDirRaw:
        return []
    return [Path(b.strip()).expanduser() for b in baseDirRaw.splitlines() if b.strip()]


def logStartBanner(stepName: str, stepId: str) -> None:
    log.info(
        "\n\n"
        "       ==============================================================================\n"
        f"          ... Start {stepName} ({stepId})  ({time.strftime('%Y-%m-%d %H:%M:%S')}) ...\n"
        "       ==============================================================================\n"
    )


def collectCriteriaToRun(
    areaCriteriaList: List[Dict],
    scenMapsDir: Path,
    baseDir: Path,
    outputMode: str,
    scenarioFileTypes: List[str],
) -> List[Dict]:
    """
    Determine which scenarios still need to be processed.
    In masterOnly mode all scenarios are processed.
    """
    if outputMode == "masterOnly":
        return areaCriteriaList

    criteriaToRun: List[Dict] = []
    skipped = 0

    for crit in areaCriteriaList:
        scenName = crit.get("name", "unnamed")
        scenNameClean = mapperUtils.sanitizeScenarioName(scenName)

        outputs = mapperUtils.buildScenarioOutputPaths(
            scenMapsDir=scenMapsDir,
            scenNameClean=scenNameClean,
            writeParquet=("parquet" in scenarioFileTypes),
            writeGeoJson=("geojson" in scenarioFileTypes),
            writeGpkg=("gpkg" in scenarioFileTypes),
            writeCsv=("csv" in scenarioFileTypes),
        )

        existing = next((p for p in outputs if p and p.exists()), None)
        if existing:
            skipped += 1
            log.info("Skipping scenario '%s' (already exists: %s)", scenName, relPath(existing, baseDir))
            continue

        criteriaToRun.append(crit)

    if skipped:
        log.info("Skipped %d scenario(s) because outputs already exist.", skipped)

    return criteriaToRun


def filterScenarios(
    avaResultsPath: Path,
    cfg: configparser.ConfigParser,
    criteriaToRun: List[Dict],
    avaLegend,
    deleteColumns: List[str],
) -> List[Tuple[Dict, gpd.GeoDataFrame]]:
    """
    Run scenario filtering either in full-load or chunked mode depending on input size.
    """
    fileSizeGb = avaResultsPath.stat().st_size / (1024 ** 3)
    useChunked = fileSizeGb >= 1.0

    if useChunked:
        log.info(
            "Input parquet is large (%.2f GB) -> using chunked row-group filtering.",
            fileSizeGb,
        )
        return mapperOutUtils.runScenarioFiltersChunked(
            avaResultsPath=avaResultsPath,
            cfg=cfg,
            criteriaToRun=criteriaToRun,
            avaLegend=avaLegend,
            deleteColumns=deleteColumns,
        )

    log.info(
        "Input parquet is moderate (%.2f GB) -> using normal full-load filtering.",
        fileSizeGb,
    )

    gdf = mapperUtils.readGdf(avaResultsPath)
    gdf = mapperUtils.dropConfiguredColumns(gdf, deleteColumns)
    gdf = mapperUtils.normalizeAvaCols(gdf)

    if not mapperUtils.checkInputData(gdf, avaResultsPath, cfg):
        log.error("Step 17 aborted: input dataset incomplete or invalid.")
        return []

    return avaScenFilter.runScenarioFilters(gdf, criteriaToRun, avaLegend)


def enrichScenarioOutputs(
    results: List[Tuple[Dict, gpd.GeoDataFrame]],
    deleteColumns: List[str],
    addScenarioNameField: bool,
) -> List[Tuple[Dict, gpd.GeoDataFrame]]:
    """
    Apply shared output cleanup before writing.
    """
    enrichedResults: List[Tuple[Dict, gpd.GeoDataFrame]] = []

    for crit, gdfOut in results:
        scenName = mapperUtils.sanitizeScenarioName(crit.get("name", "unnamed"))
        gdfOut = mapperOutUtils.prepareScenarioOutputForWrite(
            gdfOut,
            scenName,
            deleteColumns,
            addScenarioNameField=addScenarioNameField,
        )
        enrichedResults.append((crit, gdfOut))

    return enrichedResults


def writeScenarioOutputs(
    enrichedResults: List[Tuple[Dict, gpd.GeoDataFrame]],
    scenMapsDir: Path,
    scenarioFileTypes: List[str],
    csvWkt: bool,
) -> None:
    """
    Write scenario outputs in the configured formats.
    """
    for crit, gdfOut in enrichedResults:
        scenName = mapperUtils.sanitizeScenarioName(crit.get("name", "unnamed"))
        mapperOutUtils.writeOutputsByFileTypes(
            gdfOut,
            scenMapsDir,
            f"avaScen_{scenName}",
            scenarioFileTypes,
            csvWkt=csvWkt,
        )


# --------------------------- MAIN FUNCTION --------------------------- #
def runAvaScenMapper(
    cfg: configparser.ConfigParser,
    paths: Optional[dict] = None,
    areaCriteriaList: Optional[List[Dict]] = None,
) -> None:
    """
    Main entry point for the Avalanche Scenario Mapper (Step 17).
    """
    t0 = time.perf_counter()
    logStartBanner("Avalanche Scenario Mapper", "Step 17")

    if paths is None:
        paths = mapperUtils.resolvePaths(cfg)

    avaResultsPath = Path(paths["avaDirectoryResultsParquet"])
    scenMapsDir = Path(paths["avaScenMapsDir"])
    baseDir = Path(paths.get("baseDir", scenMapsDir.parent))

    log.info("Input AvaDirectoryResults : %s", relPath(avaResultsPath, baseDir))
    log.info("Output AvaScenMaps folder : %s", relPath(scenMapsDir, baseDir))

    if not mapperUtils.handleAvaDirCheckMode(cfg, avaResultsPath):
        return

    avaLegend = avaPotMatrix.avaPotMatrix()
    log.info("Step 17: Avalanche Distribution–Size matrix loaded (%d entries)", len(avaLegend))

    if areaCriteriaList is None:
        if cfg.getboolean("WORKFLOW", "mapperUseCaaml", fallback=False):
            log.info("Step 17: CAAML integration requested (not yet implemented).")
            areaCriteriaList = []
        else:
            areaCriteriaList = mapperUtils.parseFilterConfig(cfg)

    if not areaCriteriaList:
        log.warning("Step 17: No scenarios configured -> exiting Mapper.")
        return

    outputCfg = mapperOutUtils.parseOutputConfig(cfg)
    outputMode = outputCfg["outputMode"]
    scenarioFileTypes = outputCfg["scenarioFileTypes"]
    masterFileTypes = outputCfg["masterFileTypes"]
    deleteColumns = outputCfg["deleteColumns"]
    addScenarioNameField = outputCfg["addScenarioNameField"]
    allowScenarioDuplicatesInMaster = outputCfg["allowScenarioDuplicatesInMaster"]
    csvWkt = outputCfg["csvWkt"]

    log.info("Output mode: %s", outputMode)
    log.info("Scenario file types: %s", ", ".join(scenarioFileTypes) if scenarioFileTypes else "<none>")
    log.info("Master file types: %s", ", ".join(masterFileTypes) if masterFileTypes else "<none>")
    log.info("Master duplicate handling: allowScenarioDuplicatesInMaster=%s", allowScenarioDuplicatesInMaster)
    log.info("Add scenarioName field: %s", addScenarioNameField)
    log.info("Configured deleteColumns: %s", ", ".join(deleteColumns) if deleteColumns else "<none>")

    criteriaToRun = collectCriteriaToRun(
        areaCriteriaList=areaCriteriaList,
        scenMapsDir=scenMapsDir,
        baseDir=baseDir,
        outputMode=outputMode,
        scenarioFileTypes=scenarioFileTypes,
    )

    if not criteriaToRun:
        log.warning("Nothing to process -> exiting Mapper.")
        return

    fileSizeGb = avaResultsPath.stat().st_size / (1024 ** 3)
    useChunked = fileSizeGb >= 1.0

    if useChunked and outputMode == "masterOnly":
        masterName = mapperUtils.getMasterName(cfg, baseDir)
        mapperOutUtils.streamMasterOnlyChunked(
            avaResultsPath=avaResultsPath,
            cfg=cfg,
            criteriaToRun=criteriaToRun,
            avaLegend=avaLegend,
            scenMapsDir=scenMapsDir,
            baseDir=baseDir,
            masterName=masterName,
            allowScenarioDuplicatesInMaster=allowScenarioDuplicatesInMaster,
            deleteColumns=deleteColumns,
            addScenarioNameField=addScenarioNameField,
            masterFileTypes=masterFileTypes,
        )
        log.info("Step 17 finished in %.2fs", time.perf_counter() - t0)
        return

    results = filterScenarios(
        avaResultsPath=avaResultsPath,
        cfg=cfg,
        criteriaToRun=criteriaToRun,
        avaLegend=avaLegend,
        deleteColumns=deleteColumns,
    )

    if not results:
        log.warning("No scenario produced output.")
        return

    enrichedResults = enrichScenarioOutputs(
        results=results,
        deleteColumns=deleteColumns,
        addScenarioNameField=addScenarioNameField,
    )

    if outputMode in {"scenarioOnly", "scenarioAndMaster"}:
        writeScenarioOutputs(
            enrichedResults=enrichedResults,
            scenMapsDir=scenMapsDir,
            scenarioFileTypes=scenarioFileTypes,
            csvWkt=csvWkt,
        )
    else:
        log.info("Per-scenario outputs skipped (outputMode = masterOnly).")

    if outputMode in {"scenarioAndMaster", "masterOnly"}:
        masterName = mapperUtils.getMasterName(cfg, baseDir)

        if outputMode == "masterOnly":
            masterDir = scenMapsDir / masterName
            masterDir.mkdir(parents=True, exist_ok=True)

            masterParts = [gdfOut for _, gdfOut in enrichedResults if gdfOut is not None and not gdfOut.empty]
            if not masterParts:
                log.warning("Master-only: no scenario rows available.")
                return

            master = gpd.GeoDataFrame(pd.concat(masterParts, ignore_index=True), crs=masterParts[0].crs)

            if not allowScenarioDuplicatesInMaster:
                master["__master_hash"] = mapperOutUtils.buildMasterRowHashStrings(
                    master,
                    ignoreCols=["scenario", "scenarioName"],
                )

                scenOrder = [mapperUtils.sanitizeScenarioName(str(crit.get("name", "unnamed"))) for crit, _ in enrichedResults]
                outRows = []

                for _, grp in master.groupby("__master_hash", sort=False):
                    first = grp.iloc[[0]].copy()
                    scenPresent = set(grp["scenarioName"].astype(str).tolist()) if "scenarioName" in grp.columns else set()
                    if "scenarioName" in first.columns:
                        first.loc[first.index[0], "scenarioName"] = ", ".join([s for s in scenOrder if s in scenPresent])
                    outRows.append(first)

                master = gpd.GeoDataFrame(pd.concat(outRows, ignore_index=True), crs=master.crs)
                master = master.drop(columns=["__master_hash"])

            written = mapperOutUtils.writeMasterDatasetParts(masterDir, master, rowsPerPart=250000)
            log.info(
                "Master-only: parquet dataset complete: %s (parts=%d, rows=%d)",
                relPath(masterDir, baseDir),
                len(written),
                len(master),
            )

            if "gpkg" in masterFileTypes:
                outGpkg = masterDir / f"{masterName}.gpkg"
                log.info("Master-only: exporting GPKG (layers by scenario): %s", relPath(outGpkg, baseDir))
                mapperOutUtils.exportMasterDatasetToGpkgByScenario(masterDir, outGpkg)

        else:
            scenarioNames = [str(crit.get("name", "unnamed")) for crit, _ in enrichedResults]
            mapperOutUtils.buildMasterFromScenarioParquets(
                scenMapsDir=scenMapsDir,
                scenarioNames=scenarioNames,
                baseDir=baseDir,
                cfg=cfg,
                masterFileTypes=masterFileTypes,
                csvWkt=csvWkt,
                allowScenarioDuplicatesInMaster=allowScenarioDuplicatesInMaster,
            )

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
    logPath = cfgUtils.setupMapperLogging(cfg)

    mapperPathMode = cfg.get(
        "WORKFLOW",
        "mapperPathMode",
        fallback="AvaScenDirectory",
    ).strip().lower()

    if mapperPathMode == "custompaths":
        avaResultsRaw = cfg.get("PATHS", "avaDirectoryResults", fallback="").strip()
        scenMapsRaw = cfg.get("PATHS", "avaScenMapsDir", fallback="").strip()

        if not avaResultsRaw:
            log.error("No avaDirectoryResults configured in [PATHS] for customPaths mode.")
            return 1
        if not scenMapsRaw:
            log.error("No avaScenMapsDir configured in [PATHS] for customPaths mode.")
            return 1

        avaResultsPath = Path(avaResultsRaw).expanduser()
        scenMapsDir = Path(scenMapsRaw).expanduser()

        if not avaResultsPath.exists():
            log.error("avaDirectoryResults does not exist: %s", avaResultsPath)
            return 1

        scenMapsDir.mkdir(parents=True, exist_ok=True)

        log.info("Running Avalanche Scenario Mapper in customPaths mode:")
        log.info("  avaDirectoryResults: %s", avaResultsPath)
        log.info("  avaScenMapsDir     : %s", scenMapsDir)

        try:
            runAvaScenMapper(cfg)
        except Exception:
            log.exception("Scenario Mapper failed in customPaths mode.")
            return 1

        log.info("Avalanche Scenario Mapper log saved at: %s\n", logPath)
        return 0

    baseDirs = parseBaseDirs(cfg)
    if not baseDirs:
        log.error("No baseDir configured in [PATHS] for AvaScenDirectory mode.")
        return 1

    ranAny = False

    for baseDir in baseDirs:
        if not baseDir.exists():
            log.error("baseDir does not exist: %s", baseDir)
            continue

        ranAny = True
        log.info("Running Avalanche Scenario Mapper for baseDir:")
        log.info("  %s", baseDir)

        cfgRun = configparser.ConfigParser()
        cfgRun.read_dict({s: dict(cfg[s]) for s in cfg.sections()})

        if not cfgRun.has_section("PATHS"):
            cfgRun.add_section("PATHS")
        cfgRun.set("PATHS", "baseDir", str(baseDir))

        try:
            runAvaScenMapper(cfgRun)
        except Exception:
            log.exception("Scenario Mapper failed for baseDir: %s", baseDir)

    if not ranAny:
        log.error("No valid baseDir could be processed.")
        return 1

    log.info("Avalanche Scenario Mapper log saved at: %s\n", logPath)
    return 0


# --------------------------- MAIN RUNNER --------------------------- #
if __name__ == "__main__":
    raise SystemExit(main())