#!/usr/bin/env python3
"""Export styled scenario-size rasters as transparent web PNG overlays."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import numpy as np
import rasterio
from rasterio.enums import ColorInterp, Resampling
from rasterio.transform import array_bounds
from rasterio.warp import calculate_default_transform, reproject


MODEL_ROOT = Path(r"D:\Cairos\ModelChainResults")
DEFAULT_SCENARIO_ROOT = MODEL_ROOT / r"Euregio\cairosAvaMaps\13_avaScenMaps\issw"
DEFAULT_STYLE = MODEL_ROOT / r"_styles\styleDST_avaDirV_avySize.qml"
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "docs" / "data"
RASTERS = {
    "dry/destructive": ("ISSW-North2000", "pathZdelta_sized"),
    "dry/runout": ("ISSW-North2000", "pathTravellengthmax_sized"),
    "wet/destructive": ("ISSW-South2500", "pathZdelta_sized"),
    "wet/runout": ("ISSW-South2500", "pathTravellengthmax_sized"),
}


def raster_path(root: Path, scenario: str, metric: str) -> Path:
    folder = root / f"avaScen_{scenario}"
    return folder / f"avaScen_{scenario}_{metric}.tif"


def parse_qml(path: Path) -> tuple[np.ndarray, np.ndarray, float]:
    root = ET.parse(path).getroot()
    renderer = root.find(".//rasterrenderer")
    shader = root.find(".//colorrampshader")
    if renderer is None or shader is None:
        raise ValueError(f"No single-band pseudocolor renderer found in {path}")
    stops = []
    for item in shader.findall("item"):
        value = float(item.attrib["value"])
        color = item.attrib["color"].lstrip("#")
        rgb = tuple(int(color[index:index + 2], 16) for index in (0, 2, 4))
        stops.append((value, rgb))
    if len(stops) < 2:
        raise ValueError(f"Fewer than two color stops found in {path}")
    stops.sort(key=lambda item: item[0])
    return (
        np.asarray([item[0] for item in stops], dtype="float32"),
        np.asarray([item[1] for item in stops], dtype="float32"),
        float(renderer.attrib.get("opacity", "1")),
    )


def colorize(values: np.ndarray, stops: np.ndarray, colors: np.ndarray, opacity: float) -> np.ndarray:
    valid = np.isfinite(values) & (values > 0)
    clipped = np.clip(np.where(valid, values, stops[0]), stops[0], stops[-1])
    rgba = np.zeros((4, values.shape[0], values.shape[1]), dtype="uint8")
    for channel in range(3):
        rgba[channel] = np.interp(clipped, stops, colors[:, channel]).astype("uint8")
    rgba[3, valid] = round(255 * opacity)
    rgba[:3, ~valid] = 0
    return rgba


def export_png(source: Path, target: Path, stops, colors, opacity) -> dict:
    print(f"Reading: {source}")
    with rasterio.open(source) as src:
        if src.crs is None:
            raise ValueError(f"Raster has no CRS: {source}")
        transform, width, height = calculate_default_transform(
            src.crs, "EPSG:4326", src.width, src.height, *src.bounds
        )
        values = np.full((height, width), np.nan, dtype="float32")
        reproject(
            source=rasterio.band(src, 1), destination=values,
            src_transform=src.transform, src_crs=src.crs, src_nodata=src.nodata,
            dst_transform=transform, dst_crs="EPSG:4326", dst_nodata=np.nan,
            resampling=Resampling.nearest,
        )
    rgba = colorize(values, stops, colors, opacity)
    temporary = target.with_name(target.name + ".tmp.png")
    try:
        with rasterio.open(
            temporary, "w", driver="PNG", width=width, height=height, count=4,
            dtype="uint8", crs="EPSG:4326", transform=transform, compress="DEFLATE",
            zlevel=9,
        ) as dst:
            dst.write(rgba)
            dst.colorinterp = (
                ColorInterp.red, ColorInterp.green, ColorInterp.blue, ColorInterp.alpha
            )
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    west, south, east, north = array_bounds(height, width, transform)
    return {
        "file": target.name,
        "source": str(source),
        "bounds": [[south, west], [north, east]],
        "width": width,
        "height": height,
        "bytes": target.stat().st_size,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario-root", type=Path, default=DEFAULT_SCENARIO_ROOT)
    parser.add_argument("--style", type=Path, default=DEFAULT_STYLE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    stops, colors, opacity = parse_qml(args.style)
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = {
        "style": str(args.style), "opacity": opacity,
        "stops": [{"value": float(v), "rgb": c.astype(int).tolist()} for v, c in zip(stops, colors)],
        "rasters": {},
    }
    for key, (scenario, metric) in RASTERS.items():
        source = raster_path(args.scenario_root, scenario, metric)
        if not source.is_file():
            raise FileNotFoundError(source)
        flow, kind = key.split("/")
        target = args.output / f"avaSize_{flow}_{kind}.png"
        if target.exists() and not args.overwrite:
            raise FileExistsError(f"Output exists (pass --overwrite): {target}")
        manifest["rasters"][key] = export_png(source, target, stops, colors, opacity)
        print(f"Wrote: {target.name} ({target.stat().st_size / 1_048_576:.2f} MiB)")
    manifest_path = args.output / "avaSize_rasters.json"
    if manifest_path.exists() and not args.overwrite:
        raise FileExistsError(f"Output exists (pass --overwrite): {manifest_path}")
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    total = sum(item["bytes"] for item in manifest["rasters"].values())
    print(f"Prepared 4 styled raster overlays, {total / 1_048_576:.2f} MiB total")


if __name__ == "__main__":
    main()
