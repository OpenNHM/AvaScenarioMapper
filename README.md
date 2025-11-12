#
# 🏔️ CAIROS Avalanche Scenario Mapper (2025-11 Update)

<p align="center">
  <img src="https://media.giphy.com/media/3Xzlefv57zcrVIPPRN/giphy.gif"
       alt="CAIROS Avalanche Mapper"
       width="300"/>
</p>

<h3 align="center">⚠️ Handle with care — work in progress</h3>



### Overview
- The Avalanche Scenario Mapper is developed within the **EUREGIO Project CAIROS**.
- It represents **Step 16** of the CAIROS Avalanche Model Chain.
- The Mapper post-processes avalanche simulation results produced in Step 15 (AvaDirectoryResults) and generates **scenario-specific subsets** for mapping, visualization, and publication.
- It consumes **AvaDirectoryResults.parquet** and produces **per-scenario GeoDataFrames / GeoJSONs**.
- Integrated logging and configuration follow the same conventions as the Model Chain.

  - Main entrypoint: `runAvaScenMapper.py`  
  - Controlled via: `avaScenMapperCfg.ini` (+ local override)  
  - Outputs to: `13_avaScenMaps/`

---
#
#
## Cairos/cairosMapper/ Repository layout

```text
../cairosMapper/
├── avaScenMapperCfg.ini            ← Main configuration (global + scenarios)
├── local_avaScenMapperCfg.ini      ← Local override for development/testing
├── runAvaScenMapper.py             ← Main execution script (Step 16)
├── README.md                       ← This documentation
│
├── in1Utils/
│   ├── cfgUtils.py                 ← Logging, INI handling, relative-path helper
│   ├── mapperUtils.py              ← Path resolution, I/O helpers, diagnostics
│   └── caamlUtils.py               ← Placeholder for future CAAML integration
│
├── in2Matrix/
│   └── avaPotMatrix.py             ← Avalanche potential–size–modType legend
│
└── com3AvaScenFilter/
    └── avaScenFilter.py            ← Core filtering logic (area, elevation, flow, legend)
```

---
#
#
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
#
#
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
#
#
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

---

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
#
#
## Configuration overview (`avaScenMapperCfg.ini`)

```ini
# ============================================================
# CAIROS Avalanche Scenario Mapper
# ============================================================
# Purpose:
#   Filters avaDirectoryResults.parquet into scenario-specific subsets
#   for visualization, mapping, and web export.
#
# Usage:
#   - Standalone:  python runAvaScenMapper.py --cfg avaScenMapperCfg.ini
#   - Integrated:  Executed as Step 16 from the CAIROS ModelChain.
#
# Pixi environment:
#   pixi shell -e dev
#   pixi run -e dev python runAvaScenMapper.py
#
# Input  :  12_avaDirectory/avaDirectoryResults.parquet  (from Step 15)
# Output :  13_avaScenMaps/avaScen_<Scenario>.parquet / .geojson
#
# Workflow context:
#   Consumes Step 15 avalanche directory results and prepares
#   scenario-specific subsets for visualization and publication.
#
# Author : CAIROS Project Team
# Version: 2025-11
# ============================================================



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

# Path resolution mode:
#   cairosPaths : auto-resolve based on standard CAIROS folder tree
#   customPaths : use explicit entries in [PATHS]
mapperPathMode = cairosPaths

# Enable external CAAML JSON feed (not yet implemented)
mapperUseCaaml = False
#mapperCaamlURL  = 
#mapperCaamlFile = 


# ------------------ Path definitions ------------------ #
[PATHS]
# Base project directory (used when mapperPathMode = cairosPaths)
baseDir = /media/christoph/Daten/Cairos/ModelChainProcess/cairosTutti/pilotSellaTest/alpha32_3_umax8_18_maxS5

# Optional explicit paths (used only when mapperPathMode = customPaths)
#avaDirectoryResults = /path/to/avaDirectoryResults.parquet
#avaScenMapsDir      = /path/to/output/avaScenMaps
#refTif              = /path/to/00_input/10DTM_projectName.tif



# ============================================================
# Scenario definitions
# ============================================================
# Each [FILTER.<name>] defines one independent scenario.
#
# You can define as many scenario sections as required — each one
# representing a unique combination of region, flow type, elevation,
# and avalanche potential. For example:
#     [FILTER.winter]
#     [FILTER.spring]
#     [FILTER.highRiskMultiRegion]
#
# All active scenarios to be processed must be listed under:
#     [FILTER]
#     filters = winter, spring, ...
#
# --- Area filters ---
# Define administrative (LKGebietID) and forecast (areaLWDID) regions.
# Both lists can be used together or individually.
# areaMode controls the logical combination:
#     or  → keep PRAs matching either LKGebietID or LWDGebietID  (union)
#     and → keep only PRAs matching both region filters           (intersection)
# If only one of the two region lists is provided, areaMode has no effect.
#
# --- Scenario filters ---
# Control physical and spatial filtering within the selected regions:
#   subC       : Subcatchment identifier (integer, e.g. 500) used to
#                select modelled catchments of matching scale or ID.
#   sector     : Aspect / direction of potential release areas (E, N, S, W).
#                Multiple entries can be listed, comma-separated.
#   flow       : Flow type (dry / wet) defining the avalanche regime.
#   elevMin / elevMax : Elevation band (m a.s.l.) restricting PRA selection
#                       to a specific vertical range.
#
# --- Avalanche potential & size ---
# Define the avalanche magnitude class to include in the scenario:
#   avaPotential : Avalanche hazard potential level
#                  (very high, high, moderat, low)
#   avaSize      : Target avalanche size (PEM header value)
#   applySingleRsizeRule : If True, deduplicates PRAs by keeping only
#                          the largest rSize variant per unique PRA.
#
# --- Output ---
# During execution, each scenario will be filtered and exported
# separately as:
#     13_avaScenMaps/avaScen_<name>.parquet / .geojson
#
# The syntax and key names follow the CAIROS ModelChain convention
# and are parsed automatically by mapperUtils.parseFilterConfig().
# ============================================================


[FILTER]
# Active scenario filters (by section name)
filters = winter, spring

# ------------------ Scenario: Winter ------------------ #
[FILTER.winter]
name = Winter

# --- Area filters ---
LKRegionID = 70601, 70602
LwdRegionID  = IT-32-BZ-18-02, IT-32-TN-16
regionMode   = or

# --- Scenario filters ---
subC        = 500
sector      = E,N,S,W
flow        = dry
elevMin     = 1800
elevMax     = 5000

# --- Avalanche potential & size ---
avaPotential = very high
avaSize      = 5
applySingleRsizeRule = True


# ------------------ Scenario: Spring ------------------ #
[FILTER.spring]
name = Spring

# --- Area filters ---
LKRegionID = 70601, 70602
LwdRegionID  = IT-32-BZ-18-02, IT-32-TN-16
regionMode   = or

# --- Scenario filters ---
subC        = 500
sector      = E,S,W
flow        = wet
elevMin     = 1800
elevMax     = 2200

# --- Avalanche potential & size ---
avaPotential = very high
avaSize      = 3
applySingleRsizeRule = True
```

---
#
#
## Data Model — AvaDirectoryResults

- The AvaDirectoryResults dataset (Step 15) is a structured summary of all avalanche simulation outputs.  
- Each row describes one model result for one PRA (Potential Release Area) and one modType (result type).

### Key columns
| Field | Type | Description |
|-------|------|-------------|
| **praID** | *int* | Unique Potential Release Area (PRA) identifier |
| **modType** | *str* | `"res"` = avalanche outline / `"rel"` = release area |
| **LKGebiet** | *str* | Administrative district name |
| **LWDGebietID** | *str* | Avalanche warning region ID |
| **LKRegion** | *str* | Administrative region / province name |
| **subC** | *int* | Subcatchment ID |
| **sector** | *str* | Aspect sector (e.g. N, NE, E, SE, S, SW, W, NW) |
| **elevMin** | *float* | Minimum elevation (m a.s.l.) |
| **elevMax** | *float* | Maximum elevation (m a.s.l.) |
| **flow** | *str* | Flow regime: `"dry"` or `"wet"` |
| **PPM** | *int* | Potential Parameter Mass (model input) |
| **PEM** | *int* | Potential Energy Mass (model input) |
| **rSize** | *int* | Avalanche relative size class |
| **avaPotential** | *str* | Avalanche potential level (e.g. low, medium, high, very high) |
| **praAreaM** | *float* | PRA polygon area in m² |
| **praAreaSized** | *int* | PRA area size class |
| **praAreaVol** | *float* | Estimated release volume (m³) |
| **praElevBand** | *str* | Elevation band label (e.g. `"1800–2000"`) |
| **praElevBandRule** | *str* | Rule used to assign elevation band (e.g. `"mean"`) |
| **pathCellcounts** | *str (path)* | Raster path(s) for cell counts |
| **pathTravelAngleMax(_sized)** | *str (path)* | Raster path(s) for maximum travel angle |
| **pathTravelLengthMax(_sized)** | *str (path)* | Raster path(s) for maximum travel length |
| **pathZdelta(_sized)** | *str (path)* | Raster path(s) for vertical elevation difference |
| **geometry** | *GeoJSON geometry* | Polygon or MultiPolygon (release or avalanche outline) |


### AvaScen_Spring.geojson example (simplified)
```json
{
  "type": "FeatureCollection",
  "name": "avaScen_Spring",
  "features": [
    {
      "type": "Feature",
      "properties": {
        "praID": 8800001,
        "modType": "res",
        "LKGebiet": "CANAZEI - MAZZIN - CAMPITELLO",
        "LWDGebietID": "IT-32-TN-16",
        "subC": 500,
        "sector": "E",
        "flow": "wet",
        "PPM": 4, "PEM": 3, "rSize": 4,
        "avaPotential": "very high"
      },
      "geometry": { "type": "MultiPolygon", "coordinates": [...] }
    },
    {
      "type": "Feature",
      "properties": {
        "praID": 8800001,
        "modType": "rel",
        "LKGebiet": "CANAZEI - MAZZIN - CAMPITELLO",
        "LWDGebietID": "IT-32-TN-16",
        "subC": 500,
        "sector": "E",
        "flow": "wet",
        "PPM": 4, "PEM": 3, "rSize": 4,
        "avaPotential": "very high"
      },
      "geometry": { "type": "Polygon", "coordinates": [...] }
    }
  ]
}
```

---
#
#
## Relation between Release (rel) and Avalanche (res)

- Each PRA (Potential Release Area) has **paired geometries**:

| modType | Meaning |
|----------|----------|
| rel | Release Area geometry |
| res | Avalanche Outline geometry |

- Both share identical metadata and the same `praID` and `resultID`.  
- Use **(praID, resultID)** as the composite key to link release and result geometries.
#
-  **NOTE**:
    - `praID` alone is not unique across the dataset.  
    - Each `resultID` groups exactly two entries → one release and one outline.

---
#
#
## Typical workflow

1. Run the full **CAIROS Model Chain** (Steps 00–15) to generate AvaDirectoryResults.  
2. Configure scenario filters in `local_avaScenMapperCfg.ini`.
3. Run `runAvaScenMapper.py`.  
4. Inspect outputs in `13_avaScenMaps/`.  
5. Visualize in QGIS or publish via web viewer.

-  **NOTE**:
    - Diagnostic mode (`checkAvaDirResult = True`) lists all available attributes and exits before filtering.

---
#
#
## Module Responsibilities

| Module | Responsibility |
|---------|----------------|
| `in1Utils.cfgUtils` | Logging & INI management |
| `in1Utils.mapperUtils` | Path resolution, GDF I/O, diagnostics |
| `in2Matrix.avaPotMatrix` | Avalanche potential × size × modType legend |
| `com3AvaScenFilter.avaScenFilter` | Scenario filtering logic |
| `runAvaScenMapper` | Main Step 16 orchestrator |

---
#
#
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
#
#
## Summary

- Step 16 of the CAIROS Avalanche Model Chain.  
- Converts AvaDirectoryResults into scenario-specific subsets.  
- Filters by region, flow type, elevation, and avalanche potential.  
- Produces paired entries (`modType = res / rel`) linked by `(praID, resultID)`.  
- Exports ready-to-use GeoJSON and Parquet datasets in `13_avaScenMaps/`.

---
