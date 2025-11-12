# ------------------ com3AvaScenFilter/avaScenFilter.py ------------------ #
# Step 17 Core Logic: Avalanche Scenario Filtering
#
# Purpose
# --------
# Apply multi-criteria filtering to the AvaDirectory results dataset
# (avaDirectoryResults.parquet) to extract scenario-specific subsets
# for visualization and mapping.
#
# The filtering criteria correspond to physical, geographical, and
# avalanche-dynamics attributes defined in avaScenMapperCfg.ini.
#
# Each scenario is defined by a set of filters:
#   - Region filters: select PRA results by administrative or forecast region
#                     (LKGebietID or LWDGebietID)
#   - Scenario filters: select by subcatchment (subC), flow type (dry/wet),
#                       aspect/sector, and elevation range.
#   - Legend filters: apply avalanche potential (AvaPotential) and
#                     avalanche size (PEM_header) combinations using
#                     the matrix defined in in2Matrix/avaPotMatrix.py.
#   - Deduplication: enforce a single PRA per scenario, keeping the
#                    result with the largest relative size (rSize).
#
# The output is a filtered GeoDataFrame ready for export as
# avaScen_<ScenarioName>.parquet / .geojson by runAvaScenMapper.py.
#
# Author:  CAIROS Project Team
# Version: 2025-11
# -------------------------------------------------------------------------

import logging
import pandas as pd
import geopandas as gpd
from typing import Dict, List
from in2Matrix.avaPotMatrix import makeAvaLegend
from in1Utils.mapperUtils import normalizeAvaCols, logScenarioSummary

log = logging.getLogger(__name__)


# -------------------------------------------------------------------------
# Function: filterScenarioResults
# -------------------------------------------------------------------------
def filterScenarioResults(
    gdf: gpd.GeoDataFrame,
    criteria: Dict,
    legend: pd.DataFrame = None
) -> gpd.GeoDataFrame:
    """
    Filter avaDirectoryResults using region, flow, elevation and avalanche
    potential/size rules. Returns filtered GeoDataFrame.

    Parameters
    ----------
    gdf : GeoDataFrame
        Input dataset (avaDirectoryResults.parquet content).
    criteria : dict
        Scenario definition (parsed from avaScenMapperCfg.ini).
        Keys include:
          LKRegionID, LwdRegionID, regionMode,
          subCs, sectors, flows, elevMin, elevMax,
          avaPotential, avaSize, applySingleRsizeRule
    legend : DataFrame, optional
        Avalanche potential–size–modType matrix from makeAvaLegend().
    """
    gdf = normalizeAvaCols(gdf)

    # --- 1. Region filters (LKGebietID / LWDGebietID) ---
    LKRegionID = criteria.get("LKRegionID")
    LwdRegionID = criteria.get("LwdRegionID")
    regionMode = criteria.get("regionMode", "or").lower()

    maskAll = pd.Series(True, index=gdf.index)
    maskLk = gdf["LKGebietID"].isin(set(LKRegionID)) if LKRegionID else None
    maskLwd = gdf["LWDGebietID"].isin(set(LwdRegionID)) if LwdRegionID else None

    if maskLk is not None and maskLwd is not None:
        maskRegion = maskLk & maskLwd if regionMode == "and" else maskLk | maskLwd
    elif maskLk is not None:
        maskRegion = maskLk
    elif maskLwd is not None:
        maskRegion = maskLwd
    else:
        maskRegion = maskAll

    gdf = gdf[maskRegion].copy()

    # --- 2. Scenario filters (subC, sector, flow, elevation) ---
    subCs = criteria.get("subCs")
    sectors = criteria.get("sectors")
    flows = criteria.get("flows")
    elevMin = criteria.get("elevMin")
    elevMax = criteria.get("elevMax")

    if subCs:
        gdf = gdf[gdf["subC"].isin(set(subCs))]
    if sectors:
        gdf = gdf[gdf["sector"].isin(set(sectors))]
    if flows:
        gdf = gdf[gdf["flow"].isin([f.lower() for f in flows])]
    if elevMin is not None:
        gdf = gdf[gdf["elevMin"] >= elevMin]
    if elevMax is not None:
        gdf = gdf[gdf["elevMax"] <= elevMax]

    # --- 3. Avalanche Legend filters (AvaPotential + AvaSize) ---
    avaPotential = criteria.get("avaPotential")
    avaSize = criteria.get("avaSize")
    if avaPotential and avaSize is not None:
        legend = legend if legend is not None else makeAvaLegend()

        pots = [avaPotential] if isinstance(avaPotential, str) else list(avaPotential)
        pots = [p.lower() for p in pots]
        pemHeader = int(avaSize)

        leg = legend.copy()
        leg["AvaPotential"] = leg["AvaPotential"].str.lower()
        leg["modType"] = leg["modType"].str.lower().str.strip()
        leg = leg[(leg["AvaPotential"].isin(pots)) & (leg["PEM_header"] == pemHeader)]

        if leg.empty:
            log.warning("Legend selection empty for potentials=%s, PEM=%s", pots, pemHeader)
        else:
            for c in ("PPM", "PEM", "rSize"):
                if c in gdf.columns:
                    gdf[c] = pd.to_numeric(gdf[c], errors="coerce").astype("Int64")

            allowedTriples = pd.concat([
                leg.loc[leg["modType"].eq("res / rel"), ["PPM", "PEM", "rSize"]],
                leg.loc[leg["modType"].eq("res"), ["PPM", "PEM", "rSize"]],
                leg.loc[leg["modType"].eq("rel"), ["PPM", "PEM", "rSize"]],
            ]).drop_duplicates()

            before = len(gdf)
            gdf = gdf.merge(allowedTriples, on=["PPM", "PEM", "rSize"], how="inner")
            log.info("Applied legend filter: potentials=%s, PEM=%s → kept %d/%d rows",
                     pots, pemHeader, len(gdf), before)

            gdf["avaPotential"] = ",".join(sorted(set(pots)))

    # --- 4. Deduplication: keep largest rSize per PRA ---
    applySingle = criteria.get("applySingleRsizeRule", True)
    if applySingle and "rSize" in gdf.columns:
        before = len(gdf)
        groupCols = [
            "praID", "praAreaM", "praElevMin", "praElevMax", "praElevMean",
            "praElevBand", "praElevBandRule", "praAreaSized",
            "LKGebietID", "LKGebiet", "LKRegion", "LWDGebietID",
            "modType", "subC", "sector", "elevMin", "elevMax", "flow"
        ]
        groupCols = [c for c in groupCols if c in gdf.columns]
        gdf = gdf.sort_values(by="rSize", ascending=False)
        gdf = gdf.drop_duplicates(subset=groupCols, keep="first")
        log.info("Applied single-rSize rule: dropped %d duplicates", before - len(gdf))

    logScenarioSummary(gdf, criteria.get("name", "unnamed"))
    return gdf


# -------------------------------------------------------------------------
# Function: runScenarioFilters
# -------------------------------------------------------------------------
def runScenarioFilters(
    gdf: gpd.GeoDataFrame,
    criteriaList: List[Dict],
    legend: pd.DataFrame
) -> List[gpd.GeoDataFrame]:
    """
    Execute multiple scenario filters sequentially.

    Parameters
    ----------
    gdf : GeoDataFrame
        Source data (avaDirectoryResults.parquet)
    criteriaList : list of dict
        Scenario definitions parsed from configuration
    legend : DataFrame
        Avalanche potential–size matrix

    Returns
    -------
    list of GeoDataFrame
        One filtered dataset per scenario.
    """
    results = []
    for crit in criteriaList:
        scenName = crit.get("name", "unnamed")
        log.info("Starting scenario: %s", scenName)
        try:
            survivors = filterScenarioResults(gdf, crit, legend)
            if survivors.empty:
                log.warning("Scenario %s produced no results", scenName)
                continue
            results.append(survivors)
        except Exception:
            log.exception("Scenario %s failed during filtering", scenName)
    return results
