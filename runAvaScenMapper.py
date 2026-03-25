

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
import json
import shutil
import sqlite3
import logging
import configparser
from pathlib import Path
from typing import List, Dict, Optional, Sequence, Tuple, Iterable

import pandas as pd
import geopandas as gpd
import pyarrow.parquet as pq

# ------------------ Core utilities ------------------ #
import in1Utils.cfgUtils as cfgUtils
import in1Utils.mapperUtils as mapperUtils
from in1Utils.cfgUtils import relPath

# ------------------ Components ------------------ #
import com3AvaScenFilter.avaScenFilter as avaScenFilter
import in2Matrix.avaPotMatrix as avaPotMatrix
import in1Utils.caamlUtils as caamlUtils  # noqa: F401

# ------------------ Logger ------------------ #
log = logging.getLogger(__name__)


# ------------------ Helpers ------------------ #
def sanitizeScenarioName(scenName: str) -> str:
    """Keep only alnum, dash, underscore. Never return empty."""
    scenName = str(scenName or "unnamed")
    scenNameClean = "".join(ch for ch in scenName if ch.isalnum() or ch in "-_")
    return scenNameClean or "unnamed"


def parseBaseDirs(cfg: configparser.ConfigParser) -> List[Path]:
    """Parse baseDir config; supports one or multiple absolute paths (newline separated)."""
    baseDirRaw = cfg.get("PATHS", "baseDir", fallback="").strip()
    if not baseDirRaw:
        return []
    return [Path(b.strip()).expanduser() for b in baseDirRaw.splitlines() if b.strip()]


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


def logStartBanner(stepName: str, stepId: str) -> None:
    log.info(
        "\n\n"
        "       ==============================================================================\n"
        f"          ... Start {stepName} ({stepId})  ({time.strftime('%Y-%m-%d %H:%M:%S')}) ...\n"
        "       ==============================================================================\n"
    )


def buildScenarioOutputPaths(
    scenMapsDir: Path,
    scenNameClean: str,
    writeParquet: bool,
    writeGeoJson: bool,
    writeGpkg: bool,
    writeCsv: bool,
) -> List[Optional[Path]]:
    """Build a list of output paths for existence checks."""
    return [
        (scenMapsDir / f"avaScen_{scenNameClean}.parquet") if writeParquet else None,
        (scenMapsDir / f"avaScen_{scenNameClean}.gpkg") if writeGpkg else None,
        (scenMapsDir / f"avaScen_{scenNameClean}.geojson") if writeGeoJson else None,
        (scenMapsDir / f"avaScen_{scenNameClean}.csv") if writeCsv else None,
    ]


def getMasterName(cfg: configparser.ConfigParser, baseDir: Path) -> str:
    """
    Master name format:
      avaScen_<Region><prefix>
    Example:
      avaScen_NTirol_report20260223
    """
    region = deriveRegionName(baseDir)
    prefix = cfg.get("WORKFLOW", "mapperMasterPrefix", fallback="").strip()
    return f"avaScen_{region}{prefix}"


def parseDeleteColumns(cfg: configparser.ConfigParser) -> List[str]:
    """
    Parse columns to delete from scenario outputs.
    These columns are also dropped as early as possible from the input chunk,
    so they never survive into scenario/master outputs.
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


def addScenarioColumns(gdf: gpd.GeoDataFrame, scenario_name: str) -> gpd.GeoDataFrame:
    """
    Add scenario columns to a scenario output.
    """
    if gdf is None:
        return gdf

    gdf = gdf.copy()
    gdf["scenarioName"] = scenario_name
    return gdf


def prepareScenarioOutput(
    gdf: gpd.GeoDataFrame,
    scenario_name: str,
    deleteColumns: Sequence[str],
) -> gpd.GeoDataFrame:
    """
    Drop configured removable columns, ensure no scenario column survives,
    and add scenarioName.
    """
    if gdf is None or gdf.empty:
        return gdf

    gdf = dropConfiguredColumns(gdf, deleteColumns)
    gdf = gdf.drop(columns=["scenario"], errors="ignore")
    gdf = addScenarioColumns(gdf, scenario_name)
    return gdf


def build_master_row_hash_strings(
    gdf: gpd.GeoDataFrame,
    ignore_cols: Optional[Sequence[str]] = None,
) -> pd.Series:
    """
    Build a stable row hash for 'same feature' comparison across scenarios.
    Returned as fixed-width hex strings for safe sqlite storage.
    """
    if ignore_cols is None:
        ignore_cols = ["scenario", "scenarioName"]

    ignore_set = set(ignore_cols)
    compare_cols = [c for c in gdf.columns if c not in ignore_set and c != "geometry"]

    key_df = gdf[compare_cols].copy()

    if "geometry" in gdf.columns:
        key_df["__geometry_wkb__"] = gdf.geometry.to_wkb(hex=True)

    hashes = pd.util.hash_pandas_object(key_df, index=False)
    return hashes.map(lambda x: f"{int(x):016x}")


def batched(seq: Sequence[str], batch_size: int) -> Iterable[Sequence[str]]:
    for i in range(0, len(seq), batch_size):
        yield seq[i:i + batch_size]


def write_master_part(master_dir: Path, gdf_part: gpd.GeoDataFrame, part_idx: int) -> Path:
    """Write one parquet part into master dataset folder."""
    master_dir.mkdir(parents=True, exist_ok=True)
    out = master_dir / f"part-{part_idx:05d}.parquet"
    mapperUtils.writeScenarioOutputs(
        gdf_part,
        outParquet=out,
        outGeoJson=None,
        outGpkg=None,
        outCsv=None,
        csvWkt=False,
    )
    return out


def write_master_dataset_parts(
    master_dir: Path,
    gdf_master: gpd.GeoDataFrame,
    rows_per_part: int = 250000,
) -> List[Path]:
    """
    Write master as parquet dataset folder in multiple parts.
    """
    master_dir.mkdir(parents=True, exist_ok=True)

    for p in master_dir.glob("part-*.parquet"):
        p.unlink()

    paths = []
    n = len(gdf_master)
    if n == 0:
        return paths

    part_idx = 0
    for start in range(0, n, rows_per_part):
        stop = min(start + rows_per_part, n)
        gdf_part = gdf_master.iloc[start:stop].copy()
        out = write_master_part(master_dir, gdf_part, part_idx)
        paths.append(out)
        part_idx += 1

    return paths


def export_master_dataset_to_gpkg_by_scenario(master_dir: Path, out_gpkg: Path) -> None:
    """
    Export parquet dataset folder -> single GeoPackage with one layer per scenario.
    Uses the 'scenario' column as primary grouping key.
    """
    parts = sorted(master_dir.glob("part-*.parquet"))
    if not parts:
        log.warning("Master export: no parquet parts found in %s", master_dir)
        return

    groups: Dict[str, List[Path]] = {}
    for p in parts:
        try:
            g = mapperUtils.readGdf(p)
            if "scenario" in g.columns and len(g) > 0:
                scen = str(g["scenario"].iloc[0])
            elif "scenarioName" in g.columns and len(g) > 0:
                scen = str(g["scenarioName"].iloc[0])
            else:
                scen = "unknown"
            groups.setdefault(scen, []).append(p)
        except Exception:
            log.exception("Master export: failed reading part %s", p)

    if not groups:
        log.warning("Master export: nothing readable in %s", master_dir)
        return

    if out_gpkg.exists():
        out_gpkg.unlink()

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
        layer = sanitizeScenarioName(scen)[:55] or "scenario"
        log.info("Master export: writing layer '%s' (rows=%d)", layer, len(gdf))
        gdf.to_file(out_gpkg, layer=layer, driver="GPKG")

    log.info("Master export finished: %s", out_gpkg)


def read_geo_row_group(
    parquet_path: Path,
    row_group_idx: int,
    drop_cols: Optional[Sequence[str]] = None,
) -> gpd.GeoDataFrame:
    """
    Read a single GeoParquet row group as GeoDataFrame.
    Preserves geometry and CRS from GeoParquet metadata.
    Also drops configured removable columns immediately after loading.
    """
    pf = pq.ParquetFile(parquet_path)
    table = pf.read_row_group(row_group_idx)

    metadata = table.schema.metadata or {}
    if b"geo" not in metadata:
        raise ValueError(f"Missing GeoParquet metadata in {parquet_path}")

    geo = json.loads(metadata[b"geo"].decode("utf-8"))
    geom_col = geo.get("primary_column", "geometry")
    geom_meta = geo.get("columns", {}).get(geom_col, {})
    encoding = str(geom_meta.get("encoding", "")).lower()

    df = table.to_pandas()

    if drop_cols:
        cols_to_drop = [c for c in drop_cols if c in df.columns and c != geom_col]
        if cols_to_drop:
            df = df.drop(columns=cols_to_drop)

    if geom_col not in df.columns:
        raise ValueError(f"Geometry column '{geom_col}' not found in row group {row_group_idx}")

    if encoding == "wkb":
        geometry = gpd.GeoSeries.from_wkb(df[geom_col], crs=None)
    elif encoding == "wkt":
        geometry = gpd.GeoSeries.from_wkt(df[geom_col], crs=None)
    else:
        raise ValueError(
            f"Unsupported geometry encoding '{encoding}' in {parquet_path}. "
            f"Expected 'WKB' or 'WKT'."
        )

    df = df.drop(columns=[geom_col])
    gdf = gpd.GeoDataFrame(df, geometry=geometry)

    crs = geom_meta.get("crs")
    if crs:
        gdf.set_crs(crs, inplace=True, allow_override=True)

    return gdf


def _init_master_sqlite(db_path: Path) -> sqlite3.Connection:
    if db_path.exists():
        db_path.unlink()

    conn = sqlite3.connect(db_path)
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


def _register_hash_scenarios(
    conn: sqlite3.Connection,
    hashes: Sequence[str],
    scenario_name: str,
) -> None:
    if not hashes:
        return

    unique_hashes = list(dict.fromkeys(hashes))
    rows = [(h, scenario_name) for h in unique_hashes]
    with conn:
        conn.executemany(
            "INSERT OR IGNORE INTO hash_scenarios(hash, scenario) VALUES (?, ?)",
            rows,
        )


def _fetch_scenario_names_for_hashes(
    conn: sqlite3.Connection,
    hashes: Sequence[str],
    scenario_order: Sequence[str],
    batch_size: int = 900,
) -> Dict[str, str]:
    """
    Return mapping:
        hash -> "scenarioA, scenarioB, ..."
    preserving config scenario order.
    """
    if not hashes:
        return {}

    wanted = list(dict.fromkeys(hashes))
    raw_map: Dict[str, set] = {}

    for batch in batched(wanted, batch_size):
        placeholders = ",".join("?" for _ in batch)
        sql = f"SELECT hash, scenario FROM hash_scenarios WHERE hash IN ({placeholders})"
        cur = conn.execute(sql, list(batch))
        for h, scen in cur.fetchall():
            raw_map.setdefault(h, set()).add(scen)

    out: Dict[str, str] = {}
    for h, scen_set in raw_map.items():
        ordered = [s for s in scenario_order if s in scen_set]
        out[h] = ", ".join(ordered)

    return out


def _write_temp_master_part(temp_dir: Path, gdf_part: gpd.GeoDataFrame, part_idx: int) -> Path:
    temp_dir.mkdir(parents=True, exist_ok=True)
    out = temp_dir / f"temp-part-{part_idx:05d}.parquet"
    mapperUtils.writeScenarioOutputs(
        gdf_part,
        outParquet=out,
        outGeoJson=None,
        outGpkg=None,
        outCsv=None,
        csvWkt=False,
    )
    return out


def _build_master_from_temp_parts_dedup(
    temp_dir: Path,
    master_dir: Path,
    db_path: Path,
    scenario_order: Sequence[str],
    baseDir: Path,
) -> int:
    """
    Second pass:
    - read temp parts sequentially
    - keep first occurrence of each hash
    - fill aggregated scenarioName from sqlite
    - write final master parts incrementally
    """
    parts = sorted(temp_dir.glob("temp-part-*.parquet"))
    if not parts:
        log.warning("No temp master parts found in %s", temp_dir)
        return 0

    conn = sqlite3.connect(db_path)
    seen_hashes = set()
    out_part_idx = 0
    written_rows = 0

    try:
        for p in parts:
            gdf = mapperUtils.readGdf(p)
            if gdf.empty:
                continue

            hashes = gdf["__master_hash"].astype(str).tolist()

            keep_mask = []
            kept_hashes = []

            for h in hashes:
                if h in seen_hashes:
                    keep_mask.append(False)
                else:
                    keep_mask.append(True)
                    seen_hashes.add(h)
                    kept_hashes.append(h)

            if not any(keep_mask):
                continue

            gdf_keep = gdf.loc[keep_mask].copy()
            names_map = _fetch_scenario_names_for_hashes(conn, kept_hashes, scenario_order=scenario_order)

            gdf_keep["scenarioName"] = [
                names_map.get(h, scen_name)
                for h, scen_name in zip(gdf_keep["__master_hash"].astype(str), gdf_keep["scenarioName"].astype(str))
            ]

            gdf_keep = gdf_keep.drop(columns=["__master_hash"])

            out = write_master_part(master_dir, gdf_keep, out_part_idx)
            log.info("Master dedup: wrote %s (rows=%d)", relPath(out, baseDir), len(gdf_keep))
            written_rows += len(gdf_keep)
            out_part_idx += 1

            del gdf
            del gdf_keep

        return written_rows

    finally:
        conn.close()


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
    exportMasterGpkg: bool,
) -> None:
    """
    Optimized path for:
      large input + mapperOnlyMaster=True

    This avoids building all scenario outputs in RAM.
    """
    pf = pq.ParquetFile(avaResultsPath)
    num_row_groups = pf.num_row_groups

    log.info(
        "Large input detected -> streaming master-only filtering enabled "
        "(row_groups=%d, rows_total=%d)",
        num_row_groups,
        pf.metadata.num_rows,
    )

    masterDir = scenMapsDir / masterName
    masterDir.mkdir(parents=True, exist_ok=True)

    for p in masterDir.glob("part-*.parquet"):
        p.unlink()

    scenario_order = [sanitizeScenarioName(str(crit.get("name", "unnamed"))) for crit in criteriaToRun]
    scenario_stats = {name: {"chunks": 0, "rows": 0} for name in scenario_order}

    if allowScenarioDuplicatesInMaster:
        out_part_idx = 0

        for rg_idx in range(num_row_groups):
            log.info("Reading row group %d/%d", rg_idx + 1, num_row_groups)

            gdf_chunk = read_geo_row_group(
                avaResultsPath,
                rg_idx,
                drop_cols=deleteColumns,
            )
            gdf_chunk = mapperUtils.normalizeAvaCols(gdf_chunk)

            if not mapperUtils.checkInputData(gdf_chunk, avaResultsPath, cfg):
                raise ValueError(f"Input validation failed for row group {rg_idx}")

            log.info("Row group %d rows: %d", rg_idx + 1, len(gdf_chunk))

            chunk_results = avaScenFilter.runScenarioFilters(gdf_chunk, criteriaToRun, avaLegend)

            for crit, gdf_out in chunk_results:
                scen_name = sanitizeScenarioName(crit.get("name", "unnamed"))
                if gdf_out is None or gdf_out.empty:
                    continue

                gdf_out = prepareScenarioOutput(gdf_out, scen_name, deleteColumns)
                out = write_master_part(masterDir, gdf_out, out_part_idx)

                scenario_stats[scen_name]["chunks"] += 1
                scenario_stats[scen_name]["rows"] += len(gdf_out)

                log.info(
                    "Master-only stream: wrote %s for scenario '%s' (rows=%d)",
                    relPath(out, baseDir),
                    scen_name,
                    len(gdf_out),
                )
                out_part_idx += 1

            del gdf_chunk
            del chunk_results

        for scen_name in scenario_order:
            st = scenario_stats[scen_name]
            if st["rows"] > 0:
                log.info(
                    "Scenario '%s': streamed %d chunk(s), total matched rows=%d",
                    scen_name,
                    st["chunks"],
                    st["rows"],
                )
            else:
                log.warning("Scenario '%s' produced no results", scen_name)

        if exportMasterGpkg:
            outGpkg = masterDir / f"{masterName}.gpkg"
            log.info("Master-only: exporting GPKG (layers by scenario): %s", relPath(outGpkg, baseDir))
            export_master_dataset_to_gpkg_by_scenario(masterDir, outGpkg)

        return

    # ------------------ deduplicating / aggregating master path ------------------ #
    tempDir = masterDir / "_tmp_master_build"
    dbPath = masterDir / "_tmp_master_hashes.sqlite"

    if tempDir.exists():
        shutil.rmtree(tempDir, ignore_errors=True)
    tempDir.mkdir(parents=True, exist_ok=True)

    if dbPath.exists():
        dbPath.unlink()

    conn = _init_master_sqlite(dbPath)
    temp_part_idx = 0

    try:
        for rg_idx in range(num_row_groups):
            log.info("Reading row group %d/%d", rg_idx + 1, num_row_groups)

            gdf_chunk = read_geo_row_group(
                avaResultsPath,
                rg_idx,
                drop_cols=deleteColumns,
            )
            gdf_chunk = mapperUtils.normalizeAvaCols(gdf_chunk)

            if not mapperUtils.checkInputData(gdf_chunk, avaResultsPath, cfg):
                raise ValueError(f"Input validation failed for row group {rg_idx}")

            log.info("Row group %d rows: %d", rg_idx + 1, len(gdf_chunk))

            chunk_results = avaScenFilter.runScenarioFilters(gdf_chunk, criteriaToRun, avaLegend)

            for crit, gdf_out in chunk_results:
                scen_name = sanitizeScenarioName(crit.get("name", "unnamed"))
                if gdf_out is None or gdf_out.empty:
                    continue

                gdf_out = prepareScenarioOutput(gdf_out, scen_name, deleteColumns)
                gdf_out["__master_hash"] = build_master_row_hash_strings(
                    gdf_out,
                    ignore_cols=["scenario", "scenarioName"],
                )

                _register_hash_scenarios(
                    conn,
                    gdf_out["__master_hash"].astype(str).tolist(),
                    scen_name,
                )

                temp_out = _write_temp_master_part(tempDir, gdf_out, temp_part_idx)
                log.info(
                    "Master-only temp: wrote %s for scenario '%s' (rows=%d)",
                    relPath(temp_out, baseDir),
                    scen_name,
                    len(gdf_out),
                )

                scenario_stats[scen_name]["chunks"] += 1
                scenario_stats[scen_name]["rows"] += len(gdf_out)
                temp_part_idx += 1

            del gdf_chunk
            del chunk_results

        conn.close()

        for scen_name in scenario_order:
            st = scenario_stats[scen_name]
            if st["rows"] > 0:
                log.info(
                    "Scenario '%s': streamed %d temp chunk(s), total matched rows=%d",
                    scen_name,
                    st["chunks"],
                    st["rows"],
                )
            else:
                log.warning("Scenario '%s' produced no results", scen_name)

        written_rows = _build_master_from_temp_parts_dedup(
            temp_dir=tempDir,
            master_dir=masterDir,
            db_path=dbPath,
            scenario_order=scenario_order,
            baseDir=baseDir,
        )

        log.info(
            "Master-only dedup: parquet dataset complete: %s (rows=%d)",
            relPath(masterDir, baseDir),
            written_rows,
        )

        if exportMasterGpkg:
            outGpkg = masterDir / f"{masterName}.gpkg"
            log.info("Master-only: exporting GPKG (layers by scenario): %s", relPath(outGpkg, baseDir))
            export_master_dataset_to_gpkg_by_scenario(masterDir, outGpkg)

    finally:
        try:
            conn.close()
        except Exception:
            pass

        shutil.rmtree(tempDir, ignore_errors=True)
        if dbPath.exists():
            dbPath.unlink()


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
    This path is kept for non-masterOnly workflows.
    """
    pf = pq.ParquetFile(avaResultsPath)
    num_row_groups = pf.num_row_groups

    log.info(
        "Large input detected -> chunked filtering enabled "
        "(row_groups=%d, rows_total=%d)",
        num_row_groups,
        pf.metadata.num_rows,
    )

    scenario_chunks: Dict[str, List[gpd.GeoDataFrame]] = {
        str(crit.get("name", "unnamed")): [] for crit in criteriaToRun
    }
    criteria_by_name: Dict[str, Dict] = {
        str(crit.get("name", "unnamed")): crit for crit in criteriaToRun
    }

    total_rows_in = 0

    for rg_idx in range(num_row_groups):
        log.info("Reading row group %d/%d", rg_idx + 1, num_row_groups)

        gdf_chunk = read_geo_row_group(
            avaResultsPath,
            rg_idx,
            drop_cols=deleteColumns,
        )
        gdf_chunk = mapperUtils.normalizeAvaCols(gdf_chunk)

        if not mapperUtils.checkInputData(gdf_chunk, avaResultsPath, cfg):
            raise ValueError(f"Input validation failed for row group {rg_idx}")

        total_rows_in += len(gdf_chunk)
        log.info("Row group %d rows: %d", rg_idx + 1, len(gdf_chunk))

        chunk_results = avaScenFilter.runScenarioFilters(gdf_chunk, criteriaToRun, avaLegend)

        for crit, gdf_out in chunk_results:
            scen_name = str(crit.get("name", "unnamed"))
            if gdf_out is not None and not gdf_out.empty:
                gdf_out = dropConfiguredColumns(gdf_out, deleteColumns)
                scenario_chunks[scen_name].append(gdf_out)

        del gdf_chunk
        del chunk_results

    log.info("Chunked input processing complete. Total input rows processed: %d", total_rows_in)

    final_results: List[Tuple[Dict, gpd.GeoDataFrame]] = []

    for scen_name, gdf_parts in scenario_chunks.items():
        crit = criteria_by_name[scen_name]

        if not gdf_parts:
            log.info("Scenario '%s': no matches", scen_name)
            continue

        gdf_final = gpd.GeoDataFrame(
            pd.concat(gdf_parts, ignore_index=True),
            geometry="geometry",
            crs=gdf_parts[0].crs,
        )

        log.info(
            "Scenario '%s': concatenated %d chunk(s), total matched rows=%d",
            scen_name,
            len(gdf_parts),
            len(gdf_final),
        )

        final_results.append((crit, gdf_final))

    return final_results


def buildMasterFromScenarioParquets(
    scenMapsDir: Path,
    scenarioNames: Sequence[str],
    baseDir: Path,
    cfg: configparser.ConfigParser,
    writeParquet: bool,
    writeGeoJson: bool,
    writeGpkg: bool,
    writeCsv: bool,
    csvWkt: bool,
    allowScenarioDuplicatesInMaster: bool,
) -> None:
    """
    Build master by reading per-scenario parquet files from disk.
    Used when mapperOnlyMaster=False (i.e., scenarios are written individually).
    """
    masterName = getMasterName(cfg, baseDir)

    parquetPaths: List[Path] = []
    for scenName in scenarioNames:
        scenNameClean = sanitizeScenarioName(scenName)
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
        master["__master_hash"] = build_master_row_hash_strings(
            master,
            ignore_cols=["scenario", "scenarioName"],
        )

        scen_order = list(dict.fromkeys(master["scenarioName"].astype(str).tolist()))
        out_rows = []

        for _, grp in master.groupby("__master_hash", sort=False):
            first = grp.iloc[[0]].copy()
            scen_present = set(grp["scenarioName"].astype(str).tolist())
            first.loc[first.index[0], "scenarioName"] = ", ".join([s for s in scen_order if s in scen_present])
            out_rows.append(first)

        master = gpd.GeoDataFrame(pd.concat(out_rows, ignore_index=True), crs=master.crs)
        master = master.drop(columns=["__master_hash"])

    mapperUtils.writeScenarioOutputs(
        master,
        outParquet=(scenMapsDir / f"{masterName}.parquet") if writeParquet else None,
        outGeoJson=(scenMapsDir / f"{masterName}.geojson") if writeGeoJson else None,
        outGpkg=(scenMapsDir / f"{masterName}.gpkg") if writeGpkg else None,
        outCsv=(scenMapsDir / f"{masterName}.csv") if writeCsv else None,
        csvWkt=csvWkt,
    )

    mapperUtils.logScenarioSummary(master, masterName)


# --------------------------- MAIN FUNCTION --------------------------- #
def runAvaScenMapper(
    cfg: configparser.ConfigParser,
    paths: Optional[dict] = None,
    areaCriteriaList: Optional[List[Dict]] = None,
) -> None:
    """Main entry point for the Avalanche Scenario Mapper (Step 17)."""
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

    writeParquet = cfg.getboolean("WORKFLOW", "writeScenarioParquet", fallback=True)
    writeGeoJson = cfg.getboolean("WORKFLOW", "writeScenarioGeoJson", fallback=False)
    writeGpkg = cfg.getboolean("WORKFLOW", "writeScenarioGpkg", fallback=False)
    writeCsv = cfg.getboolean("WORKFLOW", "writeScenarioCsv", fallback=False)
    csvWkt = cfg.getboolean("WORKFLOW", "writeScenarioCsvWkt", fallback=False)

    makeMaster = cfg.getboolean("WORKFLOW", "mapperMakeMaster", fallback=False)
    makeMasterOnly = cfg.getboolean("WORKFLOW", "mapperOnlyMaster", fallback=False)
    exportMasterGpkg = cfg.getboolean("WORKFLOW", "exportMasterGpkg", fallback=False)
    allowScenarioDuplicatesInMaster = cfg.getboolean("WORKFLOW", "allowScenarioDuplicatesInMaster", fallback=True)
    deleteColumns = parseDeleteColumns(cfg)

    log.info("Master duplicate handling: allowScenarioDuplicatesInMaster=%s", allowScenarioDuplicatesInMaster)
    log.info("Configured deleteColumns: %s", ", ".join(deleteColumns) if deleteColumns else "<none>")

    if makeMasterOnly and not makeMaster:
        log.warning("mapperOnlyMaster=True but mapperMakeMaster=False -> no output will be produced.")

    if makeMaster and makeMasterOnly and not writeParquet:
        log.warning("masterOnly=True requires parquet staging -> forcing writeScenarioParquet=True.")
        writeParquet = True

    if makeMaster and makeMasterOnly and writeGpkg:
        log.warning("masterOnly=True + writeScenarioGpkg=True is slow/crashy -> forcing writeScenarioGpkg=False.")
        writeGpkg = False
    if makeMaster and makeMasterOnly and writeGeoJson:
        log.warning("masterOnly=True + writeScenarioGeoJson=True is slow -> forcing writeScenarioGeoJson=False.")
        writeGeoJson = False
    if makeMaster and makeMasterOnly and writeCsv:
        log.warning("masterOnly=True + writeScenarioCsv=True is slow -> forcing writeScenarioCsv=False.")
        writeCsv = False

    if makeMaster and not makeMasterOnly and not writeParquet:
        log.warning("makeMaster=True requires per-scenario parquet -> forcing writeScenarioParquet=True.")
        writeParquet = True

    criteriaToRun: List[Dict] = []
    skipped = 0

    if makeMasterOnly:
        criteriaToRun = areaCriteriaList
    else:
        for crit in areaCriteriaList:
            scenName = crit.get("name", "unnamed")
            scenNameClean = sanitizeScenarioName(scenName)

            outputs = buildScenarioOutputPaths(
                scenMapsDir=scenMapsDir,
                scenNameClean=scenNameClean,
                writeParquet=writeParquet,
                writeGeoJson=writeGeoJson,
                writeGpkg=writeGpkg,
                writeCsv=writeCsv,
            )

            existing = next((p for p in outputs if p and p.exists()), None)
            if existing:
                skipped += 1
                log.info("Skipping scenario '%s' (already exists: %s)", scenName, relPath(existing, baseDir))
                continue

            criteriaToRun.append(crit)

    if not criteriaToRun:
        log.warning("Nothing to process -> exiting Mapper.")
        return

    if skipped:
        log.info("Skipped %d scenario(s) because outputs already exist.", skipped)

    file_size_gb = avaResultsPath.stat().st_size / (1024 ** 3)
    use_chunked = file_size_gb >= 1.0

    # ------------------ optimized streaming path ------------------ #
    if use_chunked and makeMaster and makeMasterOnly:
        masterName = getMasterName(cfg, baseDir)
        streamMasterOnlyChunked(
            avaResultsPath=avaResultsPath,
            cfg=cfg,
            criteriaToRun=criteriaToRun,
            avaLegend=avaLegend,
            scenMapsDir=scenMapsDir,
            baseDir=baseDir,
            masterName=masterName,
            allowScenarioDuplicatesInMaster=allowScenarioDuplicatesInMaster,
            deleteColumns=deleteColumns,
            exportMasterGpkg=exportMasterGpkg,
        )
        log.info("Step 17 finished in %.2fs", time.perf_counter() - t0)
        return

    # ------------------ legacy / non-streaming paths ------------------ #
    if use_chunked:
        log.info(
            "Input parquet is large (%.2f GB) -> using chunked row-group filtering.",
            file_size_gb,
        )
        results = runScenarioFiltersChunked(
            avaResultsPath=avaResultsPath,
            cfg=cfg,
            criteriaToRun=criteriaToRun,
            avaLegend=avaLegend,
            deleteColumns=deleteColumns,
        )
    else:
        log.info(
            "Input parquet is moderate (%.2f GB) -> using normal full-load filtering.",
            file_size_gb,
        )

        gdf = mapperUtils.readGdf(avaResultsPath)
        gdf = dropConfiguredColumns(gdf, deleteColumns)
        gdf = mapperUtils.normalizeAvaCols(gdf)

        if not mapperUtils.checkInputData(gdf, avaResultsPath, cfg):
            log.error("Step 17 aborted: input dataset incomplete or invalid.")
            return

        results = avaScenFilter.runScenarioFilters(gdf, criteriaToRun, avaLegend)

    if not results:
        log.warning("No scenario produced output.")
        return

    enriched_results: List[Tuple[Dict, gpd.GeoDataFrame]] = []
    for crit, gdfOut in results:
        scenName = sanitizeScenarioName(crit.get("name", "unnamed"))
        gdfOut = prepareScenarioOutput(gdfOut, scenName, deleteColumns)
        enriched_results.append((crit, gdfOut))

    if not makeMasterOnly:
        for crit, gdfOut in enriched_results:
            scenName = sanitizeScenarioName(crit.get("name", "unnamed"))

            mapperUtils.writeScenarioOutputs(
                gdfOut,
                outParquet=(scenMapsDir / f"avaScen_{scenName}.parquet") if writeParquet else None,
                outGeoJson=(scenMapsDir / f"avaScen_{scenName}.geojson") if writeGeoJson else None,
                outGpkg=(scenMapsDir / f"avaScen_{scenName}.gpkg") if writeGpkg else None,
                outCsv=(scenMapsDir / f"avaScen_{scenName}.csv") if writeCsv else None,
                csvWkt=csvWkt,
            )
    else:
        log.info("Per-scenario outputs skipped (mapperOnlyMaster = True).")

    if makeMaster:
        masterName = getMasterName(cfg, baseDir)

        if makeMasterOnly:
            masterDir = scenMapsDir / masterName
            masterDir.mkdir(parents=True, exist_ok=True)

            master_parts = [gdfOut for _, gdfOut in enriched_results if gdfOut is not None and not gdfOut.empty]
            if not master_parts:
                log.warning("Master-only: no scenario rows available.")
                return

            master = gpd.GeoDataFrame(pd.concat(master_parts, ignore_index=True), crs=master_parts[0].crs)

            if not allowScenarioDuplicatesInMaster:
                master["__master_hash"] = build_master_row_hash_strings(
                    master,
                    ignore_cols=["scenario", "scenarioName"],
                )

                scen_order = [sanitizeScenarioName(str(crit.get("name", "unnamed"))) for crit, _ in enriched_results]
                out_rows = []

                for _, grp in master.groupby("__master_hash", sort=False):
                    first = grp.iloc[[0]].copy()
                    scen_present = set(grp["scenarioName"].astype(str).tolist())
                    first.loc[first.index[0], "scenarioName"] = ", ".join([s for s in scen_order if s in scen_present])
                    out_rows.append(first)

                master = gpd.GeoDataFrame(pd.concat(out_rows, ignore_index=True), crs=master.crs)
                master = master.drop(columns=["__master_hash"])

            written = write_master_dataset_parts(masterDir, master, rows_per_part=250000)
            log.info(
                "Master-only: parquet dataset complete: %s (parts=%d, rows=%d)",
                relPath(masterDir, baseDir),
                len(written),
                len(master),
            )

            if exportMasterGpkg:
                outGpkg = masterDir / f"{masterName}.gpkg"
                log.info("Master-only: exporting GPKG (layers by scenario): %s", relPath(outGpkg, baseDir))
                export_master_dataset_to_gpkg_by_scenario(masterDir, outGpkg)

        else:
            scenarioNames = [str(crit.get("name", "unnamed")) for crit, _ in enriched_results]
            buildMasterFromScenarioParquets(
                scenMapsDir=scenMapsDir,
                scenarioNames=scenarioNames,
                baseDir=baseDir,
                cfg=cfg,
                writeParquet=writeParquet,
                writeGeoJson=writeGeoJson,
                writeGpkg=writeGpkg,
                writeCsv=writeCsv,
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

    ran_any = False

    for baseDir in baseDirs:
        if not baseDir.exists():
            log.error("baseDir does not exist: %s", baseDir)
            continue

        ran_any = True
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

    if not ran_any:
        log.error("No valid baseDir could be processed.")
        return 1

    log.info("Avalanche Scenario Mapper log saved at: %s\n", logPath)
    return 0


# --------------------------- MAIN RUNNER --------------------------- #
if __name__ == "__main__":
    raise SystemExit(main())