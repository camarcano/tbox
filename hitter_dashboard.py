#!/usr/bin/env python3
"""
Hitter Dashboard Builder

Aggregates MLB hitter data from multiple sources into a single CSV:
  - Baseball Savant: xBA, barrel rate, hard hit rate, avg bat speed, exit velo buckets
  - FanGraphs (via pybaseball): K%, date-range stats (PA, HR, K%)
  - FanGraphs CSV: BatX auction values (manual export from auction calculator)
  - EVAnalytics: Derek Carty's context-neutral wOBA ranking

Usage:
    python hitter_dashboard.py
    python hitter_dashboard.py --season 2025 --fg-csv fangraphs_export.csv
    python hitter_dashboard.py --min-pa 100 --output my_dashboard.csv
    python hitter_dashboard.py --skip-exit-velo --skip-date-range  # faster run

FanGraphs Auction Values:
    To include BatX auction values, export the CSV from:
    https://www.fangraphs.com/fantasy-tools/auction-calculator
    Select THE BAT X as the projection system, configure your league,
    generate projections, then export the Batters tab as CSV.
    Pass the file path with --fg-csv.
"""

import argparse
import base64
import os
import time
from io import StringIO

import pandas as pd
import requests
from pybaseball import batting_stats, playerid_reverse_lookup
import pybaseball

pybaseball.cache.enable()

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
                resp = requests.post(url, data=data, headers=hdrs, timeout=90)
            else:
                resp = requests.get(url, headers=hdrs, timeout=90)
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            if attempt < max_retries - 1:
                wait = delay * (attempt + 1)
                print(f"  Retry {attempt + 1}/{max_retries} in {wait}s: {e}")
                time.sleep(wait)
            else:
                raise
    return None


def normalize_name(name: str) -> str:
    """'Last, First' -> 'first last' (lowercased, stripped of punctuation)."""
    if not isinstance(name, str):
        return ""
    name = name.strip()
    if "," in name:
        parts = [p.strip() for p in name.split(",", 1)]
        name = f"{parts[1]} {parts[0]}"
    import unicodedata
    name = unicodedata.normalize("NFD", name.lower())
    name = "".join(c for c in name if unicodedata.category(c) != "Mn")
    return name.replace(".", "").replace("'", "").replace("-", " ").strip()


def _build_id_map(fangraphs_ids):
    """Map FanGraphs player IDs to MLBAM IDs via the Chadwick register."""
    print("  Building FanGraphs -> MLBAM ID map...")
    try:
        reg = playerid_reverse_lookup(list(fangraphs_ids), key_type="fangraphs")
        return dict(zip(reg["key_fangraphs"], reg["key_mlbam"]))
    except Exception as e:
        print(f"  Warning: ID lookup failed: {e}")
        return {}


# =========================================================================
# BASEBALL SAVANT - Leaderboard Endpoints (fast, pre-aggregated)
# =========================================================================


def fetch_savant_expected_stats(season, min_pa=1):
    """PA and xBA from the Baseball Savant expected-statistics leaderboard."""
    print("  [Savant] Expected statistics (PA, xBA)...")
    url = (
        f"https://baseballsavant.mlb.com/leaderboard/expected_statistics"
        f"?type=batter&year={season}&position=&team=&min={min_pa}&csv=true"
    )
    resp = fetch_url(url)
    df = pd.read_csv(StringIO(resp.text))
    out = df[["player_id", "last_name, first_name", "pa", "est_ba"]].copy()
    out.columns = ["MLBAMID", "Name", "PA", "xBA"]
    return out


def fetch_savant_barrel_hardhit(season, min_bbe=1):
    """Barrel% and hard-hit% from the Baseball Savant statcast leaderboard."""
    print("  [Savant] Statcast leaderboard (barrel%, hard-hit%)...")
    url = (
        f"https://baseballsavant.mlb.com/leaderboard/statcast"
        f"?type=batter&year={season}&position=&team=&min={min_bbe}&csv=true"
    )
    resp = fetch_url(url)
    df = pd.read_csv(StringIO(resp.text))
    out = df[["player_id", "last_name, first_name", "brl_percent",
              "ev95percent"]].copy()
    out.columns = ["MLBAMID", "Name", "Barrel%", "HardHit%"]
    return out


def fetch_savant_bat_speed(season):
    """Average bat speed from the Baseball Savant bat-tracking leaderboard."""
    print("  [Savant] Bat tracking (avg bat speed)...")
    url = (
        f"https://baseballsavant.mlb.com/leaderboard/bat-tracking"
        f"?gameType=Regular&minSwings=100&minGroupSwings=1"
        f"&seasonStart={season}&seasonEnd={season}&type=batter&csv=true"
    )
    resp = fetch_url(url)
    df = pd.read_csv(StringIO(resp.text))
    out = df[["id", "name", "avg_bat_speed"]].copy()
    out.columns = ["MLBAMID", "Name", "AvgBatSpeed"]
    out["AvgBatSpeed"] = out["AvgBatSpeed"].round(1)
    return out


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


def fetch_exit_velo_buckets(season):
    """
    Count exit-velocity events per batter in three buckets:
    105-110 mph, 110-115 mph, 115+ mph.
    Queries Statcast search month-by-month then filters to >= 105 mph.
    """
    print("  [Savant] Exit velocity buckets (105-110, 110-115, 115+)...")

    windows = [
        (f"{season}-03-20", f"{season}-04-30"),
        (f"{season}-05-01", f"{season}-05-31"),
        (f"{season}-06-01", f"{season}-06-30"),
        (f"{season}-07-01", f"{season}-07-31"),
        (f"{season}-08-01", f"{season}-08-31"),
        (f"{season}-09-01", f"{season}-09-30"),
        (f"{season}-10-01", f"{season}-10-15"),
    ]

    frames = []
    for start_dt, end_dt in windows:
        print(f"    {start_dt} to {end_dt}...", end=" ", flush=True)
        try:
            df = _statcast_search_csv(season, start_dt, end_dt)
            if df.empty or "launch_speed" not in df.columns:
                print("no data")
                continue
            fast = df[df["launch_speed"] >= 105][
                ["batter", "player_name", "launch_speed"]
            ].copy()
            print(f"{len(fast)} events >= 105 mph")
            frames.append(fast)
        except Exception as e:
            print(f"error: {e}")
        time.sleep(1.5)

    if not frames:
        print("    Warning: no exit-velocity data retrieved.")
        return pd.DataFrame(
            columns=["MLBAMID", "EV_105_110", "EV_110_115", "EV_115+"]
        )

    events = pd.concat(frames, ignore_index=True)

    def _bucket(speed):
        if speed >= 115:
            return "EV_115+"
        elif speed >= 110:
            return "EV_110_115"
        else:
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

    return pivot[["MLBAMID", "EV_105_110", "EV_110_115", "EV_115+"]]


def fetch_savant_date_range_stats(season, start_date, end_date):
    """
    Compute per-player date-range stats from Statcast search data:
      PA, HR, K%, xBA, barrel%, hard-hit%.
    This replaces the broken pybaseball batting_stats_range.
    """
    print(f"  [Savant] Date-range stats ({start_date} to {end_date})...")

    import datetime as _dt

    frames = []
    d_start = _dt.date.fromisoformat(start_date)
    d_end = _dt.date.fromisoformat(end_date)
    chunk_start = d_start

    while chunk_start < d_end:
        chunk_end = min(chunk_start + _dt.timedelta(days=30), d_end)
        s, e = chunk_start.isoformat(), chunk_end.isoformat()
        print(f"    {s} to {e}...", end=" ", flush=True)
        try:
            df = _statcast_search_csv(season, s, e)
            if not df.empty:
                print(f"{len(df)} pitch records")
                frames.append(df)
            else:
                print("no data")
        except Exception as exc:
            print(f"error: {exc}")
        chunk_start = chunk_end
        time.sleep(1.5)

    empty = pd.DataFrame(columns=[
        "MLBAMID", "DR_PA", "DR_HR", "DR_K%",
        "DR_xBA", "DR_Barrel%", "DR_HardHit%",
    ])
    if not frames:
        return empty

    all_pitches = pd.concat(frames, ignore_index=True)

    # At-bat outcomes = rows where 'events' is not null
    if "events" not in all_pitches.columns:
        return empty
    ab_outcomes = all_pitches[all_pitches["events"].notna()].copy()

    agg_rows = []
    for batter_id, grp in ab_outcomes.groupby("batter"):
        pa = len(grp)
        hr = int((grp["events"] == "home_run").sum())

        strikeouts = grp["events"].isin(
            ["strikeout", "strikeout_double_play"]
        ).sum()
        k_pct = round(100 * strikeouts / pa, 1) if pa else None

        # Batted-ball subset (launch_speed present)
        bb = grp[grp["launch_speed"].notna()]
        n_bb = len(bb)

        xba = None
        if "estimated_ba_using_speedangle" in bb.columns and n_bb:
            valid = bb["estimated_ba_using_speedangle"].dropna()
            if len(valid):
                xba = round(valid.mean(), 3)

        barrel_pct = None
        if "barrel" in bb.columns and n_bb:
            barrel_pct = round(100 * bb["barrel"].sum() / n_bb, 1)

        hh_pct = None
        if n_bb:
            hh_pct = round(100 * (bb["launch_speed"] >= 95).sum() / n_bb, 1)

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
# FANGRAPHS (via pybaseball — Cloudflare blocks direct API requests)
# =========================================================================


def fetch_fangraphs_season_stats(season, min_pa=50):
    """
    Fetch full-season K% (and other stats) from FanGraphs via pybaseball.
    Returns DataFrame with MLBAMID, Name, K%.
    """
    print(f"  [FanGraphs] Season batting stats via pybaseball ({season})...")
    df = batting_stats(season, qual=min_pa)

    # Map FanGraphs IDs to MLBAM IDs
    fg_ids = df["IDfg"].dropna().unique().tolist()
    id_map = _build_id_map(fg_ids)
    df["MLBAMID"] = df["IDfg"].map(id_map)

    out = df[["MLBAMID", "Name", "K%"]].copy()
    out = out.dropna(subset=["MLBAMID"])
    out["MLBAMID"] = out["MLBAMID"].astype(int)
    # pybaseball returns K% as a proportion (0.236); convert to pct (23.6)
    if out["K%"].max() <= 1:
        out["K%"] = (out["K%"] * 100).round(1)
    print(f"    {len(out)} players with K% data.")
    return out


def load_fangraphs_auction_csv(filepath):
    """
    Load BatX auction values from a manually-exported FanGraphs CSV.
    Auto-detects the dollar-value column.
    """
    if not filepath or not os.path.exists(filepath):
        return None

    print(f"  [FanGraphs] Loading auction CSV: {filepath}")
    df = pd.read_csv(filepath)

    # Find dollar/value column
    value_col = None
    for col in df.columns:
        cl = col.lower().strip()
        if cl in ("dollars", "dollar", "$", "value", "pricedollars",
                   "auction value", "auction_value"):
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
        if col.strip().lower() in ("name", "playername", "player name",
                                    "player"):
            name_col = col
            break
    if name_col is None:
        name_col = df.columns[0]

    # Find MLBAMID column
    id_col = None
    for col in df.columns:
        cl = col.lower().strip()
        if cl in ("mlbamid", "xmlbamid", "playerid", "mlbam"):
            id_col = col
            break

    result = pd.DataFrame()
    result["Name"] = df[name_col]
    result["name_norm"] = result["Name"].apply(normalize_name)

    if value_col:
        result["FG_AuctionValue"] = df[value_col]
        print(f"    Found auction value column: '{value_col}'")
    else:
        print(f"    Warning: no dollar-value column found in: {list(df.columns)}")

    if id_col:
        result["MLBAMID"] = pd.to_numeric(df[id_col], errors="coerce")

    return result


# =========================================================================
# EVANALYTICS
# =========================================================================


def fetch_evanalytics_rankings():
    """
    Fetch Derek Carty's context-neutral wOBA ranking from EVAnalytics.
    The page loads data via a POST with Base64-encoded parameters.
    """
    print("  [EVAnalytics] Context-neutral wOBA rankings...")

    try:
        session = requests.Session()
        session.headers.update(HTTP_HEADERS)
        session.get(
            "https://evanalytics.com/mlb/leaderboards/hitter-rankings",
            timeout=30,
        )

        param_str = "mode=runTime&dataTable_id=67"
        encoded = base64.b64encode(param_str.encode()).decode()

        resp = session.post(
            "https://evanalytics.com/admin/model/datatableQuery.php",
            data={"parameter": encoded},
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
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

            print(f"    Unexpected response keys: "
                  f"{list(data.keys()) if isinstance(data, dict) else '?'}")
            return None
        else:
            print(f"    Empty or error response (status {resp.status_code}).")
            return None

    except Exception as e:
        print(f"    Warning: EVAnalytics fetch failed: {e}")

    print("    --> Visit: https://evanalytics.com/mlb/leaderboards/hitter-rankings")
    return None


# =========================================================================
# DASHBOARD BUILDER
# =========================================================================


def build_dashboard(season, fg_csv, min_pa, output, date_start, date_end,
                    skip_exit_velo=False, skip_date_range=False):
    """Fetch all data sources and merge into a single dashboard CSV."""

    print(f"\n{'=' * 64}")
    print("  HITTER DASHBOARD BUILDER")
    print(f"  Season: {season}")
    print(f"  Date range: {date_start} to {date_end}")
    print(f"  Min PA filter: {min_pa}")
    print(f"{'=' * 64}\n")

    # ------------------------------------------------------------------
    # 1) Season-long Savant leaderboard data
    # ------------------------------------------------------------------
    print("[1/6] Fetching season-long Savant leaderboard data...")
    xba_df = fetch_savant_expected_stats(season, min_pa=min_pa)
    barrel_df = fetch_savant_barrel_hardhit(season)
    bat_speed_df = fetch_savant_bat_speed(season)

    # ------------------------------------------------------------------
    # 2) FanGraphs stats (K% via pybaseball)
    # ------------------------------------------------------------------
    print("\n[2/6] Fetching FanGraphs data...")
    krate_df = fetch_fangraphs_season_stats(season, min_pa=min_pa)

    # ------------------------------------------------------------------
    # 3) Exit velocity buckets
    # ------------------------------------------------------------------
    if skip_exit_velo:
        print("\n[3/6] Skipping exit-velocity buckets (--skip-exit-velo).")
        ev_df = pd.DataFrame(
            columns=["MLBAMID", "EV_105_110", "EV_110_115", "EV_115+"]
        )
    else:
        print("\n[3/6] Fetching exit-velocity buckets from Statcast search...")
        ev_df = fetch_exit_velo_buckets(season)

    # ------------------------------------------------------------------
    # 4) Date-range stats (all from Statcast search)
    # ------------------------------------------------------------------
    if skip_date_range:
        print(f"\n[4/6] Skipping date-range stats (--skip-date-range).")
        dr_df = pd.DataFrame(columns=[
            "MLBAMID", "DR_PA", "DR_HR", "DR_K%",
            "DR_xBA", "DR_Barrel%", "DR_HardHit%",
        ])
    else:
        print(f"\n[4/6] Fetching date-range stats ({date_start} to "
              f"{date_end})...")
        dr_df = fetch_savant_date_range_stats(
            season, date_start, date_end
        )

    # ------------------------------------------------------------------
    # 5) FanGraphs auction CSV + EVAnalytics
    # ------------------------------------------------------------------
    print("\n[5/6] Loading supplemental data...")
    auction_df = load_fangraphs_auction_csv(fg_csv) if fg_csv else None
    if auction_df is None and not fg_csv:
        print("  Tip: pass --fg-csv <path> for BatX auction values.")
        print("  Export from: https://www.fangraphs.com/fantasy-tools/auction-calculator")

    eva_df = fetch_evanalytics_rankings()

    # ------------------------------------------------------------------
    # 6) Merge everything
    # ------------------------------------------------------------------
    print(f"\n[6/6] Merging all data sources...")

    # Start with expected-stats (has PA, xBA, MLBAMID)
    dash = xba_df.copy()

    # Barrel% + HardHit%
    dash = dash.merge(
        barrel_df[["MLBAMID", "Barrel%", "HardHit%"]],
        on="MLBAMID", how="left",
    )

    # K%
    dash = dash.merge(
        krate_df[["MLBAMID", "K%"]], on="MLBAMID", how="left",
    )

    # Avg bat speed
    dash = dash.merge(
        bat_speed_df[["MLBAMID", "AvgBatSpeed"]], on="MLBAMID", how="left",
    )

    # Exit-velocity buckets
    if not ev_df.empty:
        dash = dash.merge(ev_df, on="MLBAMID", how="left")
        for col in ["EV_105_110", "EV_110_115", "EV_115+"]:
            if col in dash.columns:
                dash[col] = dash[col].fillna(0).astype(int)

    # Date-range stats (PA, HR, K%, xBA, barrel%, hard-hit%)
    if not dr_df.empty:
        dash = dash.merge(dr_df, on="MLBAMID", how="left")

    # FanGraphs auction values (from CSV export)
    if auction_df is not None and "FG_AuctionValue" in auction_df.columns:
        if "MLBAMID" in auction_df.columns:
            ac = auction_df[["MLBAMID", "FG_AuctionValue"]].dropna(
                subset=["MLBAMID"]
            )
            ac["MLBAMID"] = ac["MLBAMID"].astype(int)
            dash = dash.merge(ac, on="MLBAMID", how="left")
        else:
            dash["name_norm"] = dash["Name"].apply(normalize_name)
            ac = auction_df[["name_norm", "FG_AuctionValue"]].copy()
            dash = dash.merge(ac, on="name_norm", how="left")
            dash.drop(columns=["name_norm"], inplace=True)

    # EVAnalytics rankings
    if eva_df is not None:
        # The EVAnalytics table has columns: Rank, Hitter, Team
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
            eva_clean.rename(columns={rank_col: "EVA_Rank"}, inplace=True)
            eva_clean["name_norm"] = eva_clean[hitter_col].apply(
                normalize_name
            )
            eva_clean["EVA_Rank"] = pd.to_numeric(
                eva_clean["EVA_Rank"], errors="coerce"
            )

            dash["name_norm"] = dash["Name"].apply(normalize_name)
            dash = dash.merge(
                eva_clean[["name_norm", "EVA_Rank"]],
                on="name_norm", how="left",
            )
            dash.drop(columns=["name_norm"], inplace=True)
            matched = dash["EVA_Rank"].notna().sum()
            print(f"  Matched {matched} players to EVAnalytics rankings.")
        else:
            print(f"  Could not auto-detect EVAnalytics columns: "
                  f"{list(eva_df.columns)}")

    # ------------------------------------------------------------------
    # Reorder columns for readability
    # ------------------------------------------------------------------
    priority_cols = ["Name", "MLBAMID"]
    if "FG_AuctionValue" in dash.columns:
        priority_cols.append("FG_AuctionValue")
    if "EVA_Rank" in dash.columns:
        priority_cols.append("EVA_Rank")

    season_stats = [
        "PA", "xBA", "Barrel%", "HardHit%", "K%",
        "EV_105_110", "EV_110_115", "EV_115+", "AvgBatSpeed",
    ]
    date_range_cols = [
        "DR_PA", "DR_xBA", "DR_HR", "DR_Barrel%", "DR_HardHit%", "DR_K%",
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
    dash.to_csv(output, index=False)
    print(f"\n{'=' * 64}")
    print(f"  Dashboard saved to: {output}")
    print(f"  Players: {len(dash)}")
    print(f"  Columns: {', '.join(dash.columns)}")
    print(f"{'=' * 64}")

    preview_cols = [c for c in ["Name", "PA", "xBA", "Barrel%", "K%",
                                "EV_115+", "AvgBatSpeed", "EVA_Rank"]
                    if c in dash.columns]
    print(f"\nTop 15 by PA:\n")
    print(dash[preview_cols].head(15).to_string(index=False))
    print()

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
        help=f"Minimum plate appearances (default: {DEFAULT_MIN_PA})",
    )
    parser.add_argument(
        "--output", "-o", type=str, default=DEFAULT_OUTPUT,
        help=f"Output CSV path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--date-start", type=str, default=DEFAULT_DATE_RANGE_START,
        help=f"Date-range start (default: {DEFAULT_DATE_RANGE_START})",
    )
    parser.add_argument(
        "--date-end", type=str, default=DEFAULT_DATE_RANGE_END,
        help=f"Date-range end (default: {DEFAULT_DATE_RANGE_END})",
    )
    parser.add_argument(
        "--skip-exit-velo", action="store_true",
        help="Skip exit-velocity bucket fetch (saves ~2 min)",
    )
    parser.add_argument(
        "--skip-date-range", action="store_true",
        help="Skip date-range stats fetch (saves ~2 min)",
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
