#!/usr/bin/env python3
"""
Pitcher Dashboard Builder

Aggregates MLB pitcher data from multiple sources:
  - Baseball Savant custom pitcher leaderboard: season ERA, xERA, K%, BB%, xwOBA, etc.
  - Baseball Savant pitch-arsenal leaderboard: Stuff+
  - SQLite statcast DB: H1/H2 split stats (CSW%, Whiff%, Zone%, Chase%, etc.)

Usage:
    python pitcher_dashboard.py
    python pitcher_dashboard.py --season 2025 --min-bf 50
"""

import os
import sqlite3
import time
from io import StringIO

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
DEFAULT_MIN_BF = 100
DEFAULT_MIN_IP = 20
DEFAULT_H1_START = "2025-04-01"
DEFAULT_H1_END = "2025-07-31"
DEFAULT_H2_START = "2025-08-01"
DEFAULT_H2_END = "2025-10-01"

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_db_path(season):
    return os.path.join(_BASE_DIR, "data", f"statcast_{season}.db")


def _db_exists(season):
    return os.path.exists(_get_db_path(season))


def _safe_get(url, params=None, retries=3, delay=2, log=print):
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, headers=HTTP_HEADERS, timeout=30)
            if r.status_code == 200:
                return r
            log(f"    HTTP {r.status_code} for {url}")
        except Exception as e:
            log(f"    Request error (attempt {attempt + 1}): {e}")
        if attempt < retries - 1:
            time.sleep(delay)
    return None


def _parse_savant_csv(text):
    """Parse Baseball Savant CSV response into a DataFrame."""
    lines = text.strip().splitlines()
    # Drop trailing empty/metadata lines
    clean = [l for l in lines if l.strip() and not l.startswith("Generated")]
    return pd.read_csv(StringIO("\n".join(clean)), low_memory=False)


# ---------------------------------------------------------------------------
# Season stats from Baseball Savant custom pitcher leaderboard
# ---------------------------------------------------------------------------

def fetch_savant_pitcher_season_stats(season, min_bf=1, log=print):
    """
    Fetch season-level pitcher stats from Baseball Savant custom leaderboard.
    Returns DataFrame with columns: MLBAMID, Name, IP, BF, ERA, xERA, K%, BB%,
    K-BB%, xwOBA, Whiff%, Barrel%, HH%, GB%, FB%
    """
    log(f"[1/4] Fetching pitcher season stats from Baseball Savant ({season})...")

    selections = ",".join([
        "p_formatted_ip",
        "pa",
        "k_percent",
        "bb_percent",
        "p_earned_run_avg",
        "xera",
        "xwoba",
        "whiff_percent",
        "barrel_batted_rate",
        "hard_hit_percent",
        "groundballs_percent",
        "flyballs_percent",
    ])

    url = (
        f"https://baseballsavant.mlb.com/leaderboard/custom"
        f"?year={season}&type=pitcher&filter=&min={min_bf}"
        f"&selections={selections}&chart=false&csv=true"
    )

    resp = _safe_get(url, log=log)
    if resp is None:
        log("    WARNING: Could not fetch pitcher season stats")
        return pd.DataFrame()

    df = _parse_savant_csv(resp.text)
    if df.empty:
        log("    WARNING: Empty response from Savant pitcher leaderboard")
        return pd.DataFrame()

    log(f"    Raw pitcher leaderboard: {len(df)} rows, columns: {list(df.columns[:10])}")

    # Build name
    if "last_name" in df.columns and "first_name" in df.columns:
        df["Name"] = df["first_name"].str.strip() + " " + df["last_name"].str.strip()
    elif "last_name, first_name" in df.columns:
        parts = df["last_name, first_name"].str.split(", ", n=1, expand=True)
        df["Name"] = parts[1].str.strip() + " " + parts[0].str.strip()
    else:
        df["Name"] = df.get("player_name", "Unknown")

    # MLBAMID
    df["MLBAMID"] = pd.to_numeric(df.get("player_id", df.get("pitcher_id")), errors="coerce").astype("Int64")
    df = df.dropna(subset=["MLBAMID"])

    # Rename columns
    col_map = {
        "pa": "BF",
        "p_formatted_ip": "IP",
        "k_percent": "K%",
        "bb_percent": "BB%",
        "p_earned_run_avg": "ERA",
        "xera": "xERA",
        "xwoba": "xwOBA",
        "whiff_percent": "Whiff%",
        "barrel_batted_rate": "Barrel%",
        "hard_hit_percent": "HH%",
        "groundballs_percent": "GB%",
        "flyballs_percent": "FB%",
    }
    df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})

    # Numeric coercion
    for col in ["BF", "IP", "ERA", "xERA", "K%", "BB%", "xwOBA",
                "Whiff%", "Barrel%", "HH%", "GB%", "FB%"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Derive K-BB%
    if "K%" in df.columns and "BB%" in df.columns:
        df["K-BB%"] = (df["K%"] - df["BB%"]).round(1)

    keep = ["MLBAMID", "Name", "IP", "BF", "ERA", "xERA",
            "K%", "BB%", "K-BB%", "xwOBA", "Whiff%",
            "Barrel%", "HH%", "GB%", "FB%"]
    keep = [c for c in keep if c in df.columns]
    df = df[keep].copy()

    log(f"    Season stats: {len(df)} pitchers")
    return df


# ---------------------------------------------------------------------------
# Stuff+ from pitch-arsenal leaderboard
# ---------------------------------------------------------------------------

def fetch_savant_stuff_plus(season, log=print):
    """
    Fetch Stuff+ per pitch type and compute a weighted average per pitcher.
    Returns DataFrame with MLBAMID and Stuff+.
    """
    log(f"[2/4] Fetching Stuff+ from pitch-arsenal leaderboard ({season})...")

    url = (
        f"https://baseballsavant.mlb.com/leaderboard/pitch-arsenal-stats"
        f"?type=pitcher&pitchType=&year={season}&team=&min=10"
        f"&sort=stuff_plus&sortDir=desc&csv=true"
    )

    resp = _safe_get(url, log=log)
    if resp is None:
        log("    WARNING: Could not fetch Stuff+ data")
        return pd.DataFrame()

    df = _parse_savant_csv(resp.text)
    if df.empty:
        log("    WARNING: Empty Stuff+ response")
        return pd.DataFrame()

    log(f"    Stuff+ raw: {len(df)} rows, columns: {list(df.columns[:12])}")

    # Find the Stuff+ column — varies by Savant version
    stuff_col = None
    for candidate in ["stuff_plus", "stuff_plus_stuff", "xwoba_stuff_plus"]:
        if candidate in df.columns:
            stuff_col = candidate
            break

    if stuff_col is None:
        log(f"    WARNING: No Stuff+ column found in {list(df.columns)}")
        return pd.DataFrame()

    # Find pitcher ID column
    id_col = None
    for candidate in ["pitcher_id", "player_id", "pitcher"]:
        if candidate in df.columns:
            id_col = candidate
            break

    if id_col is None:
        log("    WARNING: No pitcher ID column in Stuff+ response")
        return pd.DataFrame()

    # Find pitch count column for weighting
    count_col = None
    for candidate in ["n", "n_", "pitches", "pitch_count", "total_pitches"]:
        if candidate in df.columns:
            count_col = candidate
            break

    df["MLBAMID"] = pd.to_numeric(df[id_col], errors="coerce").astype("Int64")
    df[stuff_col] = pd.to_numeric(df[stuff_col], errors="coerce")
    df = df.dropna(subset=["MLBAMID", stuff_col])

    if count_col and count_col in df.columns:
        df[count_col] = pd.to_numeric(df[count_col], errors="coerce").fillna(0).astype(float)
        df[stuff_col] = df[stuff_col].astype(float)
        # Weighted average Stuff+ per pitcher using pure pandas
        df["_weighted"] = df[stuff_col] * df[count_col]
        grp = df.groupby("MLBAMID")
        result = (grp["_weighted"].sum() / grp[count_col].sum()).reset_index()
        result.columns = ["MLBAMID", "Stuff+"]
    else:
        df[stuff_col] = df[stuff_col].astype(float)
        result = df.groupby("MLBAMID")[stuff_col].mean().reset_index()
        result.columns = ["MLBAMID", "Stuff+"]

    result["Stuff+"] = result["Stuff+"].round(1)
    log(f"    Stuff+: {len(result)} pitchers")
    return result


# ---------------------------------------------------------------------------
# Split stats from SQLite DB
# ---------------------------------------------------------------------------

def _pitcher_split_from_db(season, start_date, end_date, prefix, log=print):
    """
    Query the statcast DB for pitcher stats over a date range.
    Returns DataFrame with MLBAMID + prefixed stat columns.
    prefix should be 'H1_' or 'H2_'.
    """
    db_path = _get_db_path(season)
    if not os.path.exists(db_path):
        log(f"    No DB at {db_path} — skipping {prefix} splits")
        return pd.DataFrame()

    log(f"    Querying DB for {prefix} splits ({start_date} to {end_date})...")

    swing_descriptions = (
        "'swinging_strike','swinging_strike_blocked','foul','foul_tip',"
        "'hit_into_play','hit_into_play_no_out','hit_into_play_score',"
        "'foul_bunt','missed_bunt','bunt_foul_tip'"
    )
    csw_descriptions = (
        "'called_strike','swinging_strike','swinging_strike_blocked'"
    )
    whiff_descriptions = (
        "'swinging_strike','swinging_strike_blocked'"
    )

    query = f"""
        SELECT
            CAST(pitcher AS INTEGER)                                        AS MLBAMID,
            COUNT(*)                                                         AS total_pitches,
            SUM(CASE WHEN events IS NOT NULL AND events != ''
                THEN 1 ELSE 0 END)                                          AS total_bf,
            SUM(CASE WHEN type = 'X' THEN 1 ELSE 0 END)                    AS total_bbe,

            SUM(CASE WHEN events IN ('strikeout','strikeout_double_play')
                THEN 1 ELSE 0 END)                                          AS k_count,
            SUM(CASE WHEN events LIKE '%walk%'
                THEN 1 ELSE 0 END)                                          AS bb_count,

            SUM(CASE WHEN description IN ({csw_descriptions})
                THEN 1 ELSE 0 END)                                          AS csw_count,
            SUM(CASE WHEN description IN ({swing_descriptions})
                THEN 1 ELSE 0 END)                                          AS swing_count,
            SUM(CASE WHEN description IN ({whiff_descriptions})
                THEN 1 ELSE 0 END)                                          AS whiff_count,

            SUM(CASE WHEN CAST(zone AS INTEGER) BETWEEN 1 AND 9
                THEN 1 ELSE 0 END)                                          AS zone_count,
            SUM(CASE WHEN zone IS NOT NULL AND zone != ''
                    AND CAST(zone AS INTEGER) > 9
                THEN 1 ELSE 0 END)                                          AS oz_count,
            SUM(CASE WHEN zone IS NOT NULL AND zone != ''
                    AND CAST(zone AS INTEGER) > 9
                    AND description IN ({swing_descriptions})
                THEN 1 ELSE 0 END)                                          AS chase_count,

            SUM(CASE WHEN type = 'X'
                    AND CAST(launch_speed AS REAL) >= 95
                THEN 1 ELSE 0 END)                                          AS hh_count,
            SUM(CASE WHEN type = 'X'
                    AND CAST(launch_speed_angle AS REAL) = 6
                THEN 1 ELSE 0 END)                                          AS barrel_count,

            SUM(CASE WHEN type = 'X'
                    AND estimated_woba_using_speedangle IS NOT NULL
                    AND estimated_woba_using_speedangle != ''
                THEN CAST(estimated_woba_using_speedangle AS REAL)
                ELSE 0 END)                                                 AS sum_xwoba,
            SUM(CASE WHEN type = 'X'
                    AND estimated_woba_using_speedangle IS NOT NULL
                    AND estimated_woba_using_speedangle != ''
                THEN 1 ELSE 0 END)                                          AS xwoba_count,

            SUM(CASE WHEN bb_type = 'ground_ball' THEN 1 ELSE 0 END)       AS gb_count,
            SUM(CASE WHEN bb_type = 'fly_ball'    THEN 1 ELSE 0 END)       AS fb_count,
            SUM(CASE WHEN bb_type IS NOT NULL AND bb_type != ''
                THEN 1 ELSE 0 END)                                          AS bbe_typed
        FROM pitches
        WHERE game_date >= ? AND game_date <= ?
        GROUP BY pitcher
    """

    conn = sqlite3.connect(db_path)
    try:
        raw = pd.read_sql_query(query, conn, params=[start_date, end_date])
    finally:
        conn.close()

    if raw.empty:
        log(f"    {prefix}: No data in DB for that date range")
        return pd.DataFrame()

    log(f"    {prefix}: {len(raw)} pitchers from DB")

    p = prefix  # e.g. "H1_"
    out = pd.DataFrame()
    out["MLBAMID"] = raw["MLBAMID"]

    out[f"{p}BF"]     = raw["total_bf"]
    out[f"{p}K%"]     = (100.0 * raw["k_count"] / raw["total_bf"].replace(0, pd.NA)).round(1)
    out[f"{p}BB%"]    = (100.0 * raw["bb_count"] / raw["total_bf"].replace(0, pd.NA)).round(1)
    out[f"{p}K-BB%"]  = (out[f"{p}K%"] - out[f"{p}BB%"]).round(1)
    out[f"{p}CSW%"]   = (100.0 * raw["csw_count"] / raw["total_pitches"].replace(0, pd.NA)).round(1)
    out[f"{p}Whiff%"] = (100.0 * raw["whiff_count"] / raw["swing_count"].replace(0, pd.NA)).round(1)
    out[f"{p}Zone%"]  = (100.0 * raw["zone_count"] / raw["total_pitches"].replace(0, pd.NA)).round(1)
    out[f"{p}Chase%"] = (100.0 * raw["chase_count"] / raw["oz_count"].replace(0, pd.NA)).round(1)
    out[f"{p}Barrel%"]= (100.0 * raw["barrel_count"] / raw["total_bbe"].replace(0, pd.NA)).round(1)
    out[f"{p}HH%"]    = (100.0 * raw["hh_count"] / raw["total_bbe"].replace(0, pd.NA)).round(1)
    out[f"{p}xwOBA"]  = (raw["sum_xwoba"] / raw["xwoba_count"].replace(0, pd.NA)).round(3)
    out[f"{p}GB%"]    = (100.0 * raw["gb_count"] / raw["bbe_typed"].replace(0, pd.NA)).round(1)
    out[f"{p}FB%"]    = (100.0 * raw["fb_count"] / raw["bbe_typed"].replace(0, pd.NA)).round(1)

    return out


# ---------------------------------------------------------------------------
# Main dashboard builder
# ---------------------------------------------------------------------------

def build_pitcher_dashboard(
    season=DEFAULT_SEASON,
    min_bf=DEFAULT_MIN_BF,
    min_ip=DEFAULT_MIN_IP,
    h1_start=DEFAULT_H1_START,
    h1_end=DEFAULT_H1_END,
    h2_start=DEFAULT_H2_START,
    h2_end=DEFAULT_H2_END,
    log=print,
):
    """Build the full pitcher dashboard DataFrame."""

    # 1. Season stats
    season_df = fetch_savant_pitcher_season_stats(season, min_bf=min_bf, log=log)
    if season_df.empty:
        raise RuntimeError("Could not fetch pitcher season stats from Baseball Savant")

    # 2. Stuff+
    stuff_df = fetch_savant_stuff_plus(season, log=log)

    # 3. H1 split
    log(f"[3/4] Fetching first-half splits ({h1_start} to {h1_end})...")
    h1_df = _pitcher_split_from_db(season, h1_start, h1_end, "H1_", log=log)

    # 4. H2 split
    log(f"[4/4] Fetching second-half splits ({h2_start} to {h2_end})...")
    h2_df = _pitcher_split_from_db(season, h2_start, h2_end, "H2_", log=log)

    # Merge
    log("Merging all data sources...")
    df = season_df.copy()

    if not stuff_df.empty:
        df = df.merge(stuff_df, on="MLBAMID", how="left")

    if not h1_df.empty:
        df = df.merge(h1_df, on="MLBAMID", how="left")

    if not h2_df.empty:
        df = df.merge(h2_df, on="MLBAMID", how="left")

    # Filter by min thresholds
    if min_bf and "BF" in df.columns:
        df = df[df["BF"].fillna(0) >= min_bf]
    if min_ip and "IP" in df.columns:
        # IP may be stored as "100.0" or "100.1" (not decimal innings)
        ip_num = pd.to_numeric(df["IP"], errors="coerce")
        df = df[ip_num.fillna(0) >= min_ip]

    df = df.sort_values("BF", ascending=False).reset_index(drop=True)

    # Column ordering
    priority = ["Name", "MLBAMID", "IP", "BF"]
    season_stats = ["ERA", "xERA", "K%", "BB%", "K-BB%", "xwOBA",
                    "Whiff%", "Barrel%", "HH%", "GB%", "FB%", "Stuff+"]
    h1_cols = [c for c in df.columns if c.startswith("H1_")]
    h2_cols = [c for c in df.columns if c.startswith("H2_")]

    ordered = (
        [c for c in priority if c in df.columns] +
        [c for c in season_stats if c in df.columns] +
        sorted(h1_cols) +
        sorted(h2_cols)
    )
    remaining = [c for c in df.columns if c not in ordered]
    df = df[ordered + remaining]

    log(f"Players: {len(df)} pitchers in dashboard")
    return df
