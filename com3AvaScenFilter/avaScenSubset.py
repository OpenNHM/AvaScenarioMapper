# --------------------------- com3AvaScenFilter/avaScenSubset.py --------------------------- #
#
# Purpose :
#   Build a spatial subset of avaDirectoryResults before normal scenario filtering.
#
#   Subset logic:
#       - spatially evaluate only modType = "rel" rows
#       - keep rel rows that are within OR intersecting the subset mask
#       - derive surviving logical result keys via (praID, resultID)
#       - reattach all rows from the full dataset for those keys
#         so paired res rows are preserved automatically
#
# Output :
#   A subset avaDirectoryResults dataset written to one or more formats
#   (parquet recommended as canonical internal format).
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
# ----------------------------------------------------------------------------------------- #

import json
import logging
from pathlib import Path
from typing import Dict, List, Sequence, Set, Tuple

import pandas as pd
import geopandas as gpd
import pyarrow as pa
import pyarrow.parquet as pq

import in1Utils.mapperUtils as mapperUtils
from in1Utils.cfgUtils import relPath

log = logging.getLogger(__name__)

pairKeys = ["praID", "resultID"]


# ------------------ Small helpers ------------------ #
def _parseFileTypes(fileTypesRaw: str | Sequence[str] | None) -> List[str]:
    """
    Parse supported subset output file types.
    Allowed: parquet, gpkg, csv, geojson
    """
    if fileTypesRaw is None:
        return ["parquet"]

    if isinstance(fileTypesRaw, str):
        vals = [v.strip().lower() for v in fileTypesRaw.split(",") if v.strip()]
    else:
        vals = [str(v).strip().lower() for v in fileTypesRaw if str(v).strip()]

    allowed = {"parquet", "gpkg", "csv", "geojson"}
    out: List[str] = []
    for v in vals:
        if v in allowed and v not in out:
            out.append(v)

    return out or ["parquet"]


def _deriveSubsetStem(sourceAvaResultsPath: Path, subsetAreaName: str) -> str:
    """
    Derive subset output stem from source avaDirectoryResults file name.

    Example:
      avaDirectoryResults_EUREGIO.parquet
      -> avaDirectoryResults_pilotStubai
    """
    subsetAreaNameClean = mapperUtils.sanitizeScenarioName(subsetAreaName)
    stem = sourceAvaResultsPath.stem

    if "_" in stem:
        prefix = stem.split("_")[0]
        return f"{prefix}_{subsetAreaNameClean}"

    return f"{stem}_{subsetAreaNameClean}"


def _buildSubsetOutputPaths(
    sourceAvaResultsPath: Path,
    subsetAreaName: str,
    subsetAvaDirectoryDir: Path,
    subsetFileTypes: Sequence[str],
) -> Dict[str, Path | None]:
    """
    Build output paths for subset AvaDirectory dataset.
    """
    subsetAvaDirectoryDir.mkdir(parents=True, exist_ok=True)
    stem = _deriveSubsetStem(sourceAvaResultsPath, subsetAreaName)

    return {
        "parquet": (subsetAvaDirectoryDir / f"{stem}.parquet") if "parquet" in subsetFileTypes else None,
        "gpkg": (subsetAvaDirectoryDir / f"{stem}.gpkg") if "gpkg" in subsetFileTypes else None,
        "geojson": (subsetAvaDirectoryDir / f"{stem}.geojson") if "geojson" in subsetFileTypes else None,
        "csv": (subsetAvaDirectoryDir / f"{stem}.csv") if "csv" in subsetFileTypes else None,
    }


def _getSubsetMask(maskPath: Path) -> gpd.GeoDataFrame:
    """
    Read subset mask.
    """
    if not maskPath.exists():
        raise FileNotFoundError(f"Subset mask not found: {maskPath}")

    maskGdf = gpd.read_file(maskPath)
    if maskGdf.empty:
        raise ValueError(f"Subset mask is empty: {maskPath}")

    if maskGdf.crs is None:
        raise ValueError(f"Subset mask has no CRS: {maskPath}")

    return maskGdf


def _readGeoRowGroupSelective(parquetPath: Path, rowGroupIdx: int, columns: Sequence[str] | None = None) -> gpd.GeoDataFrame:
    """
    Read one GeoParquet row group as GeoDataFrame, optionally selecting columns.
    Geometry + CRS are preserved from GeoParquet metadata.
    """
    pf = pq.ParquetFile(parquetPath)
    table = pf.read_row_group(rowGroupIdx, columns=list(columns) if columns else None)

    metadata = table.schema.metadata or {}
    if b"geo" not in metadata:
        raise ValueError(f"Missing GeoParquet metadata in {parquetPath}")

    geo = json.loads(metadata[b"geo"].decode("utf-8"))
    geomCol = geo.get("primary_column", "geometry")
    geomMeta = geo.get("columns", {}).get(geomCol, {})
    encoding = str(geomMeta.get("encoding", "")).lower()

    df = table.to_pandas()

    if geomCol not in df.columns:
        raise ValueError(f"Geometry column '{geomCol}' not found in row group {rowGroupIdx}")

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


def _normalizePairCols(df: pd.DataFrame) -> pd.DataFrame:
    """
    Normalize pair key columns for stable matching.
    """
    out = df.copy()

    if "praID" in out.columns:
        out["praID"] = pd.to_numeric(out["praID"], errors="coerce").astype("Int64")
    if "resultID" in out.columns:
        out["resultID"] = out["resultID"].astype(str).str.strip()

    return out


def _pairSeries(df: pd.DataFrame) -> pd.Series:
    """
    Build stable tuple series of (praID, resultID).
    """
    tmp = _normalizePairCols(df[pairKeys])
    return tmp.apply(lambda r: (str(r["praID"]), str(r["resultID"])), axis=1)


def _collectSubsetKeepPairsChunked(
    sourceAvaResultsPath: Path,
    subsetMaskPath: Path,
) -> Set[Tuple[str, str]]:
    """
    Pass 1:
    Read only minimal columns row-group by row-group, spatially inspect only rel rows,
    and collect surviving (praID, resultID) keys.
    """
    pf = pq.ParquetFile(sourceAvaResultsPath)
    numRowGroups = pf.num_row_groups

    log.info("Subset pass 1 started: row_groups=%d", numRowGroups)

    maskGdfRaw = _getSubsetMask(subsetMaskPath)
    keepPairs: Set[Tuple[str, str]] = set()

    totalRows = 0
    totalRelRows = 0
    totalKeptRelRows = 0
    maskUnion = None
    targetMaskCrs = None

    readCols = ["praID", "resultID", "modType", "geometry"]

    for rgIdx in range(numRowGroups):
        log.info("Subset pass 1: reading row group %d/%d", rgIdx + 1, numRowGroups)

        gdfChunk = _readGeoRowGroupSelective(
            parquetPath=sourceAvaResultsPath,
            rowGroupIdx=rgIdx,
            columns=readCols,
        )
        gdfChunk = mapperUtils.normalizeAvaCols(gdfChunk)

        if not all(c in gdfChunk.columns for c in pairKeys):
            raise ValueError(f"Subset preprocessing requires pair key columns {pairKeys} in row group {rgIdx}")

        if "modType" not in gdfChunk.columns:
            raise ValueError(f"Subset preprocessing requires column 'modType' in row group {rgIdx}")

        totalRows += len(gdfChunk)

        relChunk = gdfChunk[gdfChunk["modType"].astype(str).str.lower().str.strip().eq("rel")].copy()
        totalRelRows += len(relChunk)

        if relChunk.empty:
            continue

        if targetMaskCrs is None:
            if relChunk.crs is None:
                raise ValueError("Source avaDirectoryResults has no CRS; cannot align subset mask.")
            targetMaskCrs = relChunk.crs

            if str(maskGdfRaw.crs) != str(targetMaskCrs):
                log.info("Reprojecting subset mask from %s to %s", maskGdfRaw.crs, targetMaskCrs)
                maskGdf = maskGdfRaw.to_crs(targetMaskCrs)
            else:
                maskGdf = maskGdfRaw.copy()

            maskUnion = maskGdf.geometry.union_all()

        keepMask = relChunk.geometry.within(maskUnion) | relChunk.geometry.intersects(maskUnion)
        keptRelChunk = relChunk.loc[keepMask].copy()
        totalKeptRelRows += len(keptRelChunk)

        if keptRelChunk.empty:
            continue

        pairChunk = _pairSeries(keptRelChunk[pairKeys])
        keepPairs.update(pairChunk.tolist())

    log.info(
        "Subset pass 1 finished: rows_total=%d, rel_total=%d, rel_kept=%d, keep_pairs=%d",
        totalRows,
        totalRelRows,
        totalKeptRelRows,
        len(keepPairs),
    )

    return keepPairs


def _writeSubsetParquetChunked(
    sourceAvaResultsPath: Path,
    outParquet: Path,
    keepPairs: Set[Tuple[str, str]],
) -> None:
    """
    Pass 2:
    Re-read source parquet row-group by row-group, keep all rows whose
    (praID, resultID) are in keepPairs, and write one output parquet.
    """
    if outParquet.exists():
        outParquet.unlink()
    outParquet.parent.mkdir(parents=True, exist_ok=True)

    pf = pq.ParquetFile(sourceAvaResultsPath)
    numRowGroups = pf.num_row_groups

    keptChunks: List[gpd.GeoDataFrame] = []
    totalWrittenRows = 0

    log.info("Subset pass 2 started: rebuilding subset parquet in memory from kept chunks")

    for rgIdx in range(numRowGroups):
        log.info("Subset pass 2: reading row group %d/%d", rgIdx + 1, numRowGroups)

        gdfChunk = _readGeoRowGroupSelective(
            parquetPath=sourceAvaResultsPath,
            rowGroupIdx=rgIdx,
            columns=None,
        )
        gdfChunk = mapperUtils.normalizeAvaCols(gdfChunk)

        if not all(c in gdfChunk.columns for c in pairKeys):
            raise ValueError(f"Subset preprocessing requires pair key columns {pairKeys} in row group {rgIdx}")

        pairChunk = _pairSeries(gdfChunk[pairKeys])
        keepMask = pairChunk.isin(keepPairs)

        subsetChunk = gdfChunk.loc[keepMask].copy()
        if subsetChunk.empty:
            continue

        keptChunks.append(subsetChunk)
        totalWrittenRows += len(subsetChunk)

    if keptChunks:
        subsetGdf = gpd.GeoDataFrame(
            pd.concat(keptChunks, ignore_index=True),
            geometry="geometry",
            crs=keptChunks[0].crs,
        )
    else:
        emptyChunk = _readGeoRowGroupSelective(
            parquetPath=sourceAvaResultsPath,
            rowGroupIdx=0,
            columns=None,
        ).iloc[0:0].copy()
        subsetGdf = gpd.GeoDataFrame(emptyChunk, geometry="geometry", crs=emptyChunk.crs)

    subsetGdf.to_parquet(outParquet, index=False)

    log.info("Subset pass 2 finished: written_rows=%d", totalWrittenRows)


def _exportSubsetSecondaryOutputs(
    outParquet: Path,
    outPaths: Dict[str, Path | None],
) -> None:
    """
    Export secondary output formats after canonical parquet is built.
    """
    gdf = mapperUtils.readGdf(outParquet)

    mapperUtils.writeScenarioOutputs(
        gdf,
        outParquet=None,
        outGeoJson=outPaths["geojson"],
        outGpkg=outPaths["gpkg"],
        outCsv=outPaths["csv"],
        csvWkt=False,
    )


# ------------------ Public API ------------------ #
def buildSubsetAvaDirectory(
    sourceAvaResultsPath: Path,
    subsetMaskPath: Path,
    subsetAreaName: str,
    subsetAvaDirectoryDir: Path,
    subsetFileTypes: Sequence[str] | str = ("parquet",),
    baseDir: Path | None = None,
) -> Path:
    """
    Build subset AvaDirectory dataset and return canonical parquet path.
    """
    subsetFileTypes = _parseFileTypes(subsetFileTypes)
    if "parquet" not in subsetFileTypes:
        subsetFileTypes = ["parquet"] + [ft for ft in subsetFileTypes if ft != "parquet"]

    outPaths = _buildSubsetOutputPaths(
        sourceAvaResultsPath=sourceAvaResultsPath,
        subsetAreaName=subsetAreaName,
        subsetAvaDirectoryDir=subsetAvaDirectoryDir,
        subsetFileTypes=subsetFileTypes,
    )
    outParquet = outPaths["parquet"]

    log.info("Subset preprocessing started: %s", subsetAreaName)
    log.info("Source avaDirectoryResults : %s", relPath(sourceAvaResultsPath, baseDir) if baseDir else sourceAvaResultsPath)
    log.info("Subset mask               : %s", relPath(subsetMaskPath, baseDir) if baseDir else subsetMaskPath)
    log.info("Subset parquet output     : %s", relPath(outParquet, baseDir) if baseDir else outParquet)

    keepPairs = _collectSubsetKeepPairsChunked(
        sourceAvaResultsPath=sourceAvaResultsPath,
        subsetMaskPath=subsetMaskPath,
    )

    _writeSubsetParquetChunked(
        sourceAvaResultsPath=sourceAvaResultsPath,
        outParquet=outParquet,
        keepPairs=keepPairs,
    )

    subsetGdf = mapperUtils.readGdf(outParquet)
    subsetGdf = mapperUtils.normalizeAvaCols(subsetGdf)

    log.info(
        "Subset preprocessing finished: subset rows = %d (rel=%d, res=%d)",
        len(subsetGdf),
        int((subsetGdf["modType"] == "rel").sum()) if "modType" in subsetGdf.columns else 0,
        int((subsetGdf["modType"] == "res").sum()) if "modType" in subsetGdf.columns else 0,
    )

    if any(outPaths[k] is not None for k in ("gpkg", "geojson", "csv")):
        _exportSubsetSecondaryOutputs(outParquet, outPaths)

    return outParquet


def ensureSubsetAvaDirectory(
    sourceAvaResultsPath: Path,
    subsetMaskPath: Path,
    subsetAreaName: str,
    subsetAvaDirectoryDir: Path,
    subsetFileTypes: Sequence[str] | str = ("parquet",),
    baseDir: Path | None = None,
    reuseExisting: bool = True,
) -> Path:
    """
    Reuse existing subset parquet if present, otherwise build it.
    Returns canonical subset parquet path.
    """
    subsetFileTypes = _parseFileTypes(subsetFileTypes)
    if "parquet" not in subsetFileTypes:
        subsetFileTypes = ["parquet"] + [ft for ft in subsetFileTypes if ft != "parquet"]

    outPaths = _buildSubsetOutputPaths(
        sourceAvaResultsPath=sourceAvaResultsPath,
        subsetAreaName=subsetAreaName,
        subsetAvaDirectoryDir=subsetAvaDirectoryDir,
        subsetFileTypes=subsetFileTypes,
    )
    outParquet = outPaths["parquet"]

    if reuseExisting and outParquet.exists():
        log.info(
            "Reusing existing subset AvaDirectory parquet: %s",
            relPath(outParquet, baseDir) if baseDir else outParquet,
        )

        if any(outPaths[k] is not None and not outPaths[k].exists() for k in ("gpkg", "geojson", "csv")):
            log.info("Subset parquet exists, creating missing secondary outputs.")
            _exportSubsetSecondaryOutputs(outParquet, outPaths)

        return outParquet

    return buildSubsetAvaDirectory(
        sourceAvaResultsPath=sourceAvaResultsPath,
        subsetMaskPath=subsetMaskPath,
        subsetAreaName=subsetAreaName,
        subsetAvaDirectoryDir=subsetAvaDirectoryDir,
        subsetFileTypes=subsetFileTypes,
        baseDir=baseDir,
    )