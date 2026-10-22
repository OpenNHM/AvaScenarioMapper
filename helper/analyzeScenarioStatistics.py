#!/usr/bin/env python3
"""Calculate publication-ready statistics for avalanche scenarios.

Outputs a JSON report and CSV tables containing selected PRA fractions,
avalanche footprints, PPM/PEM class footprints, raster summaries, and exposed
OSM road length. Overlapping avalanche polygons and road segments are dissolved
before measuring, so totals are not inflated by duplicate scenario features.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import rasterio
from rasterio.features import shapes
from shapely.geometry import shape


MODEL_ROOT = Path(r"D:\Cairos\ModelChainResults")
DEFAULT_MASTER = MODEL_ROOT / r"Euregio\cairosAvaMaps\12_avaDirectory\pilotBrenner\avaDirectoryResults.parquet"
DEFAULT_EXTENT = MODEL_ROOT / r"_gis\issw\pilotBrennerExtentMerge.gpkg"
DEFAULT_ROADS = MODEL_ROOT / r"_gis\issw\omsRoadLine.gpkg"
DEFAULT_SCENARIO_ROOT = MODEL_ROOT / r"Euregio\cairosAvaMaps\13_avaScenMaps\issw"
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "analysis" / "scenario_statistics"
SCENARIOS = {
    "dry_winter": "ISSW-North2000",
    "wet_spring": "ISSW-South2500",
}
MOTOR_ROAD_CLASSES = {
    "motorway", "motorway_link", "trunk", "trunk_link", "primary",
    "primary_link", "secondary", "secondary_link", "tertiary",
    "tertiary_link", "unclassified", "residential", "living_street",
    "service", "road", "escape",
}


def union_geometry(series: gpd.GeoSeries):
    clean = series.loc[series.notna() & ~series.is_empty]
    if clean.empty:
        return None
    if hasattr(clean, "union_all"):
        return clean.union_all()
    return clean.unary_union


def valid_polygon_rows(data: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    data = data.loc[data.geometry.notna() & ~data.geometry.is_empty].copy()
    if hasattr(data.geometry, "make_valid"):
        data.geometry = data.geometry.make_valid()
    else:
        data.geometry = data.geometry.buffer(0)
    return data.loc[data.geometry.notna() & ~data.geometry.is_empty].copy()


def projected(data: gpd.GeoDataFrame, crs) -> gpd.GeoDataFrame:
    if data.crs is None:
        raise ValueError("A vector input has no CRS")
    return data.to_crs(crs) if data.crs != crs else data.copy()


def master_pra_count(path: Path) -> int:
    table = pq.read_table(path, columns=["praID", "modType"])
    frame = table.to_pandas()
    rel = frame.loc[frame["modType"].astype(str).str.lower() == "rel", "praID"]
    return int(rel.nunique(dropna=True))


def raster_summary(path: Path, region_geometry, region_crs) -> dict:
    with rasterio.open(path) as src:
        if src.crs is None:
            raise ValueError(f"Raster has no CRS: {path}")
        # The scenario rasters already cover the pilot. Mask nodata and values
        # outside the vector extent without resampling the source values.
        region = gpd.GeoSeries([region_geometry], crs=region_crs).to_crs(src.crs)
        from rasterio.mask import mask

        values, transform = mask(src, [region.iloc[0]], crop=True, filled=False)
        band = values[0]
        valid = band.compressed()
        valid = valid[np.isfinite(valid)]
        positive = valid[valid > 0]
        pixel_area = abs(transform.a * transform.e - transform.b * transform.d)
        if not len(positive):
            return {"file": str(path), "positiveCells": 0, "coverageKm2": 0.0}
        return {
            "file": str(path),
            "unit": src.tags(1).get("units") or src.tags().get("units"),
            "positiveCells": int(len(positive)),
            "coverageKm2": float(len(positive) * pixel_area / 1_000_000),
            "minimum": float(np.min(positive)),
            "mean": float(np.mean(positive)),
            "median": float(np.median(positive)),
            "p95": float(np.percentile(positive, 95)),
            "maximum": float(np.max(positive)),
        }


def class_footprints(res: gpd.GeoDataFrame, scenario: str) -> list[dict]:
    rows = []
    for field in ("PPM", "PEM"):
        actual = field if field in res.columns else field.lower()
        if actual not in res.columns:
            continue
        numeric = pd.to_numeric(res[actual], errors="coerce")
        for value in sorted(numeric.dropna().unique()):
            geometry = union_geometry(res.loc[numeric == value, "geometry"])
            rows.append({
                "scenario": scenario,
                "classification": field,
                "sizeClass": int(value) if float(value).is_integer() else float(value),
                "featureCount": int((numeric == value).sum()),
                "footprintKm2": float(geometry.area / 1_000_000) if geometry else 0.0,
            })
    return rows


def clipped_road_metrics(roads: gpd.GeoDataFrame, footprint) -> dict:
    if footprint is None:
        return {"allHighwaysKm": 0.0, "motorRoadKm": 0.0, "intersectingFeatures": 0}
    candidates = roads.loc[roads.geometry.intersects(footprint)].copy()
    if candidates.empty:
        return {"allHighwaysKm": 0.0, "motorRoadKm": 0.0, "intersectingFeatures": 0}
    candidates.geometry = candidates.geometry.intersection(footprint)
    total_geometry = union_geometry(candidates.geometry)
    motor = candidates.loc[
        candidates.get("highway", pd.Series(index=candidates.index, dtype=object))
        .astype(str).str.lower().isin(MOTOR_ROAD_CLASSES)
    ]
    motor_geometry = union_geometry(motor.geometry)
    return {
        "allHighwaysKm": float(total_geometry.length / 1000) if total_geometry else 0.0,
        "motorRoadKm": float(motor_geometry.length / 1000) if motor_geometry else 0.0,
        "intersectingFeatures": int(len(candidates)),
    }


def road_exposure(roads: gpd.GeoDataFrame, footprint, scenario: str) -> tuple[dict, list[dict]]:
    metrics = clipped_road_metrics(roads, footprint)
    if footprint is None or not metrics["intersectingFeatures"]:
        return metrics, []
    candidates = roads.loc[roads.geometry.intersects(footprint)].copy()
    candidates.geometry = candidates.geometry.intersection(footprint)

    class_field = next(
        (name for name in ("highway", "fclass", "type", "class") if name in candidates.columns),
        None,
    )
    classes = []
    if class_field:
        for value, group in candidates.groupby(class_field, dropna=False):
            geometry = union_geometry(group.geometry)
            classes.append({
                "scenario": scenario,
                "roadClassField": class_field,
                "roadClass": "unknown" if pd.isna(value) else str(value),
                "exposedKm": float(geometry.length / 1000) if geometry else 0.0,
            })
    return metrics, classes


def destructive_size_road_exposure(
    path: Path, roads: gpd.GeoDataFrame, region_geometry, region_crs, scenario: str
) -> list[dict]:
    """Measure clipped road length in destructive-size raster value bands."""
    from rasterio.mask import mask

    with rasterio.open(path) as src:
        region = gpd.GeoSeries([region_geometry], crs=region_crs).to_crs(src.crs)
        values, transform = mask(src, [region.iloc[0]], crop=True, filled=False)
        band = values[0]
        data = band.filled(np.nan).astype("float32")
        valid = ~np.ma.getmaskarray(band) & np.isfinite(data) & (data > 0)
        definitions = [
            ("1–<2", valid & (data >= 1) & (data < 2)),
            ("2–<3", valid & (data >= 2) & (data < 3)),
            ("3–<4", valid & (data >= 3) & (data < 4)),
            ("≥4", valid & (data >= 4)),
            ("≥3", valid & (data >= 3)),
        ]
        rows = []
        for label, selected in definitions:
            polygons = [
                shape(geometry)
                for geometry, value in shapes(
                    selected.astype("uint8"), mask=selected, transform=transform
                )
                if value == 1
            ]
            raster_zone = union_geometry(gpd.GeoSeries(polygons, crs=src.crs)) if polygons else None
            if raster_zone is not None:
                raster_zone = gpd.GeoSeries([raster_zone], crs=src.crs).to_crs(region_crs).iloc[0]
            metrics = clipped_road_metrics(roads, raster_zone)
            rows.append({
                "scenario": scenario,
                "destructiveSizeBand": label,
                **metrics,
            })
        return rows


def scenario_files(root: Path, scenario_id: str) -> dict[str, Path]:
    folder = root / f"avaScen_{scenario_id}"
    base = folder / f"avaScen_{scenario_id}"
    return {
        "vector": base.with_suffix(".gpkg"),
        "zDeltaSized": folder / f"avaScen_{scenario_id}_pathZdelta_sized.tif",
        "travelLengthSized": folder / f"avaScen_{scenario_id}_pathTravellengthmax_sized.tif",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--master", type=Path, default=DEFAULT_MASTER)
    parser.add_argument("--extent", type=Path, default=DEFAULT_EXTENT)
    parser.add_argument("--roads", type=Path, default=DEFAULT_ROADS)
    parser.add_argument("--scenario-root", type=Path, default=DEFAULT_SCENARIO_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    required = [args.master, args.extent, args.roads]
    scenario_inputs = {name: scenario_files(args.scenario_root, sid) for name, sid in SCENARIOS.items()}
    required.extend(path for files in scenario_inputs.values() for path in files.values())
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError("Missing input(s):\n" + "\n".join(missing))

    args.output.mkdir(parents=True, exist_ok=True)
    extent = valid_polygon_rows(gpd.read_file(args.extent))
    if extent.crs is None or extent.crs.is_geographic:
        raise ValueError("Study extent must use a projected CRS for area/length statistics")
    analysis_crs = extent.crs
    region = union_geometry(extent.geometry)
    study_area_km2 = float(region.area / 1_000_000)
    roads = projected(gpd.read_file(args.roads), analysis_crs)
    roads = roads.loc[roads.geometry.notna() & ~roads.geometry.is_empty].copy()
    roads = roads.loc[roads.geometry.intersects(region)].copy()
    total_pras = master_pra_count(args.master)

    summary_rows, size_rows, road_rows, destructive_road_rows, report_scenarios = [], [], [], [], {}
    for scenario_name, files in scenario_inputs.items():
        print(f"Analyzing {scenario_name}: {files['vector']}")
        scenario = projected(gpd.read_file(files["vector"]), analysis_crs)
        scenario = valid_polygon_rows(scenario)
        mod_type = scenario["modType"].astype(str).str.lower()
        rel = scenario.loc[mod_type == "rel"].copy()
        res = scenario.loc[mod_type == "res"].copy()
        rel = rel.loc[rel.geometry.intersects(region)].copy()
        selected_pras = int(rel["praID"].nunique(dropna=True))

        res = res.loc[res.geometry.intersects(region)].copy()
        res.geometry = res.geometry.intersection(region)
        res = valid_polygon_rows(res)
        footprint = union_geometry(res.geometry)
        footprint_km2 = float(footprint.area / 1_000_000) if footprint else 0.0
        road_metrics, scenario_road_rows = road_exposure(roads, footprint, scenario_name)
        scenario_destructive_roads = destructive_size_road_exposure(
            files["zDeltaSized"], roads, region, analysis_crs, scenario_name
        )
        scenario_size_rows = class_footprints(res, scenario_name)
        rasters = {
            "zDeltaSized": raster_summary(files["zDeltaSized"], region, analysis_crs),
            "travelLengthSized": raster_summary(files["travelLengthSized"], region, analysis_crs),
        }
        row = {
            "scenario": scenario_name,
            "selectedPRAs": selected_pras,
            "allPilotPRAs": total_pras,
            "selectedPRAPercent": 100 * selected_pras / total_pras if total_pras else 0.0,
            "affectedAreaKm2": footprint_km2,
            "studyAreaKm2": study_area_km2,
            "affectedStudyAreaPercent": 100 * footprint_km2 / study_area_km2 if study_area_km2 else 0.0,
            "exposedAllHighwaysKm": road_metrics["allHighwaysKm"],
            "exposedMotorRoadKm": road_metrics["motorRoadKm"],
            "intersectingRoadFeatures": road_metrics["intersectingFeatures"],
        }
        summary_rows.append(row)
        size_rows.extend(scenario_size_rows)
        road_rows.extend(scenario_road_rows)
        destructive_road_rows.extend(scenario_destructive_roads)
        report_scenarios[scenario_name] = {**row, "rasters": rasters}

    pd.DataFrame(summary_rows).to_csv(args.output / "scenario_summary.csv", index=False)
    pd.DataFrame(size_rows).to_csv(args.output / "scenario_size_class_area.csv", index=False)
    pd.DataFrame(road_rows).to_csv(args.output / "scenario_road_exposure_by_class.csv", index=False)
    pd.DataFrame(destructive_road_rows).to_csv(
        args.output / "scenario_road_exposure_by_destructive_size.csv", index=False
    )
    report = {
        "method": {
            "analysisCRS": str(analysis_crs),
            "affectedArea": "Dissolved RES polygons clipped to the study extent",
            "selectedPRAs": "Unique praID among REL features divided by unique REL praID in the pilot master",
            "roadExposure": "Length after clipping OSM lines to the dissolved RES footprint; motor roads exclude paths, tracks, cycleways, footways and pedestrian ways",
            "destructiveSizeRoadExposure": "Road lines clipped to pathZdelta_sized raster-cell polygons in fixed value bands",
            "classArea": "Dissolved footprint within each PPM/PEM class; classes may overlap each other",
        },
        "inputs": {"master": str(args.master), "extent": str(args.extent), "roads": str(args.roads)},
        "studyAreaKm2": study_area_km2,
        "allPilotPRAs": total_pras,
        "scenarios": report_scenarios,
    }
    (args.output / "scenario_statistics.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    print(pd.DataFrame(summary_rows).to_string(index=False))
    print(f"Wrote analysis to: {args.output}")


if __name__ == "__main__":
    main()
