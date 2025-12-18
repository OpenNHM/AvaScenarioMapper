# --------------------------- com3AvaScenFilter/avaScenFilter.py --------------------------- #
#
# Purpose :
#   Apply multi-criteria filtering to the AvaDirectory results dataset
#   (avaDirectoryResults.parquet) to extract scenario-specific subsets
#   for visualization and mapping.
#
#   Each scenario is defined by a set of filters :
#       - Region filters : select PRA results by administrative or forecast region
#                          (LKGebietID or LWDGebietID)
#       - Scenario filters : select by subcatchment (subC), flow type (dry/wet),
#                            aspect/sector, and elevation range
#       - Legend filters : apply avalanche distribution and size potentials
#                          (AvaDistributionPotential × AvaSizePotential)
#                          using the matrix defined in in2Matrix/avaPotMatrix.py
#       - Deduplication : enforce a single PRA per scenario, keeping the
#                         result with the largest relative size (rSize)
#
# Output :
#   A filtered GeoDataFrame ready for export as
#   avaScen_<ScenarioName>.parquet / .geojson by runAvaScenMapper.py.
#
# Author :
#   Christoph Hesselbach
#
# Institution :
#   Austrian Research Centre for Forests (BFW)
#   Department of Natural Hazards | Snow and Avalanche Unit
#
# Date & Version :
#   2025-12 - 1.1
#
# ------------------------------------------------------------------------------------------- #

import logging
from typing import Dict, List, Optional, Tuple

import geopandas as gpd
import pandas as pd

from in1Utils.mapperUtils import logScenarioSummary, normalizeAvaCols
from in2Matrix.avaPotMatrix import avaPotMatrix

log = logging.getLogger(__name__)


def filterScenarioResults(
    gdf: gpd.GeoDataFrame,
    criteria: Dict,
    legend: Optional[pd.DataFrame] = None,
) -> gpd.GeoDataFrame:
    """
    Filter avaDirectoryResults using region, flow, elevation, and
    avalanche potential–size rules.
    """

    # ------------------ helpers ------------------ #
    def _asList(val):
        if val is None:
            return []
        if isinstance(val, str):
            return [v.strip() for v in val.split(",") if v.strip()]
        return list(val)

    def _peek_unique(df: gpd.GeoDataFrame, col: str, n: int = 12):
        if df is None or df.empty or col not in df.columns:
            return None
        vals = df[col].dropna().unique().tolist()
        try:
            vals = sorted(vals)
        except Exception:
            pass
        if len(vals) > n:
            return vals[:n] + ["..."]
        return vals

    def _log_stage(stage: str, df: gpd.GeoDataFrame) -> None:
        log.info(
            "Stage=%-13s | n=%9d | subC=%s | flow=%s | sector=%s | elevMin=[%s..%s] elevMax=[%s..%s]",
            stage,
            0 if df is None else len(df),
            _peek_unique(df, "subC", n=8),
            _peek_unique(df, "flow", n=8),
            _peek_unique(df, "sector", n=12),
            (df["elevMin"].min() if df is not None and "elevMin" in df.columns and len(df) else None),
            (df["elevMin"].max() if df is not None and "elevMin" in df.columns and len(df) else None),
            (df["elevMax"].min() if df is not None and "elevMax" in df.columns and len(df) else None),
            (df["elevMax"].max() if df is not None and "elevMax" in df.columns and len(df) else None),
        )

    def _log_top_join_keys(df: gpd.GeoDataFrame, topn: int = 12) -> None:
        """
        Print most frequent join-key combos to debug legend merge mismatches.
        NOTE: This can be expensive on very large datasets.
        """
        need = ["PPM", "PEM", "rSize", "modType"]
        if df is None or df.empty:
            log.info("Join-key sample: dataset empty.")
            return
        if not all(c in df.columns for c in need):
            missing = [c for c in need if c not in df.columns]
            log.info("Join-key sample: missing columns %s", missing)
            return

        tmp = df[need].copy()
        tmp["modType"] = tmp["modType"].astype(str).str.lower().str.strip()
        for c in ("PPM", "PEM", "rSize"):
            tmp[c] = pd.to_numeric(tmp[c], errors="coerce").astype("Int64")

        vc = tmp.value_counts(dropna=False).head(topn)
        as_rows = []
        for idx, cnt in vc.items():
            ppm, pem, rsize, mod = idx
            as_rows.append(f"(PPM={ppm}, PEM={pem}, rSize={rsize}, modType={mod}) -> {cnt}")
        log.info("Top join-key combos (PPM,PEM,rSize,modType): %s", " | ".join(as_rows))

    def _max_size_for_pots(legendDf: pd.DataFrame, potList: List[str]) -> Dict[str, Optional[int]]:
        """Return max AvaSizePotential available per pot in the matrix."""
        out: Dict[str, Optional[int]] = {}
        if legendDf is None or legendDf.empty:
            for p in potList:
                out[p] = None
            return out

        tmp = legendDf.copy()
        tmp["AvaDistributionPotential"] = tmp["AvaDistributionPotential"].astype(str).str.lower().str.strip()
        tmp["AvaSizePotential"] = pd.to_numeric(tmp["AvaSizePotential"], errors="coerce")

        for p in potList:
            sel = tmp[tmp["AvaDistributionPotential"].eq(p)]
            if sel.empty:
                out[p] = None
            else:
                mx = sel["AvaSizePotential"].max()
                out[p] = int(mx) if pd.notna(mx) else None
        return out

    # ------------------ Normalize criteria inputs ------------------ #
    criteria = dict(criteria)  # defensive copy
    criteria["LKRegionID"] = _asList(criteria.get("LKRegionID"))
    criteria["LwdRegionID"] = _asList(criteria.get("LwdRegionID"))
    criteria["regionMode"] = (criteria.get("regionMode") or "or").strip().lower()
    debugJoinKeys = bool(criteria.get("debugJoinKeys", False))

    log.info(
        "Scenario criteria: subCs=%s | sectors=%s | flows=%s | elevMin=%s elevMax=%s | pots=%s size=%s | region(LK)=%d region(LWD)=%d mode=%s",
        criteria.get("subCs"),
        criteria.get("sectors"),
        criteria.get("flows"),
        criteria.get("elevMin"),
        criteria.get("elevMax"),
        criteria.get("AvaDistributionPotential"),
        criteria.get("AvaSizePotential"),
        len(criteria.get("LKRegionID") or []),
        len(criteria.get("LwdRegionID") or []),
        criteria.get("regionMode"),
    )

    # ------------------ Normalize input columns (guard) ------------------ #
    if not (
        ("flow" in gdf.columns)
        and (("PEM" in gdf.columns) or ("pem" in gdf.columns))
        and (("PPM" in gdf.columns) or ("ppm" in gdf.columns))
    ):
        gdf = normalizeAvaCols(gdf)

    _log_stage("start", gdf)

    # ------------------ Section 1: Region filters ------------------ #
    LKRegionID = criteria.get("LKRegionID")
    LwdRegionID = criteria.get("LwdRegionID")
    regionMode = criteria.get("regionMode", "or")

    maskLk = None
    maskLwd = None

    if LKRegionID:
        if "LKGebietID" in gdf.columns:
            maskLk = gdf["LKGebietID"].isin(set(LKRegionID))
        else:
            log.warning("Region filter requested (LK) but column 'LKGebietID' not found.")

    if LwdRegionID:
        if "LWDGebietID" in gdf.columns:
            maskLwd = gdf["LWDGebietID"].isin(set(LwdRegionID))
        else:
            log.warning("Region filter requested (LWD) but column 'LWDGebietID' not found.")

    if maskLk is not None or maskLwd is not None:
        if maskLk is not None and maskLwd is not None:
            maskRegion = (maskLk & maskLwd) if regionMode == "and" else (maskLk | maskLwd)
            log.info("Region filter: LK=%d ids, LWD=%d ids, mode=%s", len(LKRegionID), len(LwdRegionID), regionMode)
        else:
            maskRegion = maskLk if maskLk is not None else maskLwd
            log.info(
                "Region filter: %s only (%d ids)",
                "LK" if maskLk is not None else "LWD",
                len(LKRegionID) if maskLk is not None else len(LwdRegionID),
            )

        before = len(gdf)
        gdf = gdf[maskRegion].copy()
        log.info("Region filter kept %d/%d rows", len(gdf), before)

    _log_stage("after_region", gdf)
    if gdf.empty:
        log.warning("No rows left after region filter.")
        return gdf

    # ------------------ Section 2: Scenario filters ------------------ #
    before = len(gdf)

    subCs = criteria.get("subCs")
    sectors = criteria.get("sectors")
    flows = criteria.get("flows")
    elevMin = criteria.get("elevMin")
    elevMax = criteria.get("elevMax")

    if subCs:
        if "subC" not in gdf.columns:
            log.warning("Filter subC requested, but column 'subC' not found.")
        else:
            before2 = len(gdf)
            gdf = gdf[gdf["subC"].isin(set(subCs))]
            log.info("Filter subC=%s kept %d/%d", subCs, len(gdf), before2)

    if sectors:
        if "sector" not in gdf.columns:
            log.warning("Filter sector requested, but column 'sector' not found.")
        else:
            secSet = {str(s).strip().upper() for s in sectors}
            gdf["sector"] = gdf["sector"].astype(str).str.upper().str.strip()
            before2 = len(gdf)
            gdf = gdf[gdf["sector"].isin(secSet)]
            log.info("Filter sector=%s kept %d/%d", sorted(secSet), len(gdf), before2)

    if flows:
        if "flow" not in gdf.columns:
            log.warning("Filter flow requested, but column 'flow' not found.")
        else:
            flowSet = {str(f).strip().lower() for f in flows}
            gdf["flow"] = gdf["flow"].astype(str).str.lower().str.strip()
            before2 = len(gdf)
            gdf = gdf[gdf["flow"].isin(flowSet)]
            log.info("Filter flow=%s kept %d/%d", sorted(flowSet), len(gdf), before2)

    if elevMin is not None:
        if "elevMin" not in gdf.columns:
            log.warning("Filter elevMin requested, but column 'elevMin' not found.")
        else:
            gdf = gdf.copy()
            gdf["elevMin"] = pd.to_numeric(gdf["elevMin"], errors="coerce")
            before2 = len(gdf)
            gdf = gdf[gdf["elevMin"] >= float(elevMin)]
            log.info("Filter elevMin>=%s kept %d/%d", elevMin, len(gdf), before2)

    if elevMax is not None:
        if "elevMax" not in gdf.columns:
            log.warning("Filter elevMax requested, but column 'elevMax' not found.")
        else:
            gdf = gdf.copy()
            gdf["elevMax"] = pd.to_numeric(gdf["elevMax"], errors="coerce")
            before2 = len(gdf)
            gdf = gdf[gdf["elevMax"] <= float(elevMax)]
            log.info("Filter elevMax<=%s kept %d/%d", elevMax, len(gdf), before2)

    log.info("Scenario filters total kept %d/%d", len(gdf), before)
    _log_stage("after_scenario", gdf)

    if gdf.empty:
        log.warning("No rows left after scenario filters (subC/sector/flow/elev).")
        return gdf

    # ------------------ Section 3: Legend filters ------------------ #
    avaDistPot = criteria.get("AvaDistributionPotential")
    avaSizePot = criteria.get("AvaSizePotential")

    if avaDistPot and avaSizePot is not None:
        legend = legend if legend is not None else avaPotMatrix()

        pots = [avaDistPot] if isinstance(avaDistPot, str) else list(avaDistPot)
        pots = [str(p).lower().strip() for p in pots if str(p).strip()]

        # normalize common spelling variants (prevents "moderat" vs "moderate" mismatch)
        repl = {"moderat": "moderate"}
        pots = [repl.get(p, p) for p in pots]

        sizeRef = int(avaSizePot)

        # --- Normalize join-key column names in gdf (ppm/pem -> PPM/PEM) ---
        rename_map = {}
        if "ppm" in gdf.columns and "PPM" not in gdf.columns:
            rename_map["ppm"] = "PPM"
        if "pem" in gdf.columns and "PEM" not in gdf.columns:
            rename_map["pem"] = "PEM"
        if rename_map:
            log.info("Renaming join columns in data: %s", rename_map)
            gdf = gdf.rename(columns=rename_map)

        # --- Ensure join keys are comparable ---
        for c in ("PPM", "PEM", "rSize"):
            if c in gdf.columns:
                gdf[c] = pd.to_numeric(gdf[c], errors="coerce").astype("Int64")
        if "modType" in gdf.columns:
            gdf["modType"] = gdf["modType"].astype(str).str.lower().str.strip()

        # --- Normalize legend once (for selection + max-size checks) ---
        legAll = legend.copy()
        legAll["AvaDistributionPotential"] = legAll["AvaDistributionPotential"].astype(str).str.lower().str.strip()
        legAll["modType"] = legAll["modType"].astype(str).str.lower().str.strip()
        for c in ("PPM", "PEM", "rSize", "AvaSizePotential"):
            if c in legAll.columns:
                legAll[c] = pd.to_numeric(legAll[c], errors="coerce").astype("Int64")

        # --- Validate (pot,size) exists in matrix: if not -> warn + skip scenario ---
        legSel = legAll[(legAll["AvaDistributionPotential"].isin(pots)) & (legAll["AvaSizePotential"] == sizeRef)]
        log.info("Legend selection rows=%d for pots=%s size=%s", len(legSel), pots, sizeRef)

        if legSel.empty:
            maxByPot = _max_size_for_pots(legAll, pots)
            msgBits = []
            for p in pots:
                mx = maxByPot.get(p)
                msgBits.append(f"{p}(max={mx})" if mx is not None else f"{p}(not-in-matrix)")

            log.warning(
                "Legend selection empty: (%s, AvaSizePotential=%s) is not defined in the matrix. "
                "Highest AvaSizePotential for these potentials: %s. Skipping scenario.",
                ",".join(pots),
                sizeRef,
                ", ".join(msgBits),
            )
            return gdf.iloc[0:0].copy()

        # Tag scenario metadata EARLY (prevents confusing outputs if later becomes empty)
        gdf = gdf.copy()
        gdf["AvaDistributionPotential"] = ",".join(sorted(set(pots)))
        gdf["AvaSizePotential"] = sizeRef

        # --- SPECIAL CASE: size=1 -> rel-only ---
        if sizeRef == 1:
            if "modType" not in gdf.columns:
                log.error("Size=1 rel-only mode requested, but dataset has no 'modType' column.")
                return gdf.iloc[0:0].copy()

            beforeRel = len(gdf)
            relOnly = gdf[gdf["modType"].eq("rel")].copy()
            log.info("Legend size=1 rel-only mode: kept %d/%d rows by modType=rel", len(relOnly), beforeRel)

            if relOnly.empty:
                log.warning("Legend size=1 rel-only mode: no rel rows available after scenario filters.")
                return relOnly

            # Deterministic: keep one row per PRA, preferring smallest PEM (proxy for 'shortest runout')
            if "praID" in relOnly.columns:
                sortCols = []
                if "PEM" in relOnly.columns:
                    sortCols.append("PEM")
                if "rSize" in relOnly.columns:
                    sortCols.append("rSize")

                if sortCols:
                    ascending = [True] * len(sortCols)
                    if "rSize" in sortCols:
                        ascending[sortCols.index("rSize")] = False
                    relOnly = relOnly.sort_values(sortCols, ascending=ascending)

                relOnly = relOnly.drop_duplicates(subset=["praID"], keep="first").copy()
                log.info("Legend size=1 rel-only mode: reduced to %d unique PRA(s)", relOnly["praID"].nunique())

            _log_stage("after_legend", relOnly)
            gdf = relOnly

        else:
            # --- Regular matrix-driven legend join (STRICT ONLY; no rel-tail) ---
            allowed = legSel[["PPM", "PEM", "rSize", "modType"]].copy()
            allowed["modType"] = allowed["modType"].astype(str).str.lower().str.strip()

            both = allowed[allowed["modType"].eq("res / rel")].copy()
            allowed = pd.concat(
                [
                    allowed[~allowed["modType"].eq("res / rel")],
                    both.assign(modType="res"),
                    both.assign(modType="rel"),
                ],
                ignore_index=True,
            ).drop_duplicates()

            if debugJoinKeys:
                _log_top_join_keys(gdf, topn=12)

            if not all(c in gdf.columns for c in ["PPM", "PEM", "rSize", "modType"]):
                log.warning("Legend merge requested but dataset missing one of PPM/PEM/rSize/modType.")
                return gdf.iloc[0:0].copy()

            before_merge = len(gdf)
            gdf = gdf.merge(allowed, on=["PPM", "PEM", "rSize", "modType"], how="inner")
            log.info("Legend merge kept %d/%d rows", len(gdf), before_merge)
            _log_stage("after_legend", gdf)

    # ------------------ Section 4: Deduplication ------------------ #
    applySingle = criteria.get("applySingleRsizeRule", True)
    if applySingle and "rSize" in gdf.columns and not gdf.empty:
        before = len(gdf)
        groupCols = [
            "praID", "praAreaM", "praElevMin", "praElevMax", "praElevMean",
            "praElevBand", "praElevBandRule", "praAreaSized",
            "LKGebietID", "LKGebiet", "LKRegion", "LWDGebietID",
            "modType", "subC", "sector", "elevMin", "elevMax", "flow",
        ]
        groupCols = [c for c in groupCols if c in gdf.columns]

        gdf = gdf.sort_values(by="rSize", ascending=False)
        gdf = gdf.drop_duplicates(subset=groupCols, keep="first")
        log.info("Applied single-rSize rule: dropped %d duplicates", before - len(gdf))

    logScenarioSummary(gdf, criteria.get("name", "unnamed"))
    return gdf


def runScenarioFilters(
    gdf: gpd.GeoDataFrame,
    criteriaList: List[Dict],
    legend: pd.DataFrame,
) -> List[Tuple[Dict, gpd.GeoDataFrame]]:
    """
    Execute multiple scenario filters sequentially.

    Important: returns (criteria, gdf) pairs so that skipped scenarios
    (empty outputs) do not shift filenames during export.
    """
    results: List[Tuple[Dict, gpd.GeoDataFrame]] = []
    for crit in criteriaList:
        scenName = crit.get("name", "unnamed")
        log.info("Starting scenario: %s", scenName)
        try:
            survivors = filterScenarioResults(gdf, crit, legend)
            if survivors.empty:
                log.warning("Scenario %s produced no results", scenName)
                continue
            results.append((crit, survivors))
        except Exception:
            log.exception("Scenario %s failed during filtering", scenName)
    return results
