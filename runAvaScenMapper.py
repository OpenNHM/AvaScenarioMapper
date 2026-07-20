
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
#   Step 16 of the Avalanche Scenario Model Chain.
#   Filters avaDirectoryResults.parquet into scenario-specific outputs
#   for visualization, mapping, and publication.
#
# Inputs  :
#   12_avaDirectory/avaDirectoryResults.parquet
# Outputs :
#   13_avaScenMaps/avaScen_<Scenario>.parquet / .geojson / .gpkg / .csv
#
# Config  :
#   avaScenMapperCfg.ini + local_avaScenMapperCfg.ini
#   [WORKFLOW], [PATHS], [OUTPUT], [SUBSET], [FILTER], [FILTER.*]
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
#   2026-03 - 1.5
#
# ---------------------------------------------------------------------------------- #

# ------------------ System imports ------------------ #
import sys
import time
import logging
import configparser
from pathlib import Path
from typing import List, Dict, Optional, Tuple

import pandas as pd
import geopandas as gpd

# ------------------ Core utilities ------------------ #
import in1Utils.cfgUtils as cfgUtils
import in1Utils.mapperUtils as mapperUtils
from in1Utils.cfgUtils import relPath

# ------------------ Components ------------------ #
import com3AvaScenFilter.avaScenFilter as avaScenFilter
import com3AvaScenFilter.avaScenSubset as avaScenSubset
import in2Matrix.avaPotMatrix as avaPotMatrix
import out1Utils.mapperOutUtils as mapperOutUtils
import out1Utils.scenarioRasterUtils as scenarioRasterUtils

# ------------------ Logger ------------------ #
log = logging.getLogger(__name__)


# ------------------ Small runner helpers ------------------ #
def logStartBanner(stepName: str, stepId: str) -> None:
    log.info(
        "\n\n"
        "       ==============================================================================\n"
        f"          ... Start {stepName} ({stepId})  ({time.strftime('%Y-%m-%d %H:%M:%S')}) ...\n"
        "       ==============================================================================\n"
    )


def resolveSubsetInputPath(
    cfg: configparser.ConfigParser,
    fullAvaResultsPath: Path,
    baseDir: Path,
) -> Path:
    """
    Resolve actual input parquet for filtering.
    If subset preprocessing is enabled, create / reuse subset AvaDirectory parquet
    and return that path. Otherwise return the original full AvaDirectory parquet.
    """
    subsetEnabled = cfg.getboolean("SUBSET", "enableSubsetAreaMask", fallback=False)

    if not subsetEnabled:
        log.info("Subset preprocessing disabled -> using full AvaDirectoryResults input.")
        return fullAvaResultsPath

    subsetAreaName = cfg.get("SUBSET", "subsetAreaName", fallback="").strip()
    subsetAreaPathRaw = cfg.get("SUBSET", "subsetAreaPath", fallback="").strip()
    subsetAvaDirectoryDirRaw = cfg.get("SUBSET", "subsetAvaDirectoryDir", fallback="").strip()
    if not subsetAvaDirectoryDirRaw:
        subsetAvaDirectoryDirRaw = cfg.get(
            "SUBSET", "subsetAreaAvaDirectoryOut", fallback=""
        ).strip()
    subsetAvaDirectoryFileTypes = cfg.get("SUBSET", "subsetAvaDirectoryFileTypes", fallback="parquet").strip()

    if not subsetAreaName:
        raise ValueError("SUBSET enabled but subsetAreaName is empty.")
    if not subsetAreaPathRaw:
        raise ValueError("SUBSET enabled but subsetAreaPath is empty.")
    if not subsetAvaDirectoryDirRaw:
        raise ValueError("SUBSET enabled but subsetAvaDirectoryDir is empty.")

    subsetAreaPath = Path(subsetAreaPathRaw).expanduser()
    subsetAvaDirectoryDir = Path(subsetAvaDirectoryDirRaw).expanduser()

    log.info("Subset preprocessing enabled: subsetAreaName=%s", subsetAreaName)
    log.info("Subset mask path           : %s", relPath(subsetAreaPath, baseDir))
    log.info("Subset AvaDirectory dir    : %s", relPath(subsetAvaDirectoryDir, baseDir))
    log.info("Subset AvaDirectory types  : %s", subsetAvaDirectoryFileTypes)

    subsetAvaResultsPath = avaScenSubset.ensureSubsetAvaDirectory(
        sourceAvaResultsPath=fullAvaResultsPath,
        subsetMaskPath=subsetAreaPath,
        subsetAreaName=subsetAreaName,
        subsetAvaDirectoryDir=subsetAvaDirectoryDir,
        subsetFileTypes=subsetAvaDirectoryFileTypes,
        baseDir=baseDir,
        reuseExisting=True,
    )

    log.info("Subset AvaDirectoryResults : %s", relPath(subsetAvaResultsPath, baseDir))
    return subsetAvaResultsPath


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
        baseName = f"avaScen_{scenNameClean}"
        scenarioDir = scenMapsDir / baseName

        outputPaths = mapperOutUtils.buildOutputPaths(
            scenarioDir,
            baseName,
            scenarioFileTypes,
        )
        outputs = [path for path in outputPaths.values() if path is not None]
        outputs.append(scenarioDir / f"{baseName}.json")

        if outputs and all(path.exists() for path in outputs):
            skipped += 1
            log.info("Skipping scenario '%s' (all requested feature outputs exist)", scenName)
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
        log.error("Step 16 aborted: input dataset incomplete or invalid.")
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
        baseName = f"avaScen_{scenName}"
        scenarioDir = scenMapsDir / baseName
        outputPaths = mapperOutUtils.buildOutputPaths(
            scenarioDir,
            baseName,
            scenarioFileTypes,
        )
        requestedPaths = [path for path in outputPaths.values() if path is not None]
        if requestedPaths and all(path.exists() for path in requestedPaths):
            log.info("Scenario feature outputs already complete, skipping: %s", scenName)
            continue
        mapperOutUtils.writeOutputsByFileTypes(
            gdfOut,
            scenarioDir,
            baseName,
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
    Main entry point for the Avalanche Scenario Mapper (Step 16).
    """
    t0 = time.perf_counter()
    logStartBanner("Avalanche Scenario Mapper", "Step 16")

    if paths is None:
        paths = mapperUtils.resolvePaths(cfg)

    fullAvaResultsPath = Path(paths["avaDirectoryResultsParquet"])
    scenMapsDir = Path(paths["avaScenMapsDir"])
    baseDir = Path(paths.get("baseDir", scenMapsDir.parent))

    avaResultsPath = resolveSubsetInputPath(
        cfg=cfg,
        fullAvaResultsPath=fullAvaResultsPath,
        baseDir=baseDir,
    )

    log.info("Full AvaDirectoryResults   : %s", relPath(fullAvaResultsPath, baseDir))
    log.info("Active filtering input     : %s", relPath(avaResultsPath, baseDir))
    log.info("Output AvaScenMaps folder  : %s", relPath(scenMapsDir, baseDir))

    if not mapperUtils.handleAvaDirCheckMode(cfg, avaResultsPath):
        return

    avaLegend = avaPotMatrix.avaPotMatrix()
    log.info("Step 16: Avalanche Distribution–Size matrix loaded (%d entries)", len(avaLegend))

    if areaCriteriaList is None:
        if cfg.getboolean("WORKFLOW", "mapperUseCaaml", fallback=False):
            log.info("Step 16: CAAML integration requested (not yet implemented).")
            areaCriteriaList = []
        else:
            areaCriteriaList = mapperUtils.parseFilterConfig(cfg)

    if not areaCriteriaList:
        log.warning("Step 16: No scenarios configured -> exiting Mapper.")
        return

    outputCfg = mapperOutUtils.parseOutputConfig(cfg)
    mapScenFeatures = cfg.getboolean("WORKFLOW", "mapScenFeatures", fallback=True)
    mapScenRasters = cfg.getboolean("WORKFLOW", "mapScenRasters", fallback=False)

    if not mapScenFeatures and not mapScenRasters:
        log.warning("Both mapScenFeatures and mapScenRasters are False -> nothing to create.")
        return

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
    log.info("Map scenario features: %s", mapScenFeatures)
    log.info("Map scenario rasters : %s", mapScenRasters)
    log.info("Configured deleteColumns: %s", ", ".join(deleteColumns) if deleteColumns else "<none>")

    if mapScenRasters:
        criteriaToRun = areaCriteriaList
    else:
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

    if useChunked and outputMode == "masterOnly" and mapScenFeatures and not mapScenRasters:
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
        log.info("Step 16 finished in %.2fs", time.perf_counter() - t0)
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

    for crit, _ in results:
        scenarioName = f"avaScen_{mapperUtils.sanitizeScenarioName(crit.get('name', 'unnamed'))}"
        scenarioDir = scenMapsDir / scenarioName
        cfgUtils.writeConfigSnapshot(
            cfg,
            scenarioDir / f"{scenarioName}.json",
            scenarioName=scenarioName,
            filterSection=crit.get("_filterSection"),
        )

    if mapScenRasters:
        rasterConfig = scenarioRasterUtils.parseRasterConfig(cfg)
        dataRoot = scenarioRasterUtils.deriveDataRoot(fullAvaResultsPath)
        log.info("Scenario raster data root: %s", dataRoot)

        for crit, scenarioGdf in results:
            scenarioName = f"avaScen_{mapperUtils.sanitizeScenarioName(crit.get('name', 'unnamed'))}"
            scenarioRasterUtils.makeScenarioRasters(
                scenarioGdf=scenarioGdf,
                scenarioName=scenarioName,
                scenMapsDir=scenMapsDir,
                pathBaseDir=avaResultsPath.parent,
                dataRoot=dataRoot,
                rasterConfig=rasterConfig,
            )

    if not mapScenFeatures:
        log.info("Scenario feature output disabled; raster stage complete.")
        log.info("Step 16 finished in %.2fs", time.perf_counter() - t0)
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

    log.info("Step 16 finished in %.2fs", time.perf_counter() - t0)


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

    try:
        paths = mapperUtils.resolvePaths(cfg)
        runAvaScenMapper(cfg, paths=paths)
    except (FileNotFoundError, ValueError) as exc:
        log.error("Invalid Mapper configuration: %s", exc)
        return 1
    except Exception:
        log.exception("Scenario Mapper failed.")
        return 1

    log.info("Avalanche Scenario Mapper log saved at: %s\n", logPath)
    return 0


# --------------------------- MAIN RUNNER --------------------------- #
if __name__ == "__main__":
    raise SystemExit(main())
