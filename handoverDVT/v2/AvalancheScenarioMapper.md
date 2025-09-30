
# Avalanche Simulation Data – Handover Notes

This document describes the avalanche simulation data prepared for the **pilot Brenner dataset**,  
focusing on the `/11_avaDirectory` directory and the scenario-specific exports in `/12_avaScenMaps`.

---

## `/11_avaDirectory/`

This folder contains the **full avalanche inventory** (~26k features = ~13k avalanches + ~13k release areas).  

- **Result rasters**  
  - ~6 raster products per avalanche run (e.g. `zDelta`, `travelLengthMax`, `travelAngleMax`, …).  
  - Stored in `com4_*` subfolders.  

- **avaDirectoryType.\***  
  - Base directory files: geometry + attributes, but **no raster paths**.  
  - Formats: `.csv`, `.geojson`, `.parquet`.  
  - Use as the starting point for enrichment.  

- **avaDirectoryResults.\***  
  - Enriched directory files: geometry + attributes + **relative raster paths**.  
  - Formats: `.csv`, `.geojson`, `.parquet`.  
  - These are the **main input** for scenario filtering and WebGIS.  

---

## `/12_avaScenMaps/`

This folder contains **scenario-specific exports**, generated after applying filters  
(area IDs, sector, flow type, elevation bands, etc.):  

- **avaScen_<name>.parquet**  
  - Filtered scenario, geometry + attributes + raster paths.  
  - Fast and compact for backend processing.  

- **avaScen_<name>.geojson**  
  - Same as above, but WebGIS-ready.  
  - Includes geometry, attributes, and relative raster paths (`path_*`).  

- **avaScenMaster.\***  
  - Optional combined export of **all filtered scenarios** in one file.  
  - Written if `makeMaster=True` during filtering.  

---

## Preprocessing Pipeline

```text
avaDirectoryType.*          (geometry + attributes, no paths)
       │
       ▼
 buildFileIndex             (scan com4_* rasters)
       │
       ▼
avaDirectoryResults.*       (geometry + attributes + raster paths)
       │
       ▼
 apply scenario filters     (area IDs, subC, sector, flow, elevation)
       │
       ▼
avaScen_<name>.parquet/geojson   (per-scenario outputs)
       │
       ▼
avaScenMaster.parquet/geojson    (optional combined export)
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

## Applied Filters for Testing

### Scenario 1 – Brenner South

```python
areaLwdIds = ['IT-32-BZ-04-01', 'IT-32-BZ-05-01']
subCs      = [500]
sectors    = ["S", "E"]
flows      = ["Dry"]
elevMin    = 0
elevMax    = 2000
```

#### Output:

* AREA 1 | Scenario 1 → 12_avaScenMaps/avaScen_BrennerSued.parquet / .geojson
* (per-area exports also available if makeMaster=False)

### Scenario 2 – Brenner North

```python
areaLwdIds = ['AT-07-22', 'AT-07-23-02']
subCs      = [500]
sectors    = ["N", "W"]
flows      = ["Dry"]
elevMin    = 1800
elevMax    = 2400
```

#### Output:

* AREA 2 | Scenario 2 → 12_avaScenMaps/avaScen_BrennerNord.parquet / .geojson
* (per-area exports also available if makeMaster=False)

Summary Table
Scenario	Area IDs	SubC	Sectors	Flows	Elevation Range	Output Files
Brenner South	IT-32-BZ-04-01, IT-32-BZ-05-01	500	S, E	Dry	0 – 2000 m	avaScen_BrennerSued.parquet / .geojson
Brenner North	AT-07-22, AT-07-23-02	500	N, W	Dry	1800 – 2400 m	avaScen_BrennerNord.parquet / .geojson

## Summary Table (when `makeMaster = False`)

| Scenario       | Area IDs                        | SubC | Sectors | Flows | Elevation Range | Output Files                              |
|----------------|---------------------------------|------|---------|-------|-----------------|------------------------------------------|
| Brenner South  | IT-32-BZ-04-01, IT-32-BZ-05-01 | 500  | S, E    | Dry   | 0 – 2000 m      | avaScen_BrennerSued.parquet / .geojson   |
| Brenner North  | AT-07-22, AT-07-23-02          | 500  | N, W    | Dry   | 1800 – 2400 m   | avaScen_BrennerNord.parquet / .geojson   |
