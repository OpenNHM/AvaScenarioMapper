#!/usr/bin/env python3
"""Create a compact publication figure from scenario statistics outputs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.patches import Rectangle


DEFAULT_DATA = Path(__file__).resolve().parents[1] / "analysis" / "scenario_statistics"
COL_DRY = "#385b70"
COL_WET = "#179b8d"
COL_TEXT = "#303030"
COL_LIGHT = "#eeeeee"
COL_BORDER = "#b8b8b8"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--statistics",
        type=Path,
        default=DEFAULT_DATA / "scenario_statistics.json",
    )
    parser.add_argument(
        "--destructive-roads",
        type=Path,
        default=DEFAULT_DATA / "scenario_road_exposure_by_destructive_size.csv",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_DATA / "scenario_basic_statistics")
    parser.add_argument("--show", action="store_true", help="Open an interactive plot window")
    return parser.parse_args()


def destructive_motor_road_km(data: pd.DataFrame, scenario: str) -> float:
    selected = data.loc[
        (data["scenario"] == scenario)
        & (data["destructiveSizeBand"].astype(str).isin(["≥3", ">=3"])),
        "motorRoadKm",
    ]
    if len(selected) != 1:
        raise ValueError(
            f"Expected exactly one destructive-size ≥3 row for {scenario}; found {len(selected)}"
        )
    return float(selected.iloc[0])


def main() -> None:
    args = parse_args()
    for path in (args.statistics, args.destructive_roads):
        if not path.is_file():
            raise FileNotFoundError(path)

    stats = json.loads(args.statistics.read_text(encoding="utf-8"))
    road_size = pd.read_csv(args.destructive_roads, encoding="utf-8")
    dry = stats["scenarios"]["dry_winter"]
    wet = stats["scenarios"]["wet_spring"]
    dry_destructive3 = destructive_motor_road_km(road_size, "dry_winter")
    wet_destructive3 = destructive_motor_road_km(road_size, "wet_spring")

    fig, ax = plt.subplots(figsize=(4.0, 4.8))
    fig.patch.set_facecolor("white")
    ax.set(xlim=(0, 1), ylim=(0, 1))
    ax.axis("off")

    ax.text(0.5, 0.965, "Basic Scenario Statistics", ha="center", va="top",
            fontsize=15, fontweight="bold", color=COL_TEXT)
    ax.text(0.5, 0.915, "Brenner Pass pilot region", ha="center", va="top",
            fontsize=9, color=COL_TEXT)
    ax.text(0.47, 0.845, "Dry Winter", ha="center", va="center", fontsize=11,
            fontweight="bold", color=COL_DRY)
    ax.text(0.81, 0.845, "Wet Spring", ha="center", va="center", fontsize=11,
            fontweight="bold", color=COL_WET)
    ax.plot([0.34, 0.60], [0.815, 0.815], lw=2, color=COL_DRY)
    ax.plot([0.68, 0.94], [0.815, 0.815], lw=2, color=COL_WET)

    def metric_row(
        y: float,
        label: str,
        dry_main: str,
        wet_main: str,
        dry_sub: str | None = None,
        wet_sub: str | None = None,
        dry_fraction: float | None = None,
        wet_fraction: float | None = None,
    ) -> None:
        ax.text(0.03, y, label, ha="left", va="center", fontsize=8,
                fontweight="bold", color=COL_TEXT)
        ax.text(0.47, y, dry_main, ha="center", va="center", fontsize=11,
                fontweight="bold", color=COL_DRY)
        ax.text(0.81, y, wet_main, ha="center", va="center", fontsize=11,
                fontweight="bold", color=COL_WET)
        if dry_sub is not None:
            ax.text(0.47, y - 0.037, dry_sub, ha="center", va="center",
                    fontsize=7, color=COL_TEXT)
        if wet_sub is not None:
            ax.text(0.81, y - 0.037, wet_sub, ha="center", va="center",
                    fontsize=7, color=COL_TEXT)
        if dry_fraction is not None and wet_fraction is not None:
            bar_y, bar_width, bar_height = y - 0.070, 0.25, 0.012
            for x, fraction, color in (
                (0.345, dry_fraction, COL_DRY), (0.685, wet_fraction, COL_WET)
            ):
                ax.add_patch(Rectangle((x, bar_y), bar_width, bar_height,
                                       facecolor=COL_LIGHT, edgecolor="none"))
                ax.add_patch(Rectangle((x, bar_y), bar_width * max(0, min(1, fraction)),
                                       bar_height, facecolor=color, edgecolor="none"))

    metric_row(
        0.74, "Selected PRAs", f'{dry["selectedPRAs"]:,}', f'{wet["selectedPRAs"]:,}',
        f'{dry["selectedPRAPercent"]:.1f}% of PRAs',
        f'{wet["selectedPRAPercent"]:.1f}% of PRAs',
        dry["selectedPRAPercent"] / 100, wet["selectedPRAPercent"] / 100,
    )
    metric_row(
        0.57, "Affected area", f'{dry["affectedAreaKm2"]:.1f} km²',
        f'{wet["affectedAreaKm2"]:.1f} km²',
        f'{dry["affectedStudyAreaPercent"]:.1f}% of study area',
        f'{wet["affectedStudyAreaPercent"]:.1f}% of study area',
        dry["affectedStudyAreaPercent"] / 100,
        wet["affectedStudyAreaPercent"] / 100,
    )
    metric_row(
        0.39, "Motor roads", f'{dry["exposedMotorRoadKm"]:.1f} km',
        f'{wet["exposedMotorRoadKm"]:.1f} km', "within affected area",
        "within affected area",
    )
    metric_row(
        0.23, "Destructive\nsize ≥3", f"{dry_destructive3:.1f} km",
        f"{wet_destructive3:.1f} km", "motor roads", "motor roads",
    )

    ax.plot([0.04, 0.96], [0.115, 0.115], lw=0.8, color=COL_BORDER)
    ax.text(
        0.5, 0.070,
        f'Study area: {stats["studyAreaKm2"]:.1f} km²   |   {stats["allPilotPRAs"]:,} PRAs',
        ha="center", va="center", fontsize=8, color=COL_TEXT,
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    svg = args.output.with_suffix(".svg")
    png = args.output.with_suffix(".png")
    fig.savefig(svg, bbox_inches="tight", transparent=True)
    fig.savefig(png, dpi=400, bbox_inches="tight", transparent=True)
    print(f"Wrote: {svg}")
    print(f"Wrote: {png}")
    if args.show:
        plt.show()
    else:
        plt.close(fig)


if __name__ == "__main__":
    main()
