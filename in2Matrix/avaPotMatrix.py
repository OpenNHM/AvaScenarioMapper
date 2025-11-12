# ------------------ in2Matrix/avaPotMatrix.py ------------------ #
# Step 17: Avalanche Potential–Size–modType Legend
#
# Purpose
# --------
# Defines the standardized matrix linking avalanche potential levels
# ("very high", "high", "moderat", "low") to avalanche size classes
# (PEM_header values) and model output types ("res", "rel", "res / rel").
#
# Used by:
#   - com3AvaScenFilter/avaScenFilter.py → to apply scenario legend filters
#   - runAvaScenMapper.py → Step 17 of the CAIROS Avalanche Mapper
#
# Input : None (matrix is defined in code for full reproducibility)
# Output: pandas.DataFrame (used in filtering)
#
# Author : CAIROS Project Team
# Version: 2025-11
# -------------------------------------------------------------------------

import pandas as pd


def makeAvaLegend() -> pd.DataFrame:
    """
    Build the standardized avalanche potential–size–modType matrix.

    Returns
    -------
    pd.DataFrame
        Table containing the logical combinations of:
        AvaPotential, PEM_header, PPM, PEM, rSize, modType
    """

    # --- Helper: compute relative size class difference ---
    def rsize_from(ppm: int, pem: int) -> int:
        d = int(ppm) - int(pem)
        return 5 if d == 0 else 4 if d == 1 else 3 if d == 2 else 2 if d == 3 else 1

    # --- Define matrix blocks (AvaPotential × PEM_header) ---
    blocks = {
        # VERY HIGH
        ("very high", 5): [(5,5,"res / rel"), (4,4,"res / rel"), (3,3,"res / rel"), (2,2,"res / rel")],
        ("very high", 4): [(5,4,"res / rel"), (4,4,"res / rel"), (3,3,"res / rel"), (2,2,"res / rel")],
        ("very high", 3): [(5,3,"res / rel"), (4,3,"res / rel"), (3,3,"res / rel"), (2,2,"res / rel")],
        ("very high", 2): [(5,2,"res / rel"), (4,2,"res / rel"), (3,2,"res / rel"), (2,2,"res / rel")],

        # HIGH
        ("high", 4):      [(5,4,"res / rel"), (4,3,"res / rel"), (3,2,"res / rel"), (2,2,"rel")],
        ("high", 3):      [(5,3,"res / rel"), (4,3,"res / rel"), (3,2,"res / rel"), (2,2,"rel")],
        ("high", 2):      [(5,2,"res / rel"), (4,2,"res / rel"), (3,2,"res / rel"), (2,2,"rel")],

        # MODERATE
        ("moderat", 3):   [(5,3,"res / rel"), (4,2,"res / rel"), (3,3,"rel"),       (2,2,"rel")],
        ("moderat", 2):   [(5,2,"res / rel"), (4,2,"res / rel"), (3,3,"rel"),       (2,2,"rel")],

        # LOW
        ("low", 2):       [(5,2,"res / rel"), (4,4,"rel"),       (3,3,"rel"),       (2,2,"rel")],
    }

    # --- Flatten all combinations ---
    rows = []
    for (pot, pem_header), entries in blocks.items():
        for ppm, pem, mod in entries:
            rows.append({
                "AvaPotential": pot,
                "PEM_header": pem_header,
                "PPM": ppm,
                "PEM": pem,
                "rSize": rsize_from(ppm, pem),
                "modType": mod,
                "Source": "in2Matrix.avaPotMatrix",
                "Version": "2025-11"
            })

    # --- Create DataFrame ---
    df = pd.DataFrame(rows)

    # --- Type conversions ---
    for c in ["PEM_header", "PPM", "PEM", "rSize"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").astype("Int64")

    df["AvaPotential"] = df["AvaPotential"].astype("string")
    df["modType"] = df["modType"].astype("string")

    return df
