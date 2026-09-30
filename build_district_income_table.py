"""
Build a congressional-district income distribution lookup table.

Source data: https://github.com/kchanwong/tax_max_elim_map
  - synthetic_puma_all_earners.csv.gz  worker-level 2024 wage records by PUMA
  - geocorr2022_2619606869.csv         PUMA -> 119th Congress district crosswalk
  - TAX_MAX_POP.csv                    Cato's district output, used for validation

Input template: 119th_districts_income_distribution.csv
  Columns A-G (map, congress, geoid, district, state_fips, provisional, income)
  are kept exactly as they are. Columns H-M are filled in:
    workers_per_log10   workers per unit of log10(wage) near the row's income
    earnings_per_log10  wage dollars per unit of log10(wage) near the row's income
    share_above         share of the district's workers with wage >= income
    earnings_above      dollars of wages above income: sum of max(wage - income, 0)
    workers_total       total workers in the district
    earnings_total      total wages in the district

Usage:
  python build_district_income_table.py \
      --template 119th_districts_income_distribution.csv \
      --out 119th_districts_income_distribution_filled.csv \
      [--repo tax_max_elim_map] [--chart data-kaCYF.csv]
"""

import argparse
import csv
import os
import subprocess

import numpy as np
import pandas as pd

REPO_URL = "https://github.com/kchanwong/tax_max_elim_map.git"

# States whose congressional lines changed for the 2026 elections, by FIPS code.
# Source: NCSL "Changing the Maps: Tracking Mid-Decade Redistricting", updated Sep 2, 2026.
# AL, CA, FL, LA, MO, NC, OH, TN, TX, UT. The repo has no crosswalk for the new
# lines, so these districts are left blank on the 2026 map.
REDRAWN_FOR_2026 = {1, 6, 12, 22, 29, 37, 39, 47, 48, 49}

TAX_MAX_2024 = 168_600  # threshold used in Cato's TAX_MAX_POP.csv, for validation
DC_DELEGATE_CD = 98     # geocorr codes DC's non-voting seat as 98; not in the template


# ---------------------------------------------------------------------------
# 1. Get the repo
# ---------------------------------------------------------------------------
def get_repo(path):
    """Clone the repo if it is not already on disk. (The GitHub API is
    rate-limited for anonymous use, so a git clone is more reliable.)"""
    if not os.path.isdir(path):
        subprocess.run(["git", "clone", "--depth", "1", REPO_URL, path], check=True)
    return path


# ---------------------------------------------------------------------------
# 2. Load earners and assign them to districts
# ---------------------------------------------------------------------------
def load_district_earners(repo):
    earners = pd.read_csv(
        os.path.join(repo, "synthetic_puma_all_earners.csv.gz"),
        usecols=["PUMA", "STATEFIP", "INCWAGE", "PERWT"],
    )

    xwalk = pd.read_csv(os.path.join(repo, "geocorr2022_2619606869.csv"), dtype=str)
    xwalk = pd.DataFrame({
        "PUMA": xwalk["puma22"].astype(int),
        "STATEFIP": xwalk["state"].astype(int),
        "CD": xwalk["cd119"].astype(int),
        "afact": xwalk["afact"].astype(float),
    })

    # Join on PUMA and state together, since PUMA codes repeat across states.
    # A PUMA split across districts produces one row per district.
    df = earners.merge(xwalk, on=["PUMA", "STATEFIP"], how="left", validate="many_to_many")
    unmatched = df["CD"].isna().sum()
    if unmatched:
        raise ValueError(f"{unmatched} earner records did not match the crosswalk")

    df = df[df["CD"] != DC_DELEGATE_CD].copy()
    df["geoid"] = df["STATEFIP"] * 100 + df["CD"].astype(int)
    df["weight"] = df["PERWT"] * df["afact"]          # workers this row represents
    df["wages"] = df["weight"] * df["INCWAGE"]        # wage dollars this row represents
    return df[["geoid", "INCWAGE", "weight", "wages"]]


# ---------------------------------------------------------------------------
# 3. Compute the distribution for each district on the template's income grid
# ---------------------------------------------------------------------------
def build_grid(incomes):
    """Return the sorted income grid, the log10 bin edges between grid points,
    and the bin width. The template's grid is evenly spaced in log10."""
    grid = np.sort(np.unique(incomes))
    # The template's incomes are rounded, so rebuild the exact log10 grid from
    # its endpoints ($1,000 and $100 billion) and check the template agrees.
    logs = np.linspace(np.log10(grid[0]), np.log10(grid[-1]), len(grid))
    if not np.allclose(np.log10(grid), logs, atol=1e-4):
        raise ValueError("Income grid is not evenly spaced in log10")
    width = logs[1] - logs[0]
    edges = (logs[:-1] + logs[1:]) / 2   # midpoints between neighbouring grid points
    return grid, edges, width


def district_tables(df, grid, edges, width):
    """For each district, return a DataFrame indexed by grid income with the six columns."""
    df = df.copy()
    # Nearest grid point on the log10 scale.
    df["bin"] = np.searchsorted(edges, np.log10(df["INCWAGE"]), side="right")

    results = {}
    for geoid, d in df.groupby("geoid"):
        n_workers = d["weight"].sum()
        n_wages = d["wages"].sum()

        # Densities: total in each bin divided by the bin width.
        workers_bin = np.bincount(d["bin"], weights=d["weight"], minlength=len(grid))
        wages_bin = np.bincount(d["bin"], weights=d["wages"], minlength=len(grid))

        # Counts at or above each threshold, using cumulative sums from the top.
        d = d.sort_values("INCWAGE")
        wage = d["INCWAGE"].to_numpy()
        w_ge = np.append(np.cumsum(d["weight"].to_numpy()[::-1])[::-1], 0.0)
        e_ge = np.append(np.cumsum(d["wages"].to_numpy()[::-1])[::-1], 0.0)
        idx = np.searchsorted(wage, grid, side="left")   # first earner with wage >= x
        workers_above = w_ge[idx]
        wages_above = e_ge[idx]

        results[geoid] = pd.DataFrame({
            "workers_per_log10": workers_bin / width,
            "earnings_per_log10": wages_bin / width,
            "share_above": workers_above / n_workers,
            "earnings_above": wages_above - grid * workers_above,
            "workers_total": n_workers,
            "earnings_total": n_wages,
        }, index=grid)
    return results


# ---------------------------------------------------------------------------
# 4. Write the filled template
# ---------------------------------------------------------------------------
def fill_template(template_path, out_path, tables, grid):
    # Read columns A-G as raw text so formatting (leading zeros etc.) is unchanged.
    fmt = lambda v: "%.10g" % v
    n_filled = n_blank = 0
    with open(template_path, newline="", encoding="utf-8-sig") as fin, \
         open(out_path, "w", newline="") as fout:
        reader, writer = csv.reader(fin), csv.writer(fout, lineterminator="\r\n")
        writer.writerow(next(reader))
        for row in reader:
            keep = row[:7]
            map_id, geoid, state_fips, income = keep[0], int(keep[2]), int(keep[4]), float(keep[6])

            if map_id == "2026" and state_fips in REDRAWN_FOR_2026:
                writer.writerow(keep + [""] * 6)
                n_blank += 1
                continue

            # Look up the grid point for this row's income.
            g = grid[np.argmin(np.abs(np.log10(grid) - np.log10(income)))]
            values = tables[geoid].loc[g]
            writer.writerow(keep + [fmt(v) for v in values])
            n_filled += 1
    print(f"Wrote {out_path}: {n_filled:,} rows filled, {n_blank:,} rows left blank")


# ---------------------------------------------------------------------------
# 5. Validation
# ---------------------------------------------------------------------------
def share_at(df, threshold):
    """Workers and share at or above a threshold, by district, from the earner records."""
    above = df[df["INCWAGE"] >= threshold].groupby("geoid")["weight"].sum()
    total = df.groupby("geoid")["weight"].sum()
    return pd.DataFrame({"total": total, "above": above.reindex(total.index, fill_value=0)})


def validate(df, tables, repo, chart_path=None):
    s = share_at(df, TAX_MAX_2024)

    # Check against Cato's TAX_MAX_POP.csv (TOTAL_POP and TOTAL_TAXMAX_POP are rounded counts).
    cato = pd.read_csv(os.path.join(repo, "TAX_MAX_POP.csv"))
    cato["geoid"] = cato["STATEFIP"] * 100 + cato["CD"]
    cato = cato.set_index("geoid").reindex(s.index)
    ok = ((s["total"].round() == cato["TOTAL_POP"]) &
          (s["above"].round() == cato["TOTAL_TAXMAX_POP"]))
    print(f"TAX_MAX_POP.csv: {ok.sum()} of {len(ok)} districts match exactly")

    # Check against the Datawrapper chart data (TAX_RAISE = % of workers at or above $168,600).
    if chart_path:
        chart = pd.read_csv(chart_path, encoding="utf-8-sig").set_index("GEOID")
        pct = 100 * s["above"] / s["total"]
        gap = (pct - chart["TAX_RAISE"].reindex(pct.index)).abs()
        print(f"Datawrapper chart: {len(gap.dropna())} districts compared, "
              f"largest gap {gap.max():.6f} percentage points")

    # Internal check: densities integrate back to the totals.
    for geoid, t in tables.items():
        width = np.diff(np.log10(t.index)).mean()
        assert np.isclose(t["workers_per_log10"].sum() * width, t["workers_total"].iloc[0])
        assert np.isclose(t["earnings_per_log10"].sum() * width, t["earnings_total"].iloc[0])
    print("Densities sum back to district totals in every district")


# ---------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--template", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--repo", default="tax_max_elim_map")
    p.add_argument("--chart", default=None, help="optional data-kaCYF.csv from Datawrapper")
    a = p.parse_args()

    repo = get_repo(a.repo)
    df = load_district_earners(repo)

    incomes = pd.read_csv(a.template, usecols=["income"])["income"].to_numpy()
    grid, edges, width = build_grid(incomes)
    tables = district_tables(df, grid, edges, width)
    print(f"Computed distributions for {len(tables)} districts on a {len(grid)}-point grid")

    fill_template(a.template, a.out, tables, grid)
    validate(df, tables, repo, a.chart)


if __name__ == "__main__":
    main()
