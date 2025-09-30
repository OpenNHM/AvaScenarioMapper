# Avalanche Simulation Data – Handover Notes

This document describes the avalanche simulation data prepared for the **pilot Brenner dataset**, focusing on the `/11_avaDirectory` directory and the scenario-specific exports in `/12_avaScenMaps`.

---

## `/11_avaDirectory/`

This folder contains the **full avalanche inventory** (~26k features = ~13k avalanches + ~13k release areas).  

- **Result rasters**  
  - ~6 raster products per avalanche run (e.g. `zDelta`, `travelLengthMax`, `travelAngleMax`, …).  
  - Stored in `com4_*` subfolders.  

- **avaDirectory.csv**  
  - Attributes only (no geometry).  
  - Lightweight table (~26k rows) → best for filtering scenarios quickly.  

- **avaDirectory.geojson**  
  - Geometry + attributes for all avalanches.  
  - Heavy (~73 MB), WebGIS-ready.  

- **avaDirectory.parquet**  
  - Geometry + attributes in Parquet format.  
  - Much smaller and loads 10–50× faster in Python/QGIS.  
  - Recommended for backend filtering and processing pipelines.  

---

## `/12_avaScenMaps/`

This folder contains **scenario-specific exports**, generated after applying filters (area IDs, sector, flow type, elevation bands, etc.):  

- **avaScen_LWD-*.csv**  
Filtered Avalanches - list for each area (attributes only).  

- **avaScenFilePaths_LWD-*.csv**  
  - Filtered Avalanches - list with resolved raster file paths.  

- **avaScen_LWD-*.geojson**  
  - iltered Avalanches polygons + attributes + relative raster paths (relative to `/11_avaDirectory/`).  
  - This is the recommended input for WebGIS.  

---
## Preprocessing Pipeline

```text
avaDirectory.csv
       │
       ▼
  Set Scenario Filter
       │
       ▼
    Filtered Avalanches (attributes only)
       │
       ▼
    avaDirectory.parquet (geometry + attributes)
       │
       ▼
    merge filtered Avalanches + geometry
       │
       ▼
    append raster file paths from indexAvaFiles.pkl
       │
       ▼
avaScen_LWD-*.geojson  (final scenario outputs, WebGIS-ready)
```
---
## Suggested WebGIS Workflow

1. **Filtering pipeline**  
   - Apply scenario filters first on **avaDirectory.csv** (fast).  
   - Once filtered avalanches are selected, merge back with **avaDirectory.parquet** to attach geometry.  
   - Add raster paths from the raster index (`indexAvaFiles.pkl`).  
   - Export a **final GeoJSON** with geometry + relative raster paths  
     → already prepared in `/12_avaScenMaps/`.  

2. **Display concept**  
   - **Zoomed out** → show only **polygons** 
      - avalanche outlines [blue], release areas [pink], see 1&2 screenshot below.  
   - **Zoomed in** (after a threshold) → load the linked **TIFF rasters on demand**  
     from the `path_*` fields in the scenario GeoJSON.  
      - avalanche raster [intensity], release areas [pink], see 3 screenshot below.  
   - This keeps the map lightweight while enabling rich raster detail when needed.  

![zoomConcept1](zoomConcept1.png)

![zoomConcept2](zoomConcept2.png)

![zoomConcept3](zoomConcept3.png)

---




## Applied filter for testing

```code
# --- Filters for Scenario 1 for area 1 & 2 ---

    areaLwdIds=['IT-32-BZ-04-01', 'IT-32-BZ-05-01'],  
    subCs=[500],
    sectors=["S", "E"],
    flows=["Dry"],
    elevMin=0000,
    elevMax=2000,
```
- Output for WebGIS:
    - AREA 1 | Scenario 1: `12_avaScenMaps/avaScen_LWD-IT-32-BZ-05-01.geojson` & 
    - AREA 2 | Scenario 1: `12_avaScenMaps/avaScen_LWD-IT-32-BZ-04-01.geojson`


and  
```
# --- Filters for Scenario 2 for area 3 & 4 ---

    areaLwdIds=['AT-07-22', 'AT-07-23-02'],  
    subCs=[500],
    sectors=["N", "W"],
    flows=["Dry"],
    elevMin=1800,
    elevMax=2400,
```
- Output for WegbGIS:
    - AREA 3 | Scenario 2: `12_avaScenMaps/avaScen_LWD-AT-07-22.geojson`
    - AREA 4 | Scenario 2: `12_avaScenMaps/avaScen_LWD-AT-07-23-02.geojson`