# --------------------------- out1Utils/mapperOutUtils.py --------------------------- #
#
# Purpose :
#   Shared output and master-building utilities for the
#   Avalanche Scenario Mapper.
#
#   Provides:
#     - OUTPUT config parsing
#     - scenario/master output path helpers
#     - master row hashing and dedup staging
#     - parquet dataset writing
#     - chunked scenario filtering helpers
#     - master-only streaming helpers
#
# Author :
#   Christoph Hesselbach
#
# Institution :
#   Austrian Research Centre for Forests (BFW)
#   Department of Natural Hazards | Snow and Avalanche Unit
#
# Date & Version :
#   2026-03 - 1.0
#
# ---------------------------------------------------------------------------------- #

import shutil
import sqlite3
import logging
import configparser
from pathlib import Path
from typing import List, Dict, Optional, Sequence, Tuple

import pandas as pd
import geopandas as gpd
import pyarrow.parquet as pq

import in1Utils.mapperUtils as mapperUtils
from in1Utils.cfgUtils import relPath
import com3AvaScenFilter.avaScenFilter as avaScenFilter

log = logging.getLogger(__name__)


# ------------------ Output config parsing ------------------ #

def parseFileTypeList(raw: str) -> List[str]:
    """
    Parse comma-separated output file types.

    Allowed:
      parquet, gpkg, geojson, csv
    """
    allowed = {"parquet", "gpkg", "geojson", "csv"}
    out: List[str] = []

    for token in str(raw or "").split(","):
        value = token.strip().lower()
        if not value:
            continue
        if value not in allowed:
            log.warning("Ignoring unsupported file type: %s", value)
            continue
        if value not in out:
            out.append(value)

    return out


def parseOutputConfig(cfg: configparser.ConfigParser) -> Dict:
    """
    Parse new [OUTPUT] config with fallback to old [WORKFLOW] booleans.
    """
    outputMode = cfg.get("OUTPUT", "outputMode", fallback="").strip()

    if outputMode:
        outputMode = outputMode.strip()
    else:
        makeMaster = cfg.getboolean("WORKFLOW", "mapperMakeMaster", fallback=False)
        makeMasterOnly = cfg.getboolean("WORKFLOW", "mapperOnlyMaster", fallback=False)

        if makeMasterOnly:
            outputMode = "masterOnly"
        elif makeMaster:
            outputMode = "scenarioAndMaster"
        else:
            outputMode = "scenarioOnly"

    scenarioFileTypes = parseFileTypeList(
        cfg.get("OUTPUT", "scenarioFileTypes", fallback="")
    )
    masterFileTypes = parseFileTypeList(
        cfg.get("OUTPUT", "masterFileTypes", fallback="")
    )

    if not scenarioFileTypes:
        if cfg.getboolean("WORKFLOW", "writeScenarioParquet", fallback=True):
            scenarioFileTypes.append("parquet")
        if cfg.getboolean("WORKFLOW", "writeScenarioGpkg", fallback=False):
            scenarioFileTypes.append("gpkg")
        if cfg.getboolean("WORKFLOW", "writeScenarioGeoJson", fallback=False):
            scenarioFileTypes.append("geojson")
        if cfg.getboolean("WORKFLOW", "writeScenarioCsv", fallback=False):
            scenarioFileTypes.append("csv")

    if not masterFileTypes:
        if cfg.getboolean("WORKFLOW", "mapperMakeMaster", fallback=False):
            if cfg.getboolean("WORKFLOW", "writeScenarioParquet", fallback=True):
                masterFileTypes.append("parquet")
            if cfg.getboolean("WORKFLOW", "exportMasterGpkg", fallback=False):
                masterFileTypes.append("gpkg")

    deleteColumnsRaw = cfg.get("OUTPUT", "deleteColumns", fallback="").strip()
    if not deleteColumnsRaw:
        deleteColumns = mapperUtils.parseDeleteColumns(cfg)
    else:
        cols = [c.strip() for c in deleteColumnsRaw.split(",") if c.strip()]
        cols = list(dict.fromkeys(cols))
        protected = {"scenarioName", "geometry"}
        removed = [c for c in cols if c in protected]
        if removed:
            log.warning("Ignoring protected column(s) in deleteColumns: %s", ", ".join(removed))
        deleteColumns = [c for c in cols if c not in protected]

    addScenarioNameField = cfg.getboolean("OUTPUT", "addScenarioNameField", fallback=True)
    allowScenarioDuplicatesInMaster = cfg.getboolean(
        "OUTPUT",
        "allowScenarioDuplicatesInMaster",
        fallback=cfg.getboolean("WORKFLOW", "allowScenarioDuplicatesInMaster", fallback=True),
    )
    csvWkt = cfg.getboolean("WORKFLOW", "writeScenarioCsvWkt", fallback=False)

    if outputMode not in {"scenarioOnly", "scenarioAndMaster", "masterOnly"}:
        raise ValueError(f"Unsupported OUTPUT.outputMode: {outputMode}")

    if outputMode == "scenarioAndMaster" and "parquet" not in scenarioFileTypes:
        log.warning("scenarioAndMaster requires scenario parquet staging -> adding parquet to scenarioFileTypes.")
        scenarioFileTypes = ["parquet"] + [ft for ft in scenarioFileTypes if ft != "parquet"]

    if outputMode == "masterOnly" and "parquet" not in masterFileTypes:
        log.warning("masterOnly requires parquet master staging -> adding parquet to masterFileTypes.")
        masterFileTypes = ["parquet"] + [ft for ft in masterFileTypes if ft != "parquet"]

    return {
        "outputMode": outputMode,
        "scenarioFileTypes": scenarioFileTypes,
        "masterFileTypes": masterFileTypes,
        "deleteColumns": deleteColumns,
        "addScenarioNameField": addScenarioNameField,
        "allowScenarioDuplicatesInMaster": allowScenarioDuplicatesInMaster,
        "csvWkt": csvWkt,
    }


# ------------------ Output write helpers ------------------ #

def prepareScenarioOutputForWrite(
    gdf: gpd.GeoDataFrame,
    scenarioName: str,
    deleteColumns: Sequence[str],
    addScenarioNameField: bool,
) -> gpd.GeoDataFrame:
    """
    Apply common output cleanup and optionally remove scenarioName afterward.
    """
    gdf = mapperUtils.prepareScenarioOutput(gdf, scenarioName, deleteColumns)

    if gdf is not None and not gdf.empty and not addScenarioNameField:
        gdf = gdf.drop(columns=["scenarioName"], errors="ignore")

    return gdf


def buildOutputPaths(
    outDir: Path,
    baseName: str,
    fileTypes: Sequence[str],
) -> Dict[str, Optional[Path]]:
    """
    Build output paths from a base name and fileTypes list.
    """
    fileTypeSet = set(fileTypes)

    return {
        "parquet": (outDir / f"{baseName}.parquet") if "parquet" in fileTypeSet else None,
        "gpkg": (outDir / f"{baseName}.gpkg") if "gpkg" in fileTypeSet else None,
        "geojson": (outDir / f"{baseName}.geojson") if "geojson" in fileTypeSet else None,
        "csv": (outDir / f"{baseName}.csv") if "csv" in fileTypeSet else None,
    }


def writeOutputsByFileTypes(
    gdf: gpd.GeoDataFrame,
    outDir: Path,
    baseName: str,
    fileTypes: Sequence[str],
    csvWkt: bool,
) -> None:
    paths = buildOutputPaths(outDir, baseName, fileTypes)

    mapperUtils.writeScenarioOutputs(
        gdf,
        outParquet=paths["parquet"],
        outGeoJson=paths["geojson"],
        outGpkg=paths["gpkg"],
        outCsv=paths["csv"],
        csvWkt=csvWkt,
    )


# ------------------ Master hash / parquet parts ------------------ #

def buildMasterRowHashStrings(
    gdf: gpd.GeoDataFrame,
    ignoreCols: Optional[Sequence[str]] = None,
) -> pd.Series:
    """
    Build a stable row hash for 'same feature' comparison across scenarios.
    Returned as fixed-width hex strings for safe sqlite storage.
    """
    if ignoreCols is None:
        ignoreCols = ["scenario", "scenarioName"]

    ignoreSet = set(ignoreCols)
    compareCols = [c for c in gdf.columns if c not in ignoreSet and c != "geometry"]

    keyDf = gdf[compareCols].copy()

    if "geometry" in gdf.columns:
        keyDf["__geometry_wkb__"] = gdf.geometry.to_wkb(hex=True)

    hashes = pd.util.hash_pandas_object(keyDf, index=False)
    return hashes.map(lambda x: f"{int(x):016x}")


def writeMasterPart(masterDir: Path, gdfPart: gpd.GeoDataFrame, partIdx: int) -> Path:
    """
    Write one parquet part into master dataset folder.
    """
    masterDir.mkdir(parents=True, exist_ok=True)
    out = masterDir / f"part-{partIdx:05d}.parquet"
    mapperUtils.writeScenarioOutputs(
        gdfPart,
        outParquet=out,
        outGeoJson=None,
        outGpkg=None,
        outCsv=None,
        csvWkt=False,
    )
    return out


def writeMasterDatasetParts(
    masterDir: Path,
    gdfMaster: gpd.GeoDataFrame,
    rowsPerPart: int = 250000,
) -> List[Path]:
    """
    Write master as parquet dataset folder in multiple parts.
    """
    masterDir.mkdir(parents=True, exist_ok=True)

    for p in masterDir.glob("part-*.parquet"):
        p.unlink()

    paths = []
    n = len(gdfMaster)
    if n == 0:
        return paths

    partIdx = 0
    for start in range(0, n, rowsPerPart):
        stop = min(start + rowsPerPart, n)
        gdfPart = gdfMaster.iloc[start:stop].copy()
        out = writeMasterPart(masterDir, gdfPart, partIdx)
        paths.append(out)
        partIdx += 1

    return paths


def exportMasterDatasetToGpkgByScenario(masterDir: Path, outGpkg: Path) -> None:
    """
    Export parquet dataset folder -> single GeoPackage with one layer per scenario.
    Uses the 'scenarioName' column as grouping key if available.
    """
    parts = sorted(masterDir.glob("part-*.parquet"))
    if not parts:
        log.warning("Master export: no parquet parts found in %s", masterDir)
        return

    groups: Dict[str, List[Path]] = {}
    for p in parts:
        try:
            g = mapperUtils.readGdf(p)
            if "scenarioName" in g.columns and len(g) > 0:
                scen = str(g["scenarioName"].iloc[0])
            elif "scenario" in g.columns and len(g) > 0:
                scen = str(g["scenario"].iloc[0])
            else:
                scen = "unknown"
            groups.setdefault(scen, []).append(p)
        except Exception:
            log.exception("Master export: failed reading part %s", p)

    if not groups:
        log.warning("Master export: nothing readable in %s", masterDir)
        return

    if outGpkg.exists():
        outGpkg.unlink()

    for scen, plist in sorted(groups.items(), key=lambda kv: kv[0]):
        gdfs = []
        for p in plist:
            try:
                gdfs.append(mapperUtils.readGdf(p))
            except Exception:
                log.exception("Master export: failed reading %s", p)

        if not gdfs:
            continue

        gdf = gpd.GeoDataFrame(pd.concat(gdfs, ignore_index=True), crs=gdfs[0].crs)
        layer = mapperUtils.sanitizeScenarioName(scen)[:55] or "scenario"
        log.info("Master export: writing layer '%s' (rows=%d)", layer, len(gdf))
        gdf.to_file(outGpkg, layer=layer, driver="GPKG")

    log.info("Master export finished: %s", outGpkg)


# ------------------ SQLite staging helpers ------------------ #

def initMasterSqlite(dbPath: Path) -> sqlite3.Connection:
    if dbPath.exists():
        dbPath.unlink()

    conn = sqlite3.connect(dbPath)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA temp_store=MEMORY;")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS hash_scenarios (
            hash TEXT NOT NULL,
            scenario TEXT NOT NULL,
            PRIMARY KEY (hash, scenario)
        )
        """
    )
    return conn


def registerHashScenarios(
    conn: sqlite3.Connection,
    hashes: Sequence[str],
    scenarioName: str,
) -> None:
    if not hashes:
        return

    uniqueHashes = list(dict.fromkeys(hashes))
    rows = [(h, scenarioName) for h in uniqueHashes]
    with conn:
        conn.executemany(
            "INSERT OR IGNORE INTO hash_scenarios(hash, scenario) VALUES (?, ?)",
            rows,
        )


def fetchScenarioNamesForHashes(
    conn: sqlite3.Connection,
    hashes: Sequence[str],
    scenarioOrder: Sequence[str],
    batchSize: int = 900,
) -> Dict[str, str]:
    """
    Return mapping:
        hash -> "scenarioA, scenarioB, ..."
    preserving config scenario order.
    """
    if not hashes:
        return {}

    wanted = list(dict.fromkeys(hashes))
    rawMap: Dict[str, set] = {}

    for batch in mapperUtils.batched(wanted, batchSize):
        placeholders = ",".join("?" for _ in batch)
        sql = f"SELECT hash, scenario FROM hash_scenarios WHERE hash IN ({placeholders})"
        cur = conn.execute(sql, list(batch))
        for h, scen in cur.fetchall():
            rawMap.setdefault(h, set()).add(scen)

    out: Dict[str, str] = {}
    for h, scenSet in rawMap.items():
        ordered = [s for s in scenarioOrder if s in scenSet]
        out[h] = ", ".join(ordered)

    return out


def writeTempMasterPart(tempDir: Path, gdfPart: gpd.GeoDataFrame, partIdx: int) -> Path:
    tempDir.mkdir(parents=True, exist_ok=True)
    out = tempDir / f"temp-part-{partIdx:05d}.parquet"
    mapperUtils.writeScenarioOutputs(
        gdfPart,
        outParquet=out,
        outGeoJson=None,
        outGpkg=None,
        outCsv=None,
        csvWkt=False,
    )
    return out


def buildMasterFromTempPartsDedup(
    tempDir: Path,
    masterDir: Path,
    dbPath: Path,
    scenarioOrder: Sequence[str],
    baseDir: Path,
) -> int:
    """
    Second pass:
    - read temp parts sequentially
    - keep first occurrence of each hash
    - fill aggregated scenarioName from sqlite
    - write final master parts incrementally
    """
    parts = sorted(tempDir.glob("temp-part-*.parquet"))
    if not parts:
        log.warning("No temp master parts found in %s", tempDir)
        return 0

    conn = sqlite3.connect(dbPath)
    seenHashes = set()
    outPartIdx = 0
    writtenRows = 0

    try:
        for p in parts:
            gdf = mapperUtils.readGdf(p)
            if gdf.empty:
                continue

            hashes = gdf["__master_hash"].astype(str).tolist()

            keepMask = []
            keptHashes = []

            for h in hashes:
                if h in seenHashes:
                    keepMask.append(False)
                else:
                    keepMask.append(True)
                    seenHashes.add(h)
                    keptHashes.append(h)

            if not any(keepMask):
                continue

            gdfKeep = gdf.loc[keepMask].copy()
            namesMap = fetchScenarioNamesForHashes(conn, keptHashes, scenarioOrder=scenarioOrder)

            gdfKeep["scenarioName"] = [
                namesMap.get(h, scenName)
                for h, scenName in zip(gdfKeep["__master_hash"].astype(str), gdfKeep["scenarioName"].astype(str))
            ]

            gdfKeep = gdfKeep.drop(columns=["__master_hash"])

            out = writeMasterPart(masterDir, gdfKeep, outPartIdx)
            log.info("Master dedup: wrote %s (rows=%d)", relPath(out, baseDir), len(gdfKeep))
            writtenRows += len(gdfKeep)
            outPartIdx += 1

        return writtenRows

    finally:
        conn.close()


# ------------------ Chunked filtering helpers ------------------ #

def runScenarioFiltersChunked(
    avaResultsPath: Path,
    cfg: configparser.ConfigParser,
    criteriaToRun: List[Dict],
    avaLegend,
    deleteColumns: Sequence[str],
) -> List[Tuple[Dict, gpd.GeoDataFrame]]:
    """
    Process a large GeoParquet row-group by row-group and concatenate
    filtered results per scenario.
    """
    pf = pq.ParquetFile(avaResultsPath)
    numRowGroups = pf.num_row_groups

    log.info(
        "Large input detected -> chunked filtering enabled "
        "(row_groups=%d, rows_total=%d)",
        numRowGroups,
        pf.metadata.num_rows,
    )

    scenarioChunks: Dict[str, List[gpd.GeoDataFrame]] = {
        str(crit.get("name", "unnamed")): [] for crit in criteriaToRun
    }
    criteriaByName: Dict[str, Dict] = {
        str(crit.get("name", "unnamed")): crit for crit in criteriaToRun
    }

    totalRowsIn = 0

    for rgIdx in range(numRowGroups):
        log.info("Reading row group %d/%d", rgIdx + 1, numRowGroups)

        gdfChunk = mapperUtils.readGdfRowGroup(
            avaResultsPath,
            rgIdx,
            dropCols=deleteColumns,
        )
        gdfChunk = mapperUtils.normalizeAvaCols(gdfChunk)

        if not mapperUtils.checkInputData(gdfChunk, avaResultsPath, cfg):
            raise ValueError(f"Input validation failed for row group {rgIdx}")

        totalRowsIn += len(gdfChunk)
        log.info("Row group %d rows: %d", rgIdx + 1, len(gdfChunk))

        chunkResults = avaScenFilter.runScenarioFilters(gdfChunk, criteriaToRun, avaLegend)

        for crit, gdfOut in chunkResults:
            scenName = str(crit.get("name", "unnamed"))
            if gdfOut is not None and not gdfOut.empty:
                gdfOut = mapperUtils.dropConfiguredColumns(gdfOut, deleteColumns)
                scenarioChunks[scenName].append(gdfOut)

    log.info("Chunked input processing complete. Total input rows processed: %d", totalRowsIn)

    finalResults: List[Tuple[Dict, gpd.GeoDataFrame]] = []

    for scenName, gdfParts in scenarioChunks.items():
        crit = criteriaByName[scenName]

        if not gdfParts:
            log.info("Scenario '%s': no matches", scenName)
            continue

        gdfFinal = gpd.GeoDataFrame(
            pd.concat(gdfParts, ignore_index=True),
            geometry="geometry",
            crs=gdfParts[0].crs,
        )

        log.info(
            "Scenario '%s': concatenated %d chunk(s), total matched rows=%d",
            scenName,
            len(gdfParts),
            len(gdfFinal),
        )

        finalResults.append((crit, gdfFinal))

    return finalResults


# ------------------ Master-only chunked streaming ------------------ #

def streamMasterOnlyChunked(
    avaResultsPath: Path,
    cfg: configparser.ConfigParser,
    criteriaToRun: List[Dict],
    avaLegend,
    scenMapsDir: Path,
    baseDir: Path,
    masterName: str,
    allowScenarioDuplicatesInMaster: bool,
    deleteColumns: Sequence[str],
    addScenarioNameField: bool,
    masterFileTypes: Sequence[str],
) -> None:
    """
    Optimized path for:
      large input + masterOnly

    This avoids building all scenario outputs in RAM.
    """
    pf = pq.ParquetFile(avaResultsPath)
    numRowGroups = pf.num_row_groups

    log.info(
        "Large input detected -> streaming master-only filtering enabled "
        "(row_groups=%d, rows_total=%d)",
        numRowGroups,
        pf.metadata.num_rows,
    )

    masterDir = scenMapsDir / masterName
    masterDir.mkdir(parents=True, exist_ok=True)

    for p in masterDir.glob("part-*.parquet"):
        p.unlink()

    scenarioOrder = [mapperUtils.sanitizeScenarioName(str(crit.get("name", "unnamed"))) for crit in criteriaToRun]
    scenarioStats = {name: {"chunks": 0, "rows": 0} for name in scenarioOrder}

    if allowScenarioDuplicatesInMaster:
        outPartIdx = 0

        for rgIdx in range(numRowGroups):
            log.info("Reading row group %d/%d", rgIdx + 1, numRowGroups)

            gdfChunk = mapperUtils.readGdfRowGroup(
                avaResultsPath,
                rgIdx,
                dropCols=deleteColumns,
            )
            gdfChunk = mapperUtils.normalizeAvaCols(gdfChunk)

            if not mapperUtils.checkInputData(gdfChunk, avaResultsPath, cfg):
                raise ValueError(f"Input validation failed for row group {rgIdx}")

            log.info("Row group %d rows: %d", rgIdx + 1, len(gdfChunk))

            chunkResults = avaScenFilter.runScenarioFilters(gdfChunk, criteriaToRun, avaLegend)

            for crit, gdfOut in chunkResults:
                scenName = mapperUtils.sanitizeScenarioName(crit.get("name", "unnamed"))
                if gdfOut is None or gdfOut.empty:
                    continue

                gdfOut = prepareScenarioOutputForWrite(
                    gdfOut,
                    scenName,
                    deleteColumns,
                    addScenarioNameField=addScenarioNameField,
                )
                out = writeMasterPart(masterDir, gdfOut, outPartIdx)

                scenarioStats[scenName]["chunks"] += 1
                scenarioStats[scenName]["rows"] += len(gdfOut)

                log.info(
                    "Master-only stream: wrote %s for scenario '%s' (rows=%d)",
                    relPath(out, baseDir),
                    scenName,
                    len(gdfOut),
                )
                outPartIdx += 1

        for scenName in scenarioOrder:
            st = scenarioStats[scenName]
            if st["rows"] > 0:
                log.info(
                    "Scenario '%s': streamed %d chunk(s), total matched rows=%d",
                    scenName,
                    st["chunks"],
                    st["rows"],
                )
            else:
                log.warning("Scenario '%s' produced no results", scenName)

        if "gpkg" in masterFileTypes:
            outGpkg = masterDir / f"{masterName}.gpkg"
            log.info("Master-only: exporting GPKG (layers by scenario): %s", relPath(outGpkg, baseDir))
            exportMasterDatasetToGpkgByScenario(masterDir, outGpkg)

        return

    tempDir = masterDir / "_tmp_master_build"
    dbPath = masterDir / "_tmp_master_hashes.sqlite"

    if tempDir.exists():
        shutil.rmtree(tempDir, ignore_errors=True)
    tempDir.mkdir(parents=True, exist_ok=True)

    if dbPath.exists():
        dbPath.unlink()

    conn = initMasterSqlite(dbPath)
    tempPartIdx = 0

    try:
        for rgIdx in range(numRowGroups):
            log.info("Reading row group %d/%d", rgIdx + 1, numRowGroups)

            gdfChunk = mapperUtils.readGdfRowGroup(
                avaResultsPath,
                rgIdx,
                dropCols=deleteColumns,
            )
            gdfChunk = mapperUtils.normalizeAvaCols(gdfChunk)

            if not mapperUtils.checkInputData(gdfChunk, avaResultsPath, cfg):
                raise ValueError(f"Input validation failed for row group {rgIdx}")

            log.info("Row group %d rows: %d", rgIdx + 1, len(gdfChunk))

            chunkResults = avaScenFilter.runScenarioFilters(gdfChunk, criteriaToRun, avaLegend)

            for crit, gdfOut in chunkResults:
                scenName = mapperUtils.sanitizeScenarioName(crit.get("name", "unnamed"))
                if gdfOut is None or gdfOut.empty:
                    continue

                gdfOut = prepareScenarioOutputForWrite(
                    gdfOut,
                    scenName,
                    deleteColumns,
                    addScenarioNameField=addScenarioNameField,
                )
                gdfOut["__master_hash"] = buildMasterRowHashStrings(
                    gdfOut,
                    ignoreCols=["scenario", "scenarioName"],
                )

                registerHashScenarios(
                    conn,
                    gdfOut["__master_hash"].astype(str).tolist(),
                    scenName,
                )

                tempOut = writeTempMasterPart(tempDir, gdfOut, tempPartIdx)
                log.info(
                    "Master-only temp: wrote %s for scenario '%s' (rows=%d)",
                    relPath(tempOut, baseDir),
                    scenName,
                    len(gdfOut),
                )

                scenarioStats[scenName]["chunks"] += 1
                scenarioStats[scenName]["rows"] += len(gdfOut)
                tempPartIdx += 1

        conn.close()

        for scenName in scenarioOrder:
            st = scenarioStats[scenName]
            if st["rows"] > 0:
                log.info(
                    "Scenario '%s': streamed %d temp chunk(s), total matched rows=%d",
                    scenName,
                    st["chunks"],
                    st["rows"],
                )
            else:
                log.warning("Scenario '%s' produced no results", scenName)

        writtenRows = buildMasterFromTempPartsDedup(
            tempDir=tempDir,
            masterDir=masterDir,
            dbPath=dbPath,
            scenarioOrder=scenarioOrder,
            baseDir=baseDir,
        )

        log.info(
            "Master-only dedup: parquet dataset complete: %s (rows=%d)",
            relPath(masterDir, baseDir),
            writtenRows,
        )

        if "gpkg" in masterFileTypes:
            outGpkg = masterDir / f"{masterName}.gpkg"
            log.info("Master-only: exporting GPKG (layers by scenario): %s", relPath(outGpkg, baseDir))
            exportMasterDatasetToGpkgByScenario(masterDir, outGpkg)

    finally:
        try:
            conn.close()
        except Exception:
            pass

        shutil.rmtree(tempDir, ignore_errors=True)
        if dbPath.exists():
            dbPath.unlink()


# ------------------ Build master from scenario parquet ------------------ #

def buildMasterFromScenarioParquets(
    scenMapsDir: Path,
    scenarioNames: Sequence[str],
    baseDir: Path,
    cfg: configparser.ConfigParser,
    masterFileTypes: Sequence[str],
    csvWkt: bool,
    allowScenarioDuplicatesInMaster: bool,
) -> None:
    """
    Build master by reading per-scenario parquet files from disk.
    Used for scenarioAndMaster mode.
    """
    masterName = mapperUtils.getMasterName(cfg, baseDir)

    parquetPaths: List[Path] = []
    for scenName in scenarioNames:
        scenNameClean = mapperUtils.sanitizeScenarioName(scenName)
        p = scenMapsDir / f"avaScen_{scenNameClean}.parquet"
        if p.exists():
            parquetPaths.append(p)
        else:
            log.warning("Master build: missing scenario parquet: %s", relPath(p, baseDir))

    if not parquetPaths:
        log.warning("Master build: no scenario parquets found -> skipping master.")
        return

    gdfs: List[gpd.GeoDataFrame] = []
    for p in parquetPaths:
        try:
            gdfPart = mapperUtils.readGdf(p)
            gdfs.append(gdfPart)
        except Exception:
            log.exception("Master build: failed reading %s", relPath(p, baseDir))

    if not gdfs:
        log.warning("Master build: no readable scenario outputs -> skipping master.")
        return

    master = gpd.GeoDataFrame(pd.concat(gdfs, ignore_index=True), crs=gdfs[0].crs)

    if not allowScenarioDuplicatesInMaster:
        master["__master_hash"] = buildMasterRowHashStrings(
            master,
            ignoreCols=["scenario", "scenarioName"],
        )

        scenOrder = list(dict.fromkeys(master["scenarioName"].astype(str).tolist()))
        outRows = []

        for _, grp in master.groupby("__master_hash", sort=False):
            first = grp.iloc[[0]].copy()
            scenPresent = set(grp["scenarioName"].astype(str).tolist())
            first.loc[first.index[0], "scenarioName"] = ", ".join([s for s in scenOrder if s in scenPresent])
            outRows.append(first)

        master = gpd.GeoDataFrame(pd.concat(outRows, ignore_index=True), crs=master.crs)
        master = master.drop(columns=["__master_hash"])

    writeOutputsByFileTypes(
        master,
        scenMapsDir,
        masterName,
        masterFileTypes,
        csvWkt=csvWkt,
    )

    mapperUtils.logScenarioSummary(master, masterName)