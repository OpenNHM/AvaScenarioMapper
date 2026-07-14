# --------------------------- com3AvaScenFilter/avaScenFilter.py --------------------------- #
#
# Purpose :
#   Apply multi-criteria scenario filtering to the AvaDirectory results dataset
#   (avaDirectoryResults.parquet) for visualization, mapping, and export.
#
#   Each scenario is defined by a set of filters :
#       - Region filters : select PRA results by administrative or forecast region
#                          (LKGebiet, LKGebietID, LWDGebietID)
#       - Scenario filters : select by subcatchment (subC), flow type (dry/wet),
#                            aspect/sector, and elevation range
#       - Legend filters : apply avalanche distribution and size potentials
#                          (AvaDistributionPotential × AvaSizePotential)
#                          using the matrix defined in in2Matrix/avaPotMatrix.py
#       - Deduplication : optionally keep only the largest rSize candidate
#                         per PRA/scenario context
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
#   2026-03 - 1.4
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
    Filter avaDirectoryResults using region, scenario, and
    avalanche potential-size rules.

    Expected criteria keys:
      - LKGebiet
      - LKGebietID
      - LWDGebietID
      - regionMode
      - subC
      - sector
      - flow
      - elevMin
      - elevMax
      - AvaDistributionPotential
      - AvaSizePotential
      - applySingleRsizeRule
    """

    # ------------------ helpers ------------------ #
    def _asList(val):
        if val is None:
            return []
        if isinstance(val, str):
            return [v.strip() for v in val.split(",") if v.strip()]
        return list(val)

    def _asIntList(val):
        out: List[int] = []
        for v in _asList(val):
            try:
                out.append(int(str(v).strip()))
            except Exception:
                pass
        return out

    def _asStrList(val):
        return [str(v).strip() for v in _asList(val) if str(v).strip()]

    def _peekUnique(df: gpd.GeoDataFrame, col: str, n: int = 12):
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

    def _logStage(stage: str, df: gpd.GeoDataFrame) -> None:
        log.info(
            "Stage=%-13s | n=%9d | subC=%s | flow=%s | sector=%s | elevMin=[%s..%s] elevMax=[%s..%s]",
            stage,
            0 if df is None else len(df),
            _peekUnique(df, "subC", n=8),
            _peekUnique(df, "flow", n=8),
            _peekUnique(df, "sector", n=12),
            (df["elevMin"].min() if df is not None and "elevMin" in df.columns and len(df) else None),
            (df["elevMin"].max() if df is not None and "elevMin" in df.columns and len(df) else None),
            (df["elevMax"].min() if df is not None and "elevMax" in df.columns and len(df) else None),
            (df["elevMax"].max() if df is not None and "elevMax" in df.columns and len(df) else None),
        )

    def _logTopJoinKeys(df: gpd.GeoDataFrame, topn: int = 12) -> None:
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
        asRows = []
        for idx, cnt in vc.items():
            ppm, pem, rsize, mod = idx
            asRows.append(f"(PPM={ppm}, PEM={pem}, rSize={rsize}, modType={mod}) -> {cnt}")
        log.info("Top join-key combos (PPM,PEM,rSize,modType): %s", " | ".join(asRows))

    def _maxSizeForPots(legendDf: pd.DataFrame, potList: List[str]) -> Dict[str, Optional[int]]:
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
    criteria = dict(criteria)

    criteria["LKGebiet"] = _asStrList(criteria.get("LKGebiet"))
    criteria["LKGebietID"] = _asIntList(criteria.get("LKGebietID"))
    criteria["LWDGebietID"] = _asStrList(criteria.get("LWDGebietID"))

    subC = _asIntList(criteria.get("subC"))
    sector = _asStrList(criteria.get("sector"))
    flow = [v.lower() for v in _asStrList(criteria.get("flow"))]

    criteria["regionMode"] = (criteria.get("regionMode") or "or").strip().lower()
    debugJoinKeys = bool(criteria.get("debugJoinKeys", False))

    log.info(
        "Scenario criteria: subC=%s | sector=%s | flow=%s | elevMin=%s elevMax=%s | pots=%s size=%s | region(LKname)=%d region(LKid)=%d region(LWD)=%d mode=%s",
        subC,
        sector,
        flow,
        criteria.get("elevMin"),
        criteria.get("elevMax"),
        criteria.get("AvaDistributionPotential"),
        criteria.get("AvaSizePotential"),
        len(criteria.get("LKGebiet") or []),
        len(criteria.get("LKGebietID") or []),
        len(criteria.get("LWDGebietID") or []),
        criteria.get("regionMode"),
    )

    # ------------------ Normalize input columns ------------------ #
    gdf = normalizeAvaCols(gdf)
    _logStage("start", gdf)

    # ------------------ Section 1: Region filters ------------------ #
    LKGebiet = criteria.get("LKGebiet")
    LKGebietID = criteria.get("LKGebietID")
    LWDGebietID = criteria.get("LWDGebietID")
    regionMode = criteria.get("regionMode", "or")

    maskLkName = None
    maskLkId = None
    maskLwd = None

    if LKGebiet:
        if "LKGebiet" in gdf.columns:
            lkNameCol = gdf["LKGebiet"].astype(str).str.strip()
            maskLkName = lkNameCol.isin(set(LKGebiet))
        else:
            log.warning("Region filter requested (LKGebiet) but column 'LKGebiet' not found.")

    if LKGebietID:
        if "LKGebietID" in gdf.columns:
            lkIdCol = pd.to_numeric(gdf["LKGebietID"], errors="coerce").astype("Int64")
            maskLkId = lkIdCol.isin(set(LKGebietID))
        else:
            log.warning("Region filter requested (LKGebietID) but column 'LKGebietID' not found.")

    if LWDGebietID:
        if "LWDGebietID" in gdf.columns:
            lwdCol = gdf["LWDGebietID"].astype(str).str.strip()
            maskLwd = lwdCol.isin(set(LWDGebietID))
        else:
            log.warning("Region filter requested (LWDGebietID) but column 'LWDGebietID' not found.")

    regionMasks = [m for m in [maskLkName, maskLkId, maskLwd] if m is not None]

    if regionMasks:
        maskRegion = regionMasks[0]
        for m in regionMasks[1:]:
            maskRegion = (maskRegion & m) if regionMode == "and" else (maskRegion | m)

        before = len(gdf)
        gdf = gdf[maskRegion].copy()

        log.info(
            "Region filter kept %d/%d rows (LKGebiet=%d, LKGebietID=%d, LWDGebietID=%d, mode=%s)",
            len(gdf),
            before,
            len(LKGebiet or []),
            len(LKGebietID or []),
            len(LWDGebietID or []),
            regionMode,
        )

    _logStage("after_region", gdf)
    if gdf.empty:
        log.warning("No rows left after region filter.")
        return gdf

    # ------------------ Section 2: Scenario filters ------------------ #
    before = len(gdf)

    elevMin = criteria.get("elevMin")
    elevMax = criteria.get("elevMax")

    if subC:
        if "subC" not in gdf.columns:
            log.warning("Filter subC requested, but column 'subC' not found.")
        else:
            before2 = len(gdf)
            gdf = gdf[gdf["subC"].isin(set(subC))].copy()
            log.info("Filter subC=%s kept %d/%d", subC, len(gdf), before2)

    if sector:
        if "sector" not in gdf.columns:
            log.warning("Filter sector requested, but column 'sector' not found.")
        else:
            secSet = {str(s).strip().upper() for s in sector}
            tmpSector = gdf["sector"].astype(str).str.upper().str.strip()
            before2 = len(gdf)
            gdf = gdf[tmpSector.isin(secSet)].copy()
            log.info("Filter sector=%s kept %d/%d", sorted(secSet), len(gdf), before2)

    if flow:
        if "flow" not in gdf.columns:
            log.warning("Filter flow requested, but column 'flow' not found.")
        else:
            flowSet = {str(f).strip().lower() for f in flow}
            tmpFlow = gdf["flow"].astype(str).str.lower().str.strip()
            before2 = len(gdf)
            gdf = gdf[tmpFlow.isin(flowSet)].copy()
            log.info("Filter flow=%s kept %d/%d", sorted(flowSet), len(gdf), before2)

    if elevMin is not None:
        if "elevMin" not in gdf.columns:
            log.warning("Filter elevMin requested, but column 'elevMin' not found.")
        else:
            tmpElevMin = pd.to_numeric(gdf["elevMin"], errors="coerce")
            before2 = len(gdf)
            gdf = gdf[tmpElevMin >= float(elevMin)].copy()
            log.info("Filter elevMin>=%s kept %d/%d", elevMin, len(gdf), before2)

    if elevMax is not None:
        if "elevMax" not in gdf.columns:
            log.warning("Filter elevMax requested, but column 'elevMax' not found.")
        else:
            tmpElevMax = pd.to_numeric(gdf["elevMax"], errors="coerce")
            before2 = len(gdf)
            gdf = gdf[tmpElevMax <= float(elevMax)].copy()
            log.info("Filter elevMax<=%s kept %d/%d", elevMax, len(gdf), before2)

    log.info("Scenario filters total kept %d/%d", len(gdf), before)
    _logStage("after_scenario", gdf)

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

        repl = {"moderat": "moderate"}
        pots = [repl.get(p, p) for p in pots]

        sizeRef = int(avaSizePot)

        renameMap = {}
        if "ppm" in gdf.columns and "PPM" not in gdf.columns:
            renameMap["ppm"] = "PPM"
        if "pem" in gdf.columns and "PEM" not in gdf.columns:
            renameMap["pem"] = "PEM"
        if renameMap:
            log.info("Renaming join columns in data: %s", renameMap)
            gdf = gdf.rename(columns=renameMap)

        for c in ("PPM", "PEM", "rSize"):
            if c in gdf.columns:
                gdf[c] = pd.to_numeric(gdf[c], errors="coerce").astype("Int64")
        if "modType" in gdf.columns:
            gdf["modType"] = gdf["modType"].astype(str).str.lower().str.strip()

        legAll = legend.copy()
        legAll["AvaDistributionPotential"] = legAll["AvaDistributionPotential"].astype(str).str.lower().str.strip()
        legAll["modType"] = legAll["modType"].astype(str).str.lower().str.strip()
        for c in ("PPM", "PEM", "rSize", "AvaSizePotential"):
            if c in legAll.columns:
                legAll[c] = pd.to_numeric(legAll[c], errors="coerce").astype("Int64")

        legSel = legAll[
            (legAll["AvaDistributionPotential"].isin(pots))
            & (legAll["AvaSizePotential"] == sizeRef)
        ]
        log.info("Legend selection rows=%d for pots=%s size=%s", len(legSel), pots, sizeRef)

        if legSel.empty:
            maxByPot = _maxSizeForPots(legAll, pots)
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

        gdf = gdf.copy()
        gdf["AvaDistributionPotential"] = ",".join(sorted(set(pots)))
        gdf["AvaSizePotential"] = sizeRef

        if sizeRef == 1:
            if "modType" not in gdf.columns:
                log.error("Size=1 rel-only mode requested, but dataset has no 'modType' column.")
                return gdf.iloc[0:0].copy()

            beforeRel = len(gdf)
            gdf = gdf[gdf["modType"].eq("rel")].copy()
            log.info("Legend size=1 rel-only mode: kept %d/%d rows by modType=rel", len(gdf), beforeRel)

            if gdf.empty:
                log.warning("Legend size=1 rel-only mode: no rel rows available after scenario filters.")
                return gdf

            _logStage("after_legend", gdf)

        else:
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
                _logTopJoinKeys(gdf, topn=12)

            if not all(c in gdf.columns for c in ["PPM", "PEM", "rSize", "modType"]):
                log.warning("Legend merge requested but dataset missing one of PPM/PEM/rSize/modType.")
                return gdf.iloc[0:0].copy()

            beforeMerge = len(gdf)
            gdf = gdf.merge(allowed, on=["PPM", "PEM", "rSize", "modType"], how="inner")
            log.info("Legend merge kept %d/%d rows", len(gdf), beforeMerge)
            _logStage("after_legend", gdf)

    # ------------------ Section 4: Deduplication ------------------ #
    applySingle = criteria.get("applySingleRsizeRule", True)

    if applySingle and "rSize" in gdf.columns and not gdf.empty:
        before = len(gdf)

        groupCols = [
            "praID",
            "praAreaM",
            "praElevMin",
            "praElevMax",
            "praElevMean",
            "praElevBand",
            "praElevBandRule",
            "praAreaSized",
            "LKGebietID",
            "LKGebiet",
            "LKRegion",
            "LWDGebietID",
            "modType",
            "subC",
            "sector",
            "elevMin",
            "elevMax",
            "flow",
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

    Important:
      returns (criteria, gdf) pairs so that skipped scenarios
      do not shift filenames during export.
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