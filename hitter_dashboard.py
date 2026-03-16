#!/usr/bin/env python3
"""
Hitter Dashboard Builder

Aggregates MLB hitter data from multiple sources into a single CSV:
  - Baseball Savant: xBA, barrel rate, hard hit rate, avg bat speed, exit velo buckets
  - FanGraphs CSV: BatX auction values (manual export from auction calculator)
  - EVAnalytics: Derek Carty's context-neutral wOBA ranking
  - SFBB Player ID Map: Position data

Usage:
    python hitter_dashboard.py
    python hitter_dashboard.py --season 2025 --fg-csv fangraphs_export.csv
    python hitter_dashboard.py --min-pa 100 --output my_dashboard.csv
    python hitter_dashboard.py --skip-exit-velo --skip-date-range  # faster run
"""

import argparse
import base64
import os
import sqlite3
import time
from io import StringIO

import unicodedata

import numpy as np
import pandas as pd
import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

DEFAULT_SEASON = 2025
DEFAULT_DATE_RANGE_START = "2025-08-01"
DEFAULT_DATE_RANGE_END = "2025-10-01"
DEFAULT_MIN_PA = 50
DEFAULT_OUTPUT = "hitter_dashboard.csv"

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SFBB_MAP_PATH = os.path.join(_BASE_DIR, "SFBB Player ID Map - PLAYERIDMAP.csv")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def fetch_url(url, method="get", data=None, extra_headers=None,
              max_retries=3, delay=2):
    """HTTP request with retry logic."""
    hdrs = {**HTTP_HEADERS, **(extra_headers or {})}
    for attempt in range(max_retries):
        try:
            if method == "post":
                resp = requests.post(
                    url, data=data, headers=hdrs, timeout=90
                )
            else:
                resp = requests.get(
                    url, headers=hdrs, timeout=90
                )
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            if attempt < max_retries - 1:
                wait = delay * (attempt + 1)
                print(
                    f"  Retry {attempt + 1}/{max_retries} "
                    f"in {wait}s: {e}"
                )
                time.sleep(wait)
            else:
                raise
    return None


def normalize_name(name: str) -> str:
    """'Last, First' -> 'first last' (lowercased, no punctuation)."""
    if not isinstance(name, str):
        return ""
    name = name.strip()
    if "," in name:
        parts = [p.strip() for p in name.split(",", 1)]
        name = f"{parts[1]} {parts[0]}"
    name = unicodedata.normalize("NFD", name.lower())
    name = "".join(
        c for c in name if unicodedata.category(c) != "Mn"
    )
    return (name.replace(".", "").replace("'", "")
            .replace("-", " ").strip())


def _get_db_path(season):
    """Return path to the statcast SQLite DB for a season."""
    return os.path.join(_BASE_DIR, "data", f"statcast_{season}.db")


def _db_exists(season):
    """Check if the statcast DB exists for a season."""
    return os.path.exists(_get_db_path(season))


# =========================================================================
# BASEBALL SAVANT - Custom Leaderboard (pre-aggregated season stats)
# =========================================================================


def fetch_savant_season_stats(season, min_pa=1, log=print):
    """
    Season stats from the Savant custom leaderboard.
    Returns: MLBAMID, Name, PA, xBA, Barrel%, HardHit%, K%.

    Note: HR and AvgBatSpeed come from other sources (DB / bat-tracking).
    The custom leaderboard's 'hrs' and 'bat_speed' columns are empty.
    """
    log("  [Savant] Custom leaderboard (season stats)...")
    selections = (
        "xba,barrel_batted_rate,hard_hit_percent,"
        "k_percent,pa"
    )
    url = (
        f"https://baseballsavant.mlb.com/leaderboard/custom"
        f"?year={season}&type=batter&filter=&min={min_pa}"
        f"&selections={selections}&chart=false&csv=true"
    )
    resp = fetch_url(url)
    df = pd.read_csv(StringIO(resp.text))

    out = pd.DataFrame()
    out["MLBAMID"] = df["player_id"]
    out["Name"] = df["last_name, first_name"]
    out["PA"] = pd.to_numeric(df["pa"], errors="coerce")
    out["xBA"] = pd.to_numeric(df["xba"], errors="coerce")
    out["Barrel%"] = pd.to_numeric(
        df["barrel_batted_rate"], errors="coerce"
    )
    out["HardHit%"] = pd.to_numeric(
        df["hard_hit_percent"], errors="coerce"
    )
    out["K%"] = pd.to_numeric(
        df["k_percent"], errors="coerce"
    )

    log(f"    {len(out)} players from custom leaderboard.")
    return out


# =========================================================================
# BASEBALL SAVANT - Bat Tracking (competitive swing avg bat speed)
# =========================================================================


def fetch_savant_bat_speed(season, log=print):
    """
    Avg bat speed from the Savant bat-tracking leaderboard.
    This already filters to competitive swings (top 90% of swings
    + slower swings with 90+ mph exit velo).
    """
    log("  [Savant] Bat tracking (competitive avg bat speed)...")
    url = (
        f"https://baseballsavant.mlb.com/leaderboard/bat-tracking"
        f"?gameType=Regular&minSwings=100&minGroupSwings=1"
        f"&seasonStart={season}&seasonEnd={season}"
        f"&type=batter&csv=true"
    )
    resp = fetch_url(url)
    df = pd.read_csv(StringIO(resp.text))
    out = df[["id", "name", "avg_bat_speed"]].copy()
    out.columns = ["MLBAMID", "Name", "AvgBatSpeed"]
    out["AvgBatSpeed"] = np.round(
        pd.to_numeric(out["AvgBatSpeed"], errors="coerce")
        .values.astype("float64"), 1
    )
    log(f"    {len(out)} players with bat speed data.")
    return out


# =========================================================================
# HR + DR_ STATS from SQLite DB
# =========================================================================


def _compute_hr_from_db(season, log=print):
    """Compute season HR from the local pitch database."""
    db_path = _get_db_path(season)
    if not os.path.exists(db_path):
        log("    No local DB for HR computation.")
        return pd.DataFrame(columns=["MLBAMID", "HR"])

    log("  [DB] Computing HR from local database...")
    conn = sqlite3.connect(db_path)
    query = """
        SELECT CAST(batter AS INTEGER) AS MLBAMID,
               COUNT(*) AS HR
        FROM pitches
        WHERE events = 'home_run'
        GROUP BY batter
    """
    df = pd.read_sql_query(query, conn)
    conn.close()
    log(f"    {len(df)} batters with home runs.")
    return df


def _dr_stats_from_db(season, start_date, end_date, log=print):
    """Compute date-range stats from local pitch database.

    xBA = SUM(estimated_ba) / at_bats (not AVG over batted balls).
    DR_AvgBatSpeed uses competitive swings only.
    """
    db_path = _get_db_path(season)
    if not os.path.exists(db_path):
        return None

    log(f"  [DB] Computing date-range stats from local DB "
        f"({start_date} to {end_date})...")
    conn = sqlite3.connect(db_path)

    # Core stats (PA, HR, K%, Barrel%, HardHit%, xBA)
    query = """
        WITH pa_events AS (
            SELECT batter, events,
                   CAST(launch_speed AS REAL) AS ls,
                   CAST(launch_speed_angle AS REAL) AS lsa,
                   CASE WHEN estimated_ba_using_speedangle
                            IS NOT NULL
                        AND estimated_ba_using_speedangle != ''
                   THEN CAST(
                       estimated_ba_using_speedangle AS REAL)
                   ELSE NULL END AS xba,
                   type
            FROM pitches
            WHERE events IS NOT NULL
              AND events != ''
              AND game_date >= ?
              AND game_date <= ?
        )
        SELECT
            CAST(batter AS INTEGER) AS MLBAMID,
            COUNT(*) AS DR_PA,
            SUM(CASE WHEN events = 'home_run'
                THEN 1 ELSE 0 END) AS DR_HR,
            ROUND(100.0 * SUM(CASE
                WHEN events IN ('strikeout',
                    'strikeout_double_play')
                THEN 1 ELSE 0 END) / COUNT(*), 1)
                AS "DR_K%",
            ROUND(
                SUM(CASE WHEN type = 'X' AND xba IS NOT NULL
                    THEN xba ELSE 0 END)
                / NULLIF(SUM(CASE
                    WHEN events NOT LIKE '%walk%'
                     AND events != 'hit_by_pitch'
                     AND events NOT LIKE '%sac%'
                     AND events != 'catcher_interf'
                    THEN 1 ELSE 0 END), 0),
                3) AS DR_xBA,
            ROUND(100.0 * SUM(CASE
                WHEN type = 'X' AND lsa = 6
                THEN 1 ELSE 0 END)
                / NULLIF(SUM(CASE
                    WHEN type = 'X' THEN 1 ELSE 0
                    END), 0), 1)
                AS "DR_Barrel%",
            ROUND(100.0 * SUM(CASE
                WHEN type = 'X' AND ls >= 95
                THEN 1 ELSE 0 END)
                / NULLIF(SUM(CASE
                    WHEN type = 'X' THEN 1 ELSE 0
                    END), 0), 1)
                AS "DR_HardHit%"
        FROM pa_events
        GROUP BY batter
    """
    df = pd.read_sql_query(query, conn, params=[
        start_date, end_date
    ])

    # Competitive-swing avg bat speed per batter
    bs_df = _competitive_bat_speed_from_db(
        conn, start_date, end_date, log
    )
    if bs_df is not None and not bs_df.empty:
        df = df.merge(bs_df, on="MLBAMID", how="left")

    conn.close()
    log(f"    {len(df)} batters in date range.")
    return df


def _competitive_bat_speed_from_db(conn, start_date, end_date,
                                   log=print):
    """Compute competitive-swing avg bat speed from DB.

    Competitive swings = top 90% of a batter's swings by bat speed
    + any slower swing that produced 90+ mph exit velocity.
    """

    query = """
        SELECT CAST(batter AS INTEGER) AS MLBAMID,
               CAST(bat_speed AS REAL) AS bs,
               CAST(launch_speed AS REAL) AS ev
        FROM pitches
        WHERE bat_speed IS NOT NULL AND bat_speed != ''
          AND game_date >= ? AND game_date <= ?
    """
    raw = pd.read_sql_query(query, conn, params=[
        start_date, end_date
    ])
    if raw.empty:
        return None

    rows = []
    for batter_id, grp in raw.groupby("MLBAMID"):
        speeds = grp["bs"].values
        if len(speeds) < 5:
            continue
        threshold = np.percentile(speeds, 10)
        mask = (
            (grp["bs"] >= threshold)
            | (grp["ev"].fillna(0) >= 90)
        )
        comp = grp.loc[mask, "bs"]
        if len(comp) > 0:
            rows.append({
                "MLBAMID": batter_id,
                "DR_AvgBatSpeed": round(comp.mean(), 1),
            })

    if not rows:
        return None
    log(f"    {len(rows)} batters with DR bat speed.")
    return pd.DataFrame(rows)


# =========================================================================
# BASEBALL SAVANT - Statcast Search (pitch-level data)
# =========================================================================


def _statcast_search_csv(season, start_date="", end_date="",
                         extra_params=""):
    """Query the Statcast search CSV endpoint for individual events."""
    url = (
        f"https://baseballsavant.mlb.com/statcast_search/csv"
        f"?all=true"
        f"&hfPT=&hfAB=&hfGT=R%7C&hfPR=&hfZ=&hfBBL="
        f"&hfNewZones=&hfPull=&hfC="
        f"&hfSea={season}%7C"
        f"&hfSit=&player_type=batter&hfOuts=&hfOpponent="
        f"&pitcher_throws=&batter_stands=&hfSA="
        f"&game_date_gt={start_date}&game_date_lt={end_date}"
        f"&hfMo=&hfTeam=&home_road=&hfRO=&hfInn="
        f"&min_pitches=0&min_results=0"
        f"&sort_col=pitches&player_event_sort=h_launch_speed"
        f"&sort_order=desc&min_pas=0&type=details"
        f"{extra_params}"
    )
    resp = fetch_url(url)
    if resp is None or not resp.text.strip():
        return pd.DataFrame()
    try:
        return pd.read_csv(StringIO(resp.text), low_memory=False)
    except Exception:
        return pd.DataFrame()


# =========================================================================
# EXIT VELOCITY BUCKETS
# =========================================================================


def _ev_buckets_from_db(db_path, log=print):
    """Query EV buckets from local SQLite database.

    Only counts batted ball events (type = 'X') to match Savant.
    """
    log(f"  [DB] Querying exit-velocity buckets from "
        f"{db_path}...")
    conn = sqlite3.connect(db_path)
    query = """
        SELECT CAST(batter AS INTEGER) AS MLBAMID,
            SUM(CASE
                WHEN CAST(launch_speed AS REAL) >= 105
                 AND CAST(launch_speed AS REAL) < 110
                THEN 1 ELSE 0 END) AS EV_105_110,
            SUM(CASE
                WHEN CAST(launch_speed AS REAL) >= 110
                 AND CAST(launch_speed AS REAL) < 115
                THEN 1 ELSE 0 END) AS EV_110_115,
            SUM(CASE
                WHEN CAST(launch_speed AS REAL) >= 115
                THEN 1 ELSE 0 END) AS "EV_115+"
        FROM pitches
        WHERE CAST(launch_speed AS REAL) >= 105
          AND type = 'X'
        GROUP BY batter
    """
    df = pd.read_sql_query(query, conn)
    conn.close()
    log(f"    {len(df)} batters with 105+ mph batted balls.")
    return df


def _ev_buckets_from_savant(season, log=print):
    """Fetch EV buckets via HTTP (slow fallback)."""
    log("  [Savant] Exit velocity buckets via HTTP "
        "(105-110, 110-115, 115+)...")
    log("  Tip: run 'python build_statcast_db.py "
        f"--season {season}' for instant queries.")

    import datetime as _dt
    d_start = _dt.date(season, 3, 20)
    d_end = min(
        _dt.date(season, 10, 15),
        _dt.date.today(),
    )

    frames = []
    current = d_start
    while current <= d_end:
        date_str = current.isoformat()
        log(f"    {date_str}...", flush=True)
        try:
            df = _statcast_search_csv(
                season, date_str, date_str
            )
            if df.empty or "launch_speed" not in df.columns:
                current += _dt.timedelta(days=1)
                time.sleep(1.5)
                continue
            # Only batted ball events (type = 'X')
            batted = df[df["type"] == "X"] if "type" in df.columns else df
            fast = batted[batted["launch_speed"] >= 105][
                ["batter", "player_name", "launch_speed"]
            ].copy()
            if not fast.empty:
                log(f"    {len(fast)} batted balls >= 105 mph")
                frames.append(fast)
        except Exception as e:
            log(f"    error: {e}")
        current += _dt.timedelta(days=1)
        time.sleep(1.5)

    empty = pd.DataFrame(
        columns=["MLBAMID", "EV_105_110",
                 "EV_110_115", "EV_115+"]
    )
    if not frames:
        log("    Warning: no exit-velocity data.")
        return empty

    events = pd.concat(frames, ignore_index=True)

    def _bucket(speed):
        if speed >= 115:
            return "EV_115+"
        elif speed >= 110:
            return "EV_110_115"
        return "EV_105_110"

    events["bucket"] = events["launch_speed"].apply(_bucket)
    pivot = (
        events.groupby(["batter", "bucket"])
        .size()
        .unstack(fill_value=0)
        .reset_index()
        .rename(columns={"batter": "MLBAMID"})
    )
    for col in ["EV_105_110", "EV_110_115", "EV_115+"]:
        if col not in pivot.columns:
            pivot[col] = 0
    return pivot[["MLBAMID", "EV_105_110",
                  "EV_110_115", "EV_115+"]]


def fetch_exit_velo_buckets(season, log=print):
    """EV buckets: uses local DB if available, else HTTP."""
    db_path = _get_db_path(season)
    if os.path.exists(db_path):
        return _ev_buckets_from_db(db_path, log)
    return _ev_buckets_from_savant(season, log)


# =========================================================================
# DATE-RANGE STATS
# =========================================================================


def fetch_savant_date_range_stats(season, start_date, end_date,
                                  log=print):
    """
    Date-range stats. Uses local DB if available (accurate),
    otherwise falls back to day-by-day HTTP fetch.
    """
    log(f"  [Savant] Date-range stats "
        f"({start_date} to {end_date})...")

    empty = pd.DataFrame(columns=[
        "MLBAMID", "DR_PA", "DR_HR", "DR_K%",
        "DR_xBA", "DR_Barrel%", "DR_HardHit%",
    ])

    # Try DB first
    if _db_exists(season):
        result = _dr_stats_from_db(
            season, start_date, end_date, log
        )
        if result is not None and not result.empty:
            return result

    # HTTP fallback: day-by-day statcast search
    log("  No local DB, fetching day-by-day from Savant...")
    log("  Tip: run 'python build_statcast_db.py "
        f"--season {season}' first.")

    import datetime as _dt
    d_start = _dt.date.fromisoformat(start_date)
    d_end = _dt.date.fromisoformat(end_date)

    frames = []
    current = d_start
    while current <= d_end:
        date_str = current.isoformat()
        log(f"    {date_str}...", flush=True)
        try:
            df = _statcast_search_csv(
                season, date_str, date_str
            )
            if not df.empty:
                log(f"    {len(df)} pitch records")
                frames.append(df)
        except Exception as exc:
            log(f"    error: {exc}")
        current += _dt.timedelta(days=1)
        time.sleep(1.0)

    if not frames:
        return empty

    all_pitches = pd.concat(frames, ignore_index=True)

    if "events" not in all_pitches.columns:
        return empty
    ab_outcomes = all_pitches[
        all_pitches["events"].notna()
        & (all_pitches["events"] != "")
    ].copy()

    agg_rows = []
    for batter_id, grp in ab_outcomes.groupby("batter"):
        pa = len(grp)
        hr = int((grp["events"] == "home_run").sum())
        strikeouts = grp["events"].isin(
            ["strikeout", "strikeout_double_play"]
        ).sum()
        k_pct = round(100 * strikeouts / pa, 1) if pa else None

        # At-bats (exclude walks, HBP, sac)
        non_ab = grp["events"].str.contains(
            "walk|hit_by_pitch|sac|catcher_interf",
            na=False,
        ).sum()
        ab = pa - non_ab

        bb = grp[
            (grp["type"] == "X") & grp["launch_speed"].notna()
        ] if "type" in grp.columns else grp[
            grp["launch_speed"].notna()
        ]
        n_bb = len(bb)

        # xBA = sum(estimated_ba) / at_bats
        xba = None
        if ("estimated_ba_using_speedangle" in bb.columns
                and ab > 0):
            sum_xba = pd.to_numeric(
                bb["estimated_ba_using_speedangle"],
                errors="coerce",
            ).sum()
            xba = round(sum_xba / ab, 3)

        # Barrel% using launch_speed_angle=6
        barrel_pct = None
        if "launch_speed_angle" in bb.columns and n_bb:
            barrels = (
                pd.to_numeric(
                    bb["launch_speed_angle"],
                    errors="coerce",
                ) == 6
            ).sum()
            barrel_pct = round(100 * barrels / n_bb, 1)

        hh_pct = None
        if n_bb:
            hh_pct = round(
                100 * (bb["launch_speed"] >= 95).sum() / n_bb,
                1,
            )

        agg_rows.append({
            "MLBAMID": batter_id,
            "DR_PA": pa,
            "DR_HR": hr,
            "DR_K%": k_pct,
            "DR_xBA": xba,
            "DR_Barrel%": barrel_pct,
            "DR_HardHit%": hh_pct,
        })

    return pd.DataFrame(agg_rows)


# =========================================================================
# POSITION DATA from SFBB Player ID Map
# =========================================================================


def load_position_data(log=print):
    """Load MLBAMID -> Position mapping from SFBB Player ID Map."""
    if not os.path.exists(SFBB_MAP_PATH):
        log(f"    SFBB map not found: {SFBB_MAP_PATH}")
        return pd.DataFrame(columns=["MLBAMID", "Pos"])

    log("  [SFBB] Loading position data...")
    df = pd.read_csv(SFBB_MAP_PATH)

    if "MLBID" not in df.columns or "ALLPOS" not in df.columns:
        log("    Missing MLBID or ALLPOS columns.")
        return pd.DataFrame(columns=["MLBAMID", "Pos"])

    pos_df = df[["MLBID", "ALLPOS"]].copy()
    pos_df = pos_df[pos_df["MLBID"].notna()]
    pos_df["MLBAMID"] = pd.to_numeric(
        pos_df["MLBID"], errors="coerce"
    )
    pos_df = pos_df[pos_df["MLBAMID"].notna()]
    pos_df["MLBAMID"] = pos_df["MLBAMID"].astype(int)
    pos_df["Pos"] = pos_df["ALLPOS"].fillna("")
    pos_df = pos_df[["MLBAMID", "Pos"]].drop_duplicates(
        subset=["MLBAMID"]
    )
    log(f"    {len(pos_df)} players with position data.")
    return pos_df


# =========================================================================
# FANGRAPHS AUCTION CSV
# =========================================================================


def load_fangraphs_auction_csv(filepath, log=print):
    """Load BatX auction values from a FanGraphs CSV export."""
    if not filepath or not os.path.exists(filepath):
        return None

    log(f"  [FanGraphs] Loading auction CSV: {filepath}")
    df = pd.read_csv(filepath)

    # Find dollar/value column
    value_col = None
    for col in df.columns:
        cl = col.lower().strip()
        if cl in ("dollars", "dollar", "$", "value",
                   "pricedollars", "auction value",
                   "auction_value"):
            value_col = col
            break
        if "$" in col:
            value_col = col
            break
    if value_col is None:
        for col in df.columns:
            if "dollar" in col.lower() or "value" in col.lower():
                value_col = col
                break

    # Find name column
    name_col = None
    for col in df.columns:
        if col.strip().lower() in (
            "name", "playername", "player name", "player"
        ):
            name_col = col
            break
    if name_col is None:
        name_col = df.columns[0]

    # Find MLBAMID column (prefer mlbamid over playerid)
    id_col = None
    for col in df.columns:
        cl = col.lower().strip()
        if cl in ("mlbamid", "xmlbamid", "mlbam"):
            id_col = col
            break
    if id_col is None:
        for col in df.columns:
            cl = col.lower().strip()
            if cl == "playerid":
                id_col = col
                break

    result = pd.DataFrame()
    result["Name"] = df[name_col]
    result["name_norm"] = result["Name"].apply(normalize_name)

    if value_col:
        result["FG_AuctionValue"] = pd.to_numeric(
            df[value_col], errors="coerce"
        )
        log(f"    Found auction value column: '{value_col}'")
    else:
        log("    Warning: no dollar-value column found")

    if id_col:
        result["MLBAMID"] = pd.to_numeric(
            df[id_col], errors="coerce"
        )

    return result


# =========================================================================
# EVANALYTICS
# =========================================================================


def fetch_evanalytics_rankings():
    """Fetch context-neutral wOBA ranking from EVAnalytics."""
    print("  [EVAnalytics] Context-neutral wOBA rankings...")

    try:
        session = requests.Session()
        session.headers.update(HTTP_HEADERS)
        session.get(
            "https://evanalytics.com/mlb/leaderboards/"
            "hitter-rankings",
            timeout=30,
        )

        param_str = "mode=runTime&dataTable_id=67"
        encoded = base64.b64encode(
            param_str.encode()
        ).decode()

        resp = session.post(
            "https://evanalytics.com/admin/model/"
            "datatableQuery.php",
            data={"parameter": encoded},
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type":
                    "application/x-www-form-urlencoded; "
                    "charset=UTF-8",
            },
            timeout=30,
        )

        if resp.status_code == 200 and resp.text:
            data = resp.json()
            if isinstance(data, dict) and "dataRows" in data:
                rows = []
                for entry in data["dataRows"]:
                    cols = entry.get("columns", {})
                    rows.append(cols)
                if rows:
                    df = pd.DataFrame(rows)
                    print(f"    Retrieved {len(df)} players.")
                    return df

            print(
                "    Unexpected response keys: "
                f"{list(data.keys()) if isinstance(data, dict) else '?'}"
            )
            return None
        else:
            print(
                f"    Empty or error response "
                f"(status {resp.status_code})."
            )
            return None

    except Exception as e:
        print(f"    Warning: EVAnalytics fetch failed: {e}")

    return None


# =========================================================================
# DASHBOARD BUILDER
# =========================================================================


def build_dashboard(season, fg_csv, min_pa, output,
                    date_start, date_end,
                    skip_exit_velo=False,
                    skip_date_range=False, log=print):
    """Fetch all data sources and merge into a dashboard."""

    log(f"\n{'=' * 64}")
    log("  HITTER DASHBOARD BUILDER")
    log(f"  Season: {season}")
    log(f"  Date range: {date_start} to {date_end}")
    log(f"  Min PA filter: {min_pa}")
    log(f"{'=' * 64}\n")

    # ------------------------------------------------------------------
    # 1) Season stats + bat speed + HR
    # ------------------------------------------------------------------
    log("[1/5] Fetching season-long stats...")
    season_df = fetch_savant_season_stats(
        season, min_pa=min_pa, log=log
    )
    bat_speed_df = fetch_savant_bat_speed(season, log=log)
    hr_df = _compute_hr_from_db(season, log=log)

    # ------------------------------------------------------------------
    # 2) Exit velocity buckets
    # ------------------------------------------------------------------
    if skip_exit_velo:
        log("\n[2/5] Skipping exit-velocity buckets.")
        ev_df = pd.DataFrame(
            columns=["MLBAMID", "EV_105_110",
                      "EV_110_115", "EV_115+"]
        )
    else:
        log("\n[2/5] Fetching exit-velocity buckets...")
        ev_df = fetch_exit_velo_buckets(season, log=log)

    # ------------------------------------------------------------------
    # 3) Date-range stats
    # ------------------------------------------------------------------
    if skip_date_range:
        log("\n[3/5] Skipping date-range stats.")
        dr_df = pd.DataFrame(columns=[
            "MLBAMID", "DR_PA", "DR_HR", "DR_K%",
            "DR_xBA", "DR_Barrel%", "DR_HardHit%",
        ])
    else:
        log(f"\n[3/5] Fetching date-range stats "
            f"({date_start} to {date_end})...")
        dr_df = fetch_savant_date_range_stats(
            season, date_start, date_end, log=log
        )

    # ------------------------------------------------------------------
    # 4) Positions from SFBB map
    # ------------------------------------------------------------------
    log("\n[4/5] Loading position data...")
    pos_df = load_position_data(log=log)

    # ------------------------------------------------------------------
    # 5) FanGraphs auction CSV + EVAnalytics
    # ------------------------------------------------------------------
    log("\n[5/5] Loading supplemental data...")
    fg_path = fg_csv
    auction_df = (
        load_fangraphs_auction_csv(fg_path, log=log)
        if fg_path else None
    )
    if auction_df is None and not fg_path:
        log("  Tip: pass --fg-csv <path> for BatX auction "
            "values.")

    eva_df = fetch_evanalytics_rankings()

    # ------------------------------------------------------------------
    # Merge everything
    # ------------------------------------------------------------------
    log("\nMerging all data sources...")

    dash = season_df.copy()

    # Bat speed
    if not bat_speed_df.empty:
        dash = dash.merge(
            bat_speed_df[["MLBAMID", "AvgBatSpeed"]],
            on="MLBAMID", how="left",
        )

    # HR from DB
    if not hr_df.empty:
        dash = dash.merge(
            hr_df, on="MLBAMID", how="left"
        )
        dash["HR"] = dash["HR"].fillna(0).astype(int)
    else:
        dash["HR"] = None

    # Positions
    if not pos_df.empty:
        dash = dash.merge(
            pos_df, on="MLBAMID", how="left"
        )

    # Exit-velocity buckets
    if not ev_df.empty:
        dash = dash.merge(ev_df, on="MLBAMID", how="left")
        for col in ["EV_105_110", "EV_110_115", "EV_115+"]:
            if col in dash.columns:
                dash[col] = dash[col].fillna(0).astype(int)

    # Date-range stats
    if not dr_df.empty:
        dash = dash.merge(dr_df, on="MLBAMID", how="left")

    # FanGraphs auction values
    if (auction_df is not None
            and "FG_AuctionValue" in auction_df.columns):
        if "MLBAMID" in auction_df.columns:
            ac = auction_df[
                ["MLBAMID", "FG_AuctionValue"]
            ].dropna(subset=["MLBAMID"])
            ac["MLBAMID"] = ac["MLBAMID"].astype(int)
            dash = dash.merge(ac, on="MLBAMID", how="left")
        else:
            dash["name_norm"] = dash["Name"].apply(
                normalize_name
            )
            ac = auction_df[
                ["name_norm", "FG_AuctionValue"]
            ].copy()
            dash = dash.merge(ac, on="name_norm", how="left")
            dash.drop(columns=["name_norm"], inplace=True)

    # EVAnalytics rankings
    if eva_df is not None:
        hitter_col = None
        rank_col = None
        for col in eva_df.columns:
            cl = col.strip().lower()
            if cl in ("hitter", "name", "player"):
                hitter_col = col
            if cl == "rank":
                rank_col = col

        if hitter_col and rank_col:
            eva_clean = eva_df[[rank_col, hitter_col]].copy()
            eva_clean.rename(
                columns={rank_col: "EVA_Rank"}, inplace=True
            )
            eva_clean["name_norm"] = eva_clean[
                hitter_col
            ].apply(normalize_name)
            eva_clean["EVA_Rank"] = pd.to_numeric(
                eva_clean["EVA_Rank"], errors="coerce"
            )

            dash["name_norm"] = dash["Name"].apply(
                normalize_name
            )
            dash = dash.merge(
                eva_clean[["name_norm", "EVA_Rank"]],
                on="name_norm", how="left",
            )
            dash.drop(columns=["name_norm"], inplace=True)
            matched = dash["EVA_Rank"].notna().sum()
            log(f"  Matched {matched} players to "
                f"EVAnalytics rankings.")

    # ------------------------------------------------------------------
    # Reorder columns
    # ------------------------------------------------------------------
    priority_cols = ["Name", "MLBAMID", "Pos"]
    if "FG_AuctionValue" in dash.columns:
        priority_cols.append("FG_AuctionValue")
    if "EVA_Rank" in dash.columns:
        priority_cols.append("EVA_Rank")

    season_stats = [
        "PA", "xBA", "HR", "Barrel%", "HardHit%", "K%",
        "EV_105_110", "EV_110_115", "EV_115+", "AvgBatSpeed",
    ]
    date_range_cols = [
        "DR_PA", "DR_xBA", "DR_HR",
        "DR_Barrel%", "DR_HardHit%", "DR_K%",
        "DR_AvgBatSpeed",
    ]

    ordered = []
    for col in priority_cols + season_stats + date_range_cols:
        if col in dash.columns:
            ordered.append(col)
    for col in dash.columns:
        if col not in ordered:
            ordered.append(col)

    dash = dash[ordered]

    # Filter + sort
    dash = dash[dash["PA"] >= min_pa].copy()
    dash.sort_values("PA", ascending=False, inplace=True)

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------
    if output:
        dash.to_csv(output, index=False)
        log(f"\n  Dashboard saved to: {output}")

    log(f"\n{'=' * 64}")
    log(f"  Players: {len(dash)}")
    log(f"  Columns: {', '.join(dash.columns)}")
    log(f"{'=' * 64}")

    preview_cols = [
        c for c in ["Name", "Pos", "PA", "xBA", "HR",
                     "Barrel%", "K%", "AvgBatSpeed",
                     "EVA_Rank"]
        if c in dash.columns
    ]
    log("\nTop 15 by PA:\n")
    log(dash[preview_cols].head(15).to_string(index=False))

    return dash


# =========================================================================
# CLI
# =========================================================================


def main():
    parser = argparse.ArgumentParser(
        description="Hitter Dashboard - aggregate MLB hitter data",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--season", type=int, default=DEFAULT_SEASON,
        help=f"MLB season year (default: {DEFAULT_SEASON})",
    )
    parser.add_argument(
        "--fg-csv", type=str, default=None,
        help="Path to FanGraphs auction calculator CSV export",
    )
    parser.add_argument(
        "--min-pa", type=int, default=DEFAULT_MIN_PA,
        help=f"Min plate appearances (default: {DEFAULT_MIN_PA})",
    )
    parser.add_argument(
        "--output", "-o", type=str, default=DEFAULT_OUTPUT,
        help=f"Output CSV path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--date-start", type=str,
        default=DEFAULT_DATE_RANGE_START,
        help=f"Date-range start (default: "
             f"{DEFAULT_DATE_RANGE_START})",
    )
    parser.add_argument(
        "--date-end", type=str,
        default=DEFAULT_DATE_RANGE_END,
        help=f"Date-range end (default: "
             f"{DEFAULT_DATE_RANGE_END})",
    )
    parser.add_argument(
        "--skip-exit-velo", action="store_true",
        help="Skip exit-velocity bucket fetch",
    )
    parser.add_argument(
        "--skip-date-range", action="store_true",
        help="Skip date-range stats fetch",
    )

    args = parser.parse_args()

    build_dashboard(
        season=args.season,
        fg_csv=args.fg_csv,
        min_pa=args.min_pa,
        output=args.output,
        date_start=args.date_start,
        date_end=args.date_end,
        skip_exit_velo=args.skip_exit_velo,
        skip_date_range=args.skip_date_range,
    )


if __name__ == "__main__":
    main()
