"""
Pitcher Workload and Mechanics Monitor: data pipeline.

Steps
  1. Pull a season of Red Sox Statcast pitches from Baseball Savant (via pybaseball)
  2. Keep regular season pitches thrown by Red Sox pitchers, top N by pitch count
  3. Load pitchers and pitches into SQLite
  4. Run sql/outings.sql for pitch counts, days of rest and workload ratio
  5. Add role aware workload flags and mechanics drift flags
  6. Export CSVs for Tableau or Power BI

Usage
  python pipeline.py                      # 2025 season, top 10 pitchers
  python pipeline.py --season 2025 --pitchers 12
  python pipeline.py --refresh            # ignore the cached raw pull
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent
DATA = ROOT / "data"
EXPORTS = DATA / "exports"
DB_PATH = DATA / "workload.db"
SQL_PATH = ROOT / "sql" / "outings.sql"

TEAM = "BOS"
FASTBALLS = ["FF", "SI", "FC"]  # four seam, sinker, cutter

# Workload settings
STARTER_AVG_PITCHES = 50  # average pitches per outing at or above this = starter
SHORT_REST_DAYS = 3       # starter flagged at this many days of rest or fewer
RELIEVER_3_IN_3 = 3       # reliever flagged on his 3rd appearance in 3 days
RELIEVER_7D_APPS = 5      # reliever flagged at 5 or more appearances in 7 days
PITCH_COUNT_Z = 2.0       # any pitcher flagged at 2 SD above his own average pitch count
LAYOFF_DAYS = 14          # days of rest at or above this = a layoff
LAYOFF_RAMP_DAYS = 28     # workload ratio marked unreliable this long after a layoff

# Drift settings
MIN_FB_PITCHES = 5        # outing needs this many primary fastballs to count
MIN_OUTINGS_FOR_FLAGS = 8  # pitcher needs this many qualifying outings to get flags
VELO_Z = -2.0             # flag when outing velocity is 2 SD below season average
RELEASE_Z = 2.0           # flag when release height or side is 2 SD from season average

KEEP_COLS = [
    "game_date", "game_pk", "game_type", "pitcher", "p_throws", "pitch_type",
    "release_speed", "release_pos_x", "release_pos_z", "release_extension",
    "release_spin_rate", "inning", "inning_topbot", "at_bat_number",
    "pitch_number", "home_team", "away_team",
]


# 1. Pull ---------------------------------------------------------------------

def fetch_season(season: int, refresh: bool = False) -> pd.DataFrame:
    """Pull every pitch from games involving the team. Cached to a CSV."""
    raw_path = DATA / f"raw_statcast_{season}.csv"
    if raw_path.exists() and not refresh:
        print(f"Using cached pull: {raw_path}")
        return pd.read_csv(raw_path, low_memory=False)

    from pybaseball import cache, statcast  # imported here so tests run without it

    cache.enable()
    print(f"Pulling {season} Statcast data for {TEAM}. This can take several minutes.")
    raw = statcast(start_dt=f"{season}-03-15", end_dt=f"{season}-10-05", team=TEAM)
    raw = raw[[c for c in KEEP_COLS if c in raw.columns]]
    DATA.mkdir(exist_ok=True)
    raw.to_csv(raw_path, index=False)  # save right away so you never pull twice
    print(f"Saved {len(raw):,} rows to {raw_path}")
    return raw


# 2. Filter -------------------------------------------------------------------

def select_team_pitches(raw: pd.DataFrame, n_pitchers: int) -> pd.DataFrame:
    """Regular season pitches thrown by the team's pitchers, top N by volume."""
    df = raw.copy()
    if "game_type" in df.columns:
        df = df[df["game_type"] == "R"]

    # Home team pitches in the top of the inning, away team in the bottom.
    df["pitching_team"] = np.where(df["inning_topbot"] == "Top", df["home_team"], df["away_team"])
    df = df[df["pitching_team"] == TEAM]

    top = df["pitcher"].value_counts().head(n_pitchers).index
    df = df[df["pitcher"].isin(top)].copy()
    df["game_date"] = pd.to_datetime(df["game_date"]).dt.strftime("%Y-%m-%d")
    df = df.dropna(subset=["release_speed"])
    print(f"Kept {len(df):,} pitches from {df['pitcher'].nunique()} {TEAM} pitchers")
    return df


def lookup_names(ids: list[int]) -> dict[int, str]:
    """MLBAM id to 'First Last'. Falls back to the id if the lookup fails."""
    try:
        from pybaseball import playerid_reverse_lookup

        people = playerid_reverse_lookup(ids, key_type="mlbam")
        return {
            int(r.key_mlbam): f"{str(r.name_first).title()} {str(r.name_last).title()}"
            for r in people.itertuples()
        }
    except Exception as exc:  # network or package problem
        print(f"Name lookup failed ({exc}); using player ids instead")
        return {}


def build_pitchers(df: pd.DataFrame, names: dict[int, str]) -> pd.DataFrame:
    """One row per pitcher, with his most thrown fastball type."""
    fb = df[df["pitch_type"].isin(FASTBALLS)]
    primary = fb.groupby("pitcher")["pitch_type"].agg(lambda s: s.value_counts().idxmax())
    out = (
        df.groupby("pitcher")
        .agg(throws=("p_throws", "first"), total_pitches=("pitch_type", "size"))
        .join(primary.rename("primary_fastball"))
        .reset_index()
        .rename(columns={"pitcher": "pitcher_id"})
    )
    out["name"] = out["pitcher_id"].map(lambda i: names.get(int(i), f"Pitcher {i}"))
    return out[["pitcher_id", "name", "throws", "primary_fastball", "total_pitches"]]


# 3 and 4. Load and run SQL --------------------------------------------------

def load_and_transform(df: pd.DataFrame, pitchers: pd.DataFrame, db_path: Path) -> None:
    pitches = df.rename(columns={"pitcher": "pitcher_id"})[
        ["pitcher_id", "game_pk", "game_date", "inning", "at_bat_number", "pitch_number",
         "pitch_type", "release_speed", "release_pos_x", "release_pos_z",
         "release_extension", "release_spin_rate"]
    ]
    db_path.parent.mkdir(exist_ok=True)
    with sqlite3.connect(db_path) as con:
        pitchers.to_sql("pitchers", con, if_exists="replace", index=False)
        pitches.to_sql("pitches", con, if_exists="replace", index=False)
        con.execute("CREATE INDEX IF NOT EXISTS ix_pitches_pitcher ON pitches(pitcher_id, game_date)")
        con.executescript(SQL_PATH.read_text())
        n = con.execute("SELECT COUNT(*) FROM outings").fetchone()[0]
    print(f"Built {n:,} outings in {db_path}")


# 5. Workload and mechanics flags -------------------------------------------

def add_workload_flags(outings: pd.DataFrame) -> pd.DataFrame:
    """Role aware workload flags, plus layoff handling for the workload ratio.

    The acute to chronic ratio is kept for context, but not used as a flag: with
    game pitches only, a starter on a normal five day rotation has two starts in
    most 7 day windows, so the ratio sits near 1.5 on a healthy schedule.
    """
    o = outings.sort_values(["pitcher_id", "game_date", "game_pk"]).copy()
    o["game_date"] = pd.to_datetime(o["game_date"])

    avg_pitches = o.groupby("pitcher_id")["pitches"].transform("mean")
    o["role"] = np.where(avg_pitches >= STARTER_AVG_PITCHES, "Starter", "Reliever")
    starter = o["role"] == "Starter"

    sd = o.groupby("pitcher_id")["pitches"].transform("std")
    o["z_pitches"] = ((o["pitches"] - avg_pitches) / sd).round(2)

    o["short_rest_flag"] = (starter & (o["days_rest"] <= SHORT_REST_DAYS)).astype(int)
    o["reliever_usage_flag"] = (
        ~starter & ((o["appearances_3d"] >= RELIEVER_3_IN_3) | (o["appearances_7d"] >= RELIEVER_7D_APPS))
    ).astype(int)
    o["pitch_count_flag"] = (o["z_pitches"] >= PITCH_COUNT_Z).astype(int)
    o["high_load_flag"] = o[["short_rest_flag", "reliever_usage_flag", "pitch_count_flag"]].max(axis=1)

    # After a layoff (injured list, minors), his rehab ramp-up isn't in the data,
    # so the ratio would show a false spike. Mark it unreliable for a while.
    o["returning_from_layoff"] = 0
    for _, grp in o.groupby("pitcher_id"):
        returns = grp.loc[grp["days_rest"] >= LAYOFF_DAYS, "game_date"]
        for d in returns:
            window = (grp["game_date"] >= d) & (grp["game_date"] < d + pd.Timedelta(days=LAYOFF_RAMP_DAYS))
            o.loc[grp.index[window], "returning_from_layoff"] = 1
    o.loc[o["returning_from_layoff"] == 1, "acwr_reliable"] = 0

    o["game_date"] = o["game_date"].dt.strftime("%Y-%m-%d")
    return o


def add_drift_flags(outings: pd.DataFrame) -> pd.DataFrame:
    """Compare each outing's fastball to the pitcher's own season range."""
    outings = outings.copy()
    metrics = {"fb_velo": "z_velo", "fb_release_x": "z_release_x", "fb_release_z": "z_release_z"}
    for z in metrics.values():
        outings[z] = np.nan

    qualifies = outings["fb_pitches"] >= MIN_FB_PITCHES
    for _, grp in outings[qualifies].groupby("pitcher_id"):
        if len(grp) < MIN_OUTINGS_FOR_FLAGS:
            continue
        for col, z in metrics.items():
            sd = grp[col].std()
            if sd and sd > 0:
                outings.loc[grp.index, z] = ((grp[col] - grp[col].mean()) / sd).round(2)

    outings["velo_drop_flag"] = (outings["z_velo"] <= VELO_Z).astype(int)
    outings["release_drift_flag"] = (
        (outings["z_release_x"].abs() >= RELEASE_Z) | (outings["z_release_z"].abs() >= RELEASE_Z)
    ).astype(int)
    outings["mechanics_flag"] = outings[["velo_drop_flag", "release_drift_flag"]].max(axis=1)
    return outings


def add_flags(db_path: Path) -> pd.DataFrame:
    with sqlite3.connect(db_path) as con:
        outings = pd.read_sql("SELECT * FROM outings", con)
    outings = add_drift_flags(add_workload_flags(outings))
    outings["any_flag"] = outings[["mechanics_flag", "high_load_flag"]].max(axis=1)
    with sqlite3.connect(db_path) as con:
        outings.to_sql("outings", con, if_exists="replace", index=False)
    return outings


# 6. Export -------------------------------------------------------------------

def export_for_bi(db_path: Path) -> None:
    """Flat files for Tableau Public or Power BI."""
    EXPORTS.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as con:
        outings = pd.read_sql(
            """
            SELECT pr.name, pr.throws, pr.primary_fastball, o.*
            FROM outings o JOIN pitchers pr USING (pitcher_id)
            ORDER BY pr.name, o.game_date
            """,
            con,
        )
        staff = pd.read_sql(
            """
            SELECT pr.name, pr.throws, pr.primary_fastball,
                   COUNT(*)                         AS outings,
                   SUM(o.pitches)                   AS total_pitches,
                   ROUND(AVG(o.pitches), 1)         AS avg_pitches_per_outing,
                   ROUND(AVG(o.days_rest), 1)       AS avg_days_rest,
                   MAX(CASE WHEN o.acwr_reliable = 1 THEN o.acwr END) AS peak_acwr,
                   SUM(o.high_load_flag)            AS high_load_outings,
                   SUM(o.short_rest_flag)           AS short_rest_outings,
                   SUM(o.reliever_usage_flag)       AS heavy_usage_outings,
                   SUM(o.pitch_count_flag)          AS high_pitch_count_outings,
                   SUM(o.velo_drop_flag)            AS velo_drop_outings,
                   SUM(o.release_drift_flag)        AS release_drift_outings,
                   ROUND(AVG(o.fb_velo), 1)         AS avg_fb_velo,
                   MAX(o.role)                      AS role
            FROM outings o JOIN pitchers pr USING (pitcher_id)
            GROUP BY pr.pitcher_id
            ORDER BY total_pitches DESC
            """,
            con,
        )
    outings.to_csv(EXPORTS / "outings.csv", index=False)
    staff.to_csv(EXPORTS / "staff_summary.csv", index=False)
    print(f"Exported {len(outings):,} outings and {len(staff)} pitchers to {EXPORTS}")


def summarize(outings: pd.DataFrame) -> None:
    """Numbers for the resume bullet."""
    with sqlite3.connect(DB_PATH) as con:
        n_pitches = con.execute("SELECT COUNT(*) FROM pitches").fetchone()[0]
    print("\nResume numbers")
    print(f"  Pitches loaded:          {n_pitches:,}")
    print(f"  Pitchers:                {outings['pitcher_id'].nunique()}")
    print(f"  Outings:                 {len(outings):,}")
    print(f"  High workload outings:   {int(outings['high_load_flag'].sum())}")
    print(f"    short rest (starters): {int(outings['short_rest_flag'].sum())}")
    print(f"    heavy usage (relievers): {int(outings['reliever_usage_flag'].sum())}")
    print(f"    high pitch count:      {int(outings['pitch_count_flag'].sum())}")
    print(f"  Velocity drop outings:   {int(outings['velo_drop_flag'].sum())}")
    print(f"  Release drift outings:   {int(outings['release_drift_flag'].sum())}")
    print(f"  Outings with any flag:   {int(outings['any_flag'].sum())}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--season", type=int, default=2025)
    parser.add_argument("--pitchers", type=int, default=10, help="top N pitchers by pitch count")
    parser.add_argument("--refresh", action="store_true", help="pull again instead of using the cache")
    args = parser.parse_args()

    raw = fetch_season(args.season, refresh=args.refresh)
    df = select_team_pitches(raw, args.pitchers)
    names = lookup_names(sorted(df["pitcher"].unique().tolist()))
    pitchers = build_pitchers(df, names)
    load_and_transform(df, pitchers, DB_PATH)
    outings = add_flags(DB_PATH)
    export_for_bi(DB_PATH)
    summarize(outings)


if __name__ == "__main__":
    main()
