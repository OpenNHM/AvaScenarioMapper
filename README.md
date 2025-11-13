
# 🏔️ Avalanche Scenario Mapper (2025-11 Update)

<p align="center">
  <img src="https://media.giphy.com/media/3Xzlefv57zcrVIPPRN/giphy.gif"
       alt="Avalanche Scenario Mapper"
       width="300"/>
</p>

<h3 align="center">⚠️ Handle with care — work in progress</h3>

---

## Overview

- The Avalanche Scenario Mapper was developed within the **EUREGIO Project CAIROS**.  
- It represents **Step 16** of the **Avalanche Scenario Model Chain**.  
- The Mapper post-processes avalanche simulation results from Step 15 (AvaDirectoryResults)  
  and generates **scenario-specific subsets** for visualization, mapping, and publication.  
- It consumes `AvaDirectoryResults.parquet` and produces per-scenario GeoDataFrames / GeoJSONs.  
- Logging, configuration, and folder hierarchy follow the Avalanche Scenario Model Chain conventions.

| Component | Description |
|------------|-------------|
| Main entry point | `runAvaScenMapper.py` |
| Configuration | `avaScenMapperCfg.ini` (+ `local_avaScenMapperCfg.ini`) |
| Output folder | `13_avaScenMaps/` |

---

## Repository layout

```text
../AvalancheScenarioMapper/
├── avaScenMapperCfg.ini            ← Main configuration (global + scenarios)
├── local_avaScenMapperCfg.ini      ← Local override for testing
├── runAvaScenMapper.py             ← Main execution script (Step 17)
├── README.md                       ← This documentation
│
├── in1Utils/
│   ├── cfgUtils.py                 ← Logging, INI handling, relative-path helper
│   ├── mapperUtils.py              ← Path resolution, I/O helpers, diagnostics
│   └── caamlUtils.py               ← Placeholder for future CAAML v6 integration
│
├── in2Matrix/
│   └── avaPotMatrix.py             ← Avalanche Distribution × Size × modType legend
│
└── com3AvaScenFilter/
    └── avaScenFilter.py            ← Core filtering logic (region / flow / elevation / legend)
```
---

## Purpose

- The Avalanche Scenario Mapper consumes the **AvaDirectoryResults** dataset from Step 15 of the Model Chain and generates **scenario-specific subsets** for visualization, web mapping, or export.

- Each scenario is defined in `avaScenMapperCfg.ini` and represents a combination of:
    - **Geographic filters** → LKGebietID / LWDGebietID / Region  
    - **Physical filters** → subcatchment (subC), sector, elevation range  
    - **Flow type filters** → dry / wet  
    - **Avalanche legend** → AvaPotential × avalanche size (PEM_header)  
    - **Deduplication rule** → keep only largest relative size (rSize)

### Results
- Each configured scenario is exported as:
```
13_avaScenMaps/avaScen_<Scenario>.parquet
13_avaScenMaps/avaScen_<Scenario>.geojson
```

Optionally, a combined master file can be created:
```
13_avaScenMaps/avaScen_Master.parquet
13_avaScenMaps/avaScen_Master.geojson
```

- Each dataset contains paired geometries:
    - **modType = rel** → Release Area  
    - **modType = res** → Avalanche Outline  

- Linked uniquely by **(praID, resultID)**.

---

## Execution

- Run from within the Pixi environment:

```bash
pixi shell -e dev
pixi run -e dev python runAvaScenMapper.py
```

- or standalone:

```bash
python runAvaScenMapper.py --cfg avaScenMapperCfg.ini
```

- Logs are written automatically to:

```
13_avaScenMaps/runAvaScenMapper_<timestamp>.log
```
---
##  CAIROS Mapper — Quick Start Guide

- The **CAIROS Avalanche Scenario Mapper** can be run **inside the Pixi environment** or **stand-alone**.

### Run inside Pixi

- From within the `cairosMapper/` directory:

```bash
# Activate Pixi environment
pixi shell

# Run the mapper (task defined in pyproject.toml)
pixi run mapper
```

Or explicitly select the environment and script:

```bash
pixi shell -e dev
pixi run -e dev python runAvaScenMapper.py
```

```bash
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
```

### Run standalone (without Pixi)

- If you already have the required Python dependencies installed:

```bash
python runAvaScenMapper.py --cfg avaScenMapperCfg.ini
```

- You can also provide an absolute path to a custom configuration:

```bash
python runAvaScenMapper.py --cfg /path/to/local_avaScenMapperCfg.ini
```

### Log output

- All run-time information and scenario summaries are written automatically to:

```
13_avaScenMaps/runAvaScenMapper_<timestamp>.log
```

- Each log contains:
    - input / output directory paths  
    - active filters or CAAML scenarios  
    - summary statistics per scenario  
    - total runtime

- Outputs will appear under:

```
13_avaScenMaps/<caseFolder>/avaScen_<scenario>.parquet
13_avaScenMaps/<caseFolder>/avaScen_<scenario>.geojson
```

---
## Configuration overview (`avaScenMapperCfg.ini`)

```ini
# --------------------------- Avalanche Scenario Mapper --------------------------- #
#
# Purpose :
#   Filters avaDirectoryResults.parquet into scenario-specific subsets
#   for visualization, mapping, and publication.
#
# Usage :
#   - Standalone :  python runAvaScenMapper.py --cfg avaScenMapperCfg.ini
#   - Integrated :  Executed as Step 17 of the Avalanche Scenario Model Chain.
#
# Pixi environment :
#   pixi shell -e dev
#   pixi run -e dev python runAvaScenMapper.py
#
# Input  :  12_avaDirectory/avaDirectoryResults.parquet  (from Step 15)
# Output :  13_avaScenMaps/avaScen_<Scenario>.parquet / .geojson
#
# Workflow context :
#   Consumes Step 15 Avalanche Directory results and prepares
#   scenario-specific subsets for visualization and dissemination.
#
# Author :
#   Christoph Hesselbach
#
# Institution :
#   Austrian Research Centre for Forests (BFW)
#   Department of Natural Hazards | Snow and Avalanche Unit
#
# Version :
#   2025-11
#
# ---------------------------------------------------------------------------------- #



# ------------------ Workflow control ------------------ #
[WORKFLOW]

# Check only the available attributes in avaDirectoryResults.parquet
#   True  → print columns and exit (diagnostic mode)
#   False → continue full workflow
checkAvaDirResult = True

# Enable or disable the Scenario Mapper (Step 16)
mapperRun = True

# If True, merge all scenario results into one master dataset
mapperMakeMaster = False

# Log level (DEBUG, INFO, WARNING, ERROR)
logLevel = INFO

# Path resolution mode :
#   AvaScenDirectory : auto-resolve based on standard Model Chain folder tree
#   customPaths      : use explicit entries in [PATHS]
mapperPathMode = AvaScenDirectory

# Enable external CAAML JSON feed (not yet implemented)
mapperUseCaaml = False
#mapperCaamlURL  =
#mapperCaamlFile =



# ------------------ Path definitions ------------------ #
[PATHS]
# Base project directory (used when mapperPathMode = AvaScenDirectory)
baseDir = /path/to/AvaScenarioModelChain/Directory

# Optional explicit paths (used only when mapperPathMode = customPaths)
#avaDirectoryResults = /path/to/avaDirectoryResults.parquet
#avaScenMapsDir      = /path/to/output/avaScenMaps
#refTif              = /path/to/00_input/10DTM_projectName.tif



# --------------------------- Scenario definitions --------------------------- #
# Each [FILTER.<name>] defines one independent scenario.
#
# You can define as many scenario sections as required — each one
# representing a unique combination of region, flow type, elevation,
# and avalanche distribution/size potential. Example :
#     [FILTER.winter]
#     [FILTER.spring]
#     [FILTER.<....>]
#
# All active scenarios to be processed must be listed under :
#     [FILTER]
#     filters = winter, spring, ...
#
# --- Area filters ---
# Define administrative (LKGebietID) and forecast (LWDGebietID) regions.
# Both lists can be used together or individually.
# regionMode controls the logical combination :
#     or  → keep PRAs matching either LKGebietID or LWDGebietID  (union)
#     and → keep only PRAs matching both region filters           (intersection)
# If only one of the two region lists is provided, regionMode has no effect.
#
# --- Scenario filters ---
# Control physical and spatial filtering within the selected regions :
#   subC       : Subcatchment identifier (integer, e.g. 500)
#   sector     : Aspect / direction of potential release areas (E, N, S, W)
#   flow       : Flow type (dry / wet) defining the avalanche regime
#   elevMin / elevMax : Elevation band (m a.s.l.) restricting PRA selection
#                       to a specific vertical range
#
# --- Avalanche distribution & size potential ---
# Define the avalanche hazard and target scenario size class :
#   AvaDistributionPotential : Avalanche hazard potential level
#                              (very high, high, moderate, low)
#   AvaSizePotential         : Reference avalanche size scenario (2–5)
#                              formerly “PEM_header”
#   applySingleRsizeRule     : If True, deduplicates PRAs by keeping only
#                              the largest rSize variant per unique PRA
#
# --- Output ---
# During execution, each scenario will be filtered and exported
# separately as :
#     13_avaScenMaps/avaScen_<name>.parquet / .geojson
#
# The syntax and key names follow the Avalanche Scenario Model Chain
# convention and are parsed automatically by mapperUtils.parseFilterConfig().
# ---------------------------------------------------------------------------------- #


[FILTER]
# Active scenario filters (by section name)
filters = winter, spring


# ------------------ Scenario: Winter ------------------ #
[FILTER.winter]
name = Winter

# --- Area filters ---
LKRegionID  = 70601, 70602
LwdRegionID = IT-32-BZ-18-02, IT-32-TN-16
regionMode  = or

# --- Scenario filters ---
subC        = 500
sector      = E,N,S,W
flow        = dry
elevMin     = 1800
elevMax     = 5000

# --- Avalanche distribution & size potential ---
AvaDistributionPotential = very high
AvaSizePotential         = 5
applySingleRsizeRule     = True


# ------------------ Scenario: Spring ------------------ #
[FILTER.spring]
name = Spring

# --- Area filters ---
LKRegionID  = 70601, 70602
LwdRegionID = IT-32-BZ-18-02, IT-32-TN-16
regionMode  = or

# --- Scenario filters ---
subC        = 500
sector      = E,S,W
flow        = wet
elevMin     = 1800
elevMax     = 2200

# --- Avalanche distribution & size potential ---
AvaDistributionPotential = very high
AvaSizePotential         = 3
applySingleRsizeRule     = True


```

---

## Data Model — AvaDirectoryResults

- The **AvaDirectoryResults** dataset (Step 15) is the structured summary of all avalanche simulation outputs.  
- Each row represents one model result for one Potential Release Area (PRA) and one result type (`modType`).

### Key columns
| Field | Type | Description |
|-------|------|-------------|
| **praID** | *int* | Unique Potential Release Area identifier |
| **resultID** | *str* | Unique simulation run identifier (pairs `rel` + `res`) |
| **modType** | *str* | `"res"` = avalanche outline / `"rel"` = release area |
| **LKGebiet** | *str* | Avalanche commission region |
| **LWDGebietID** | *str* | Avalanche warning region ID |
| **subC** | *int* | Sub-catchment identifier |
| **sector** | *str* | Aspect sector (e.g. N, NE, E, SE, S, SW, W, NW) |
| **elevMin** | *float* | Minimum elevation (m a.s.l.) |
| **elevMax** | *float* | Maximum elevation (m a.s.l.) |
| **flow** | *str* | Flow regime: `"dry"` or `"wet"` |
| **PPM** | *int* | Potential Path Mobility (size of release area) |
| **PEM** | *int* | Potential Event Mobility (size in scenario context) |
| **rSize** | *int* | Relative size class derived from PPM – PEM |
| **AvaDistributionPotential** | *str* | Avalanche hazard potential (e.g. very high, high, moderate, low) |
| **AvaSizePotential** | *int* | Scenario target avalanche size class (2 – 5) |
| **praAreaM** | *float* | PRA area [m²] |
| **praAreaSized** | *int* | PRA area size class |
| **praAreaVol** | *float* | Estimated release volume [m³] |
| **praElevBand** | *str* | Elevation band label (e.g. `"1800–2000"`) |
| **praElevBandRule** | *str* | Rule used for band assignment (e.g. `"mean"`) |
| **pathCellcounts** | *str (path)* | Raster path for cell counts |
| **pathTravelAngleMax(_sized)** | *str (path)* | Raster path for maximum travel angle |
| **pathTravelLengthMax(_sized)** | *str (path)* | Raster path for maximum travel length |
| **pathZdelta(_sized)** | *str (path)* | Raster path for vertical elevation difference |
| **geometry** | *GeoJSON geometry* | Polygon or MultiPolygon (`rel` or `res`) |

---

### `avaScen_Spring.geojson` example (simplified)

```json
{
  "type": "FeatureCollection",
  "name": "avaScen_Spring",
  "features": [
    {
      "type": "Feature",
      "properties": {
        "praID": 8800001,
        "resultID": "0035c942e8",
        "modType": "res",
        "LKGebiet": "CANAZEI - MAZZIN - CAMPITELLO",
        "LWDGebietID": "IT-32-TN-16",
        "subC": 500,
        "sector": "E",
        "flow": "wet",
        "PPM": 4,
        "PEM": 3,
        "rSize": 4,
        "AvaDistributionPotential": "very high",
        "AvaSizePotential": 3
      },
      "geometry": { "type": "MultiPolygon", "coordinates": [...] }
    },
    {
      "type": "Feature",
      "properties": {
        "praID": 8800001,
        "resultID": "0035c942e8",
        "modType": "rel",
        "LKGebiet": "CANAZEI - MAZZIN - CAMPITELLO",
        "LWDGebietID": "IT-32-TN-16",
        "subC": 500,
        "sector": "E",
        "flow": "wet",
        "PPM": 4,
        "PEM": 3,
        "rSize": 4,
        "AvaDistributionPotential": "very high",
        "AvaSizePotential": 3
      },
      "geometry": { "type": "Polygon", "coordinates": [...] }
    }
  ]
}
```

---

## Relation between Release (`rel`) and Avalanche (`res`)

- Each PRA (Potential Release Area) is represented by **two geometries** in the dataset — one *release area* (`rel`) and one *avalanche outline* (`res`).  
- These paired geometries together describe the simulated avalanche event.

| modType | Meaning |
|----------|----------|
| `rel` | Release Area geometry — derived from the delineated PRA |
| `res` | Avalanche Outline geometry — simulated runout extent |

- Both entries share identical metadata and are linked by the same **`praID`** and **`resultID`**.
- Use **(`praID`, `resultID`)** as the composite key to connect each *release area* with its *avalanche outline*.
- **Note:**  
  - `praID` geometry alone is not unique, because a single PRA can appear in multiple simulations or scenarios.  
  - Each `resultID` uniquely identifies one simulation event and groups **exactly two entries** —  
    → one `rel` (release area) and one `res` (avalanche outline).  
  - This pairing ensures that the *release area polygon* and *avalanche runout polygon* can always be traced to the same modeled event, even across multiple simulation batches or scenarios.

### Additional note on data redundancy

- A single PRA may be associated with **multiple simulation results**, for example, across different **size classes, flow types, or scenario configurations**.  
- Each of these variants creates its own `rel`/`res` pair with a distinct `resultID`.  
- As a result, the same PRA geometry can appear multiple times within the dataset.  

- This redundancy is intentionally maintained to preserve **traceability and completeness** but could be reconsidered for final or aggregated datasets to achieve a **significant reduction in data volume**.

---

## Typical workflow

1. Run Steps 00 – 15 of the Avalanche Scenario Model Chain to generate AvaDirectoryResults.
2. Configure scenario filters in local_avaScenMapperCfg.ini.
3. Execute python runAvaScenMapper.py.
4. Inspect outputs in 13_avaScenMaps/.
5. Visualize in QGIS or publish via web viewer.

-  **NOTE**:
    - Diagnostic mode (`checkAvaDirResult = True`) lists all available attributes and exits before filtering.

---

## Module Responsibilities

| Module | Responsibility |
|---------|----------------|
| `in1Utils.cfgUtils` | Logging & INI management |
| `in1Utils.mapperUtils` | Path resolution, GDF I/O, diagnostics |
| `in2Matrix.avaPotMatrix` | Avalanche potential × size × modType legend |
| `com3AvaScenFilter.avaScenFilter` | Scenario filtering logic |
| `runAvaScenMapper` | Main Step 16 orchestrator |

---

## Output summary

```text
13_avaScenMaps/
├── avaScen_<Scenario>.parquet         ← per-scenario results (GeoDataFrame)
├── avaScen_<Scenario>.geojson         ← per-scenario GeoJSON export
├── avaScen_Master.parquet             ← combined dataset (optional)
├── avaScen_Master.geojson             ← combined dataset (optional)
└── runAvaScenMapper_<timestamp>.log   ← log file
```
---

## Summary

- Step 16 of the Avalanche Scenario Model Chain.
- Converts AvaDirectoryResults into scenario-specific subsets.  
- Filters by region, flow type, elevation, and avalanche potential.  
- Produces paired entries (`modType = res / rel`) linked by `(praID, resultID)`.  
- Exports ready-to-use GeoJSON and Parquet datasets in `13_avaScenMaps/`.

---
