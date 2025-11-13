# --------------------------- in2Matrix/avaPotMatrix.py --------------------------- #
#
# Purpose :
#   Defines the standardized matrix linking avalanche distribution potentials
#   ("very high", "high", "moderate", "low") to avalanche size potentials
#   (AvaSizePotential = scenario reference size class) and model output types
#   ("res", "rel", "res / rel").
#
# Context :
#   Avalanche Distribution–Size–modType Legend
#   Used by :
#     - com3AvaScenFilter/avaScenFilter.py → apply scenario legend filters
#     - runAvaScenMapper.py → Step 17 of the Avalanche Scenario Mapper
#
# Input  : None (matrix is defined in code for full reproducibility)
# Output : pandas.DataFrame (used during scenario filtering)
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


import pandas as pd


def avaPotMatrix() -> pd.DataFrame:
    """
    Build the standardized Avalanche Distribution–Size matrix.

    This matrix defines how the qualitative avalanche hazard level
    (AvaDistributionPotential) and the reference avalanche size scenario
    (AvaSizePotential) translate into valid combinations of potential and
    modelled avalanche mobility classes (PPM ↔ PEM) and geometry types
    ("rel", "res", "res / rel").

    Returns
    -------
    pd.DataFrame
        Structured table of all valid combinations including:
        AvaDistributionPotential, AvaSizePotential, PPM, PEM, rSize, modType

    Column definitions
    ------------------
    AvaDistributionPotential :
        Forecast / distribution-level hazard potential controlling
        how permissive the mapping matrix is.
        { "very high", "high", "moderate", "low" }

    AvaSizePotential :
        Reference avalanche size class (formerly PEM_header) defined
        by the scenario configuration; determines which simulation
        branch of the Avalanche Directory is used (2–5).

    PPM :
        Potential Path Mobility — represents release area size class
        derived from PRA segmentation (Step 06–07).

    PEM :
        Potential Event Mobility — represents avalanche event size class
        resulting from FlowPy simulation (Step 12–15).

    rSize :
        Relative size index derived from (PPM - PEM).
        Higher values indicate stronger agreement between potential and event.

    modType :
        Geometry mapping type:
          - "rel"       → release area geometry only
          - "res"       → result (runout) geometry only
          - "res / rel" → both linked for mapping
    """

    # --- Helper: compute relative size class difference ---
    def rsize_from(ppm: int, pem: int) -> int:
        d = int(ppm) - int(pem)
        return 5 if d == 0 else 4 if d == 1 else 3 if d == 2 else 2 if d == 3 else 1

    # --- Define matrix blocks (AvaDistributionPotential × AvaSizePotential) ---
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
        ("moderate", 3):  [(5,3,"res / rel"), (4,2,"res / rel"), (3,3,"rel"),       (2,2,"rel")],
        ("moderate", 2):  [(5,2,"res / rel"), (4,2,"res / rel"), (3,3,"rel"),       (2,2,"rel")],

        # LOW
        ("low", 2):       [(5,2,"res / rel"), (4,4,"rel"),       (3,3,"rel"),       (2,2,"rel")],
    }

    # --- Flatten all combinations ---
    rows = []
    for (dist_pot, size_pot), entries in blocks.items():
        for ppm, pem, mod in entries:
            rows.append({
                "AvaDistributionPotential": dist_pot,
                "AvaSizePotential": size_pot,
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
    for c in ["AvaSizePotential", "PPM", "PEM", "rSize"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").astype("Int64")

    df["AvaDistributionPotential"] = df["AvaDistributionPotential"].astype("string")
    df["modType"] = df["modType"].astype("string")

    return df
