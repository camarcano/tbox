#!/usr/bin/env python3
"""
Scouting Report Generator

Generates Inside Edge-style scouting reports from the Statcast pitch database.
Includes zone charts, spray charts, pitch type performance, and by-count breakdowns.
"""

import os
import sqlite3

import numpy as np
import pandas as pd

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# Pitch type classification
# ---------------------------------------------------------------------------

PITCH_TYPE_MAP = {
    'FF': '4sm/2sm FB', 'FA': '4sm/2sm FB', 'FT': '4sm/2sm FB',
    'SI': 'Sinker',
    'FC': 'Cutter',
    'CU': 'Curve', 'KC': 'Curve', 'CS': 'Curve',
    'SL': 'Slider', 'ST': 'Slider', 'SV': 'Slider',
    'CH': 'Changeup',
    'KN': 'Knuckleball',
    'FS': 'Split/Fork', 'FO': 'Split/Fork',
    'SC': 'Screwball',
    'EP': 'Eephus',
}

FASTBALL_CODES = {'FF', 'FA', 'FT', 'SI'}

PITCH_TYPE_ORDER = [
    '4sm/2sm FB', 'Sinker', 'Cutter', 'Curve', 'Slider',
    'Changeup', 'Knuckleball', 'Split/Fork', 'Screwball', 'Eephus',
]

HIT_EVENTS = {'single', 'double', 'triple', 'home_run'}

NON_AB_EVENTS = {
    'walk', 'intent_walk', 'hit_by_pitch',
    'sac_fly', 'sac_bunt', 'sac_fly_double_play',
    'sac_bunt_double_play', 'catcher_interf',
}

SWING_DESCRIPTIONS = {
    'swinging_strike', 'swinging_strike_blocked',
    'foul', 'foul_tip', 'foul_bunt', 'missed_bunt', 'bunt_foul_tip',
    'hit_into_play', 'hit_into_play_no_out', 'hit_into_play_score',
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_db_path(season):
    return os.path.join(_BASE_DIR, "data", f"statcast_{season}.db")


def _ba(hits, ab):
    if ab == 0:
        return None
    return round(hits / ab, 3)


def _slg(tb, ab):
    if ab == 0:
        return None
    return round(tb / ab, 3)


def _pct(num, denom):
    if denom == 0:
        return None
    return round(100 * num / denom, 1)


def _total_bases_from_events(events):
    tb = 0
    for ev in events:
        if ev == 'single':
            tb += 1
        elif ev == 'double':
            tb += 2
        elif ev == 'triple':
            tb += 3
        elif ev == 'home_run':
            tb += 4
    return tb


def _is_ab(event):
    if not event or not isinstance(event, str) or event == '':
        return False
    for non_ab in NON_AB_EVENTS:
        if non_ab in event:
            return False
    return True


def _is_hit(event):
    return isinstance(event, str) and event in HIT_EVENTS


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def generate_scouting_report(season, batter_id, p_throws='ALL',
                              start_date=None, end_date=None):
    """Generate a full scouting report for a batter."""
    db_path = _get_db_path(season)
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"No Statcast DB for {season}")

    if not start_date:
        start_date = f"{season}-03-20"
    if not end_date:
        end_date = f"{season}-11-01"

    conn = sqlite3.connect(db_path)

    query = """
        SELECT
            pitch_type, zone,
            CAST(NULLIF(balls, '') AS INTEGER) AS balls,
            CAST(NULLIF(strikes, '') AS INTEGER) AS strikes,
            p_throws, stand, type, description, events,
            CAST(NULLIF(launch_speed, '') AS REAL) AS launch_speed,
            CAST(NULLIF(launch_angle, '') AS REAL) AS launch_angle,
            CAST(NULLIF(hc_x, '') AS REAL) AS hc_x,
            CAST(NULLIF(hc_y, '') AS REAL) AS hc_y,
            bb_type,
            CASE WHEN (on_2b IS NOT NULL AND on_2b != '')
                   OR (on_3b IS NOT NULL AND on_3b != '')
                 THEN 1 ELSE 0 END AS is_risp,
            game_date
        FROM pitches
        WHERE CAST(batter AS INTEGER) = ?
          AND game_date >= ? AND game_date <= ?
    """
    df = pd.read_sql_query(query, conn, params=[batter_id, start_date, end_date])

    # Get player name
    name_q = "SELECT DISTINCT player_name FROM pitches WHERE CAST(batter AS INTEGER) = ? LIMIT 1"
    name_df = pd.read_sql_query(name_q, conn, params=[batter_id])
    conn.close()

    if df.empty:
        raise ValueError(f"No pitches found for batter {batter_id} in {season}")

    player_name = name_df.iloc[0]['player_name'] if not name_df.empty else str(batter_id)

    # Ensure balls/strikes are integers (SQLite may store as text)
    df['balls'] = pd.to_numeric(df['balls'], errors='coerce').fillna(0).astype(int)
    df['strikes'] = pd.to_numeric(df['strikes'], errors='coerce').fillna(0).astype(int)

    # Filter by pitcher hand
    if p_throws and p_throws != 'ALL':
        df = df[df['p_throws'] == p_throws]
        if df.empty:
            raise ValueError(f"No pitches found vs {p_throws}HP")

    bats = df['stand'].mode().iloc[0] if not df['stand'].mode().empty else 'R'

    # Classify pitches
    df['pitch_group'] = df['pitch_type'].map(PITCH_TYPE_MAP).fillna('Other')
    df['is_fastball'] = df['pitch_type'].isin(FASTBALL_CODES)
    df['has_event'] = df['events'].notna() & (df['events'] != '')
    df['is_ab'] = df['events'].apply(_is_ab)
    df['is_hit'] = df['events'].apply(_is_hit)
    df['is_swing'] = df['description'].isin(SWING_DESCRIPTIONS)
    df['zone_int'] = pd.to_numeric(df['zone'], errors='coerce')

    return {
        'player': {
            'name': player_name,
            'mlbamid': int(batter_id),
            'bats': bats,
            'vs': p_throws if p_throws != 'ALL' else 'All',
        },
        'summary': _compute_summary(df),
        'zone_fb': _compute_zone_chart(df[df['is_fastball']]),
        'zone_other': _compute_zone_chart(df[~df['is_fastball']]),
        'spray': _compute_spray_chart(df, bats),
        'pitch_type_table': _compute_pitch_type_table(df),
        'by_count': _compute_by_count(df),
    }


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def _compute_summary(df):
    total_pitches = len(df)
    pa_df = df[df['has_event']]
    total_pa = len(pa_df)
    ab_df = pa_df[pa_df['is_ab']]
    total_ab = len(ab_df)
    hits = int(pa_df['is_hit'].sum())

    k_events = int(pa_df['events'].isin(['strikeout', 'strikeout_double_play']).sum())
    bb_events = int(pa_df['events'].str.contains('walk', na=False).sum())

    batted = df[df['bb_type'].notna() & (df['bb_type'] != '')]
    total_batted = len(batted)
    gb = int((batted['bb_type'] == 'ground_ball').sum())
    fb = int(((batted['bb_type'] == 'fly_ball') | (batted['bb_type'] == 'popup')).sum())
    ld = int((batted['bb_type'] == 'line_drive').sum())

    tb = _total_bases_from_events(ab_df['events'].dropna())

    return {
        'pitches_charted': total_pitches,
        'pa': total_pa,
        'ab': total_ab,
        'hits': hits,
        'ba': _ba(hits, total_ab),
        'slg': _slg(tb, total_ab),
        'k_pct': _pct(k_events, total_pa),
        'bb_pct': _pct(bb_events, total_pa),
        'gb_pct': _pct(gb, total_batted),
        'fb_pct': _pct(fb, total_batted),
        'ld_pct': _pct(ld, total_batted),
    }


# ---------------------------------------------------------------------------
# Zone chart
# ---------------------------------------------------------------------------

def _compute_zone_chart(df):
    total_pitches = len(df)
    if total_pitches == 0:
        return {'zones': {}, 'row_pcts': {}, 'col_pcts': {}, 'total_pitches': 0}

    zones = {}
    for z in range(1, 10):
        zone_df = df[df['zone_int'] == z]
        zone_pa = zone_df[zone_df['has_event']]
        zone_ab = zone_pa[zone_pa['is_ab']]

        ab_count = len(zone_ab)
        hit_count = int(zone_pa['is_hit'].sum())
        tb = _total_bases_from_events(zone_ab['events'].dropna())

        zones[str(z)] = {
            'ba': _ba(hit_count, ab_count),
            'hits': hit_count,
            'ab': ab_count,
            'slg': _slg(tb, ab_count),
            'pitches': len(zone_df),
            'pct': _pct(len(zone_df), total_pitches),
        }

    row_pcts = {}
    for row, zone_ids in enumerate([('1', '2', '3'), ('4', '5', '6'), ('7', '8', '9')]):
        row_pitches = sum(zones[z]['pitches'] for z in zone_ids)
        row_pcts[str(row)] = _pct(row_pitches, total_pitches)

    col_pcts = {}
    for col, zone_ids in enumerate([('1', '4', '7'), ('2', '5', '8'), ('3', '6', '9')]):
        col_pitches = sum(zones[z]['pitches'] for z in zone_ids)
        col_pcts[str(col)] = _pct(col_pitches, total_pitches)

    return {
        'zones': zones,
        'row_pcts': row_pcts,
        'col_pcts': col_pcts,
        'total_pitches': total_pitches,
    }


# ---------------------------------------------------------------------------
# Spray chart
# ---------------------------------------------------------------------------

def _compute_spray_chart(df, bats):
    # Only batted ball events with valid hit coordinates
    batted = df[
        (df['type'] == 'X') &
        (df['hc_x'].notna()) & (df['hc_y'].notna()) &
        (df['hc_x'] != 0) & (df['hc_y'] != 0)
    ].copy()
    if batted.empty:
        return {'sections': {}, 'total': 0, 'bats': bats}

    HP_X, HP_Y = 125.42, 198.27
    dx = batted['hc_x'].values - HP_X
    dy = HP_Y - batted['hc_y'].values
    batted['angle'] = np.degrees(np.arctan2(dx, dy))
    batted['distance'] = np.sqrt(dx ** 2 + dy ** 2)

    INFIELD_THRESHOLD = 110

    # Vectorized classification
    angles = batted['angle'].values
    if bats == 'R':
        batted['direction'] = np.where(
            angles > 15, 'pull', np.where(angles < -15, 'oppo', 'center'))
    else:
        batted['direction'] = np.where(
            angles < -15, 'pull', np.where(angles > 15, 'oppo', 'center'))
    batted['depth'] = np.where(
        batted['distance'].values < INFIELD_THRESHOLD, 'infield', 'outfield')

    total = len(batted)
    sections = {}
    for direction in ['pull', 'center', 'oppo']:
        for depth in ['infield', 'outfield']:
            count = int(((batted['direction'] == direction) & (batted['depth'] == depth)).sum())
            sections[f"{direction}_{depth}"] = {
                'count': count,
                'pct': _pct(count, total),
            }

    return {'sections': sections, 'total': total, 'bats': bats}


# ---------------------------------------------------------------------------
# Pitch type performance table
# ---------------------------------------------------------------------------

def _situation_stats(subset):
    pa_df = subset[subset['has_event']]
    ab_df = pa_df[pa_df['is_ab']]
    hits = int(ab_df['is_hit'].sum())
    ab_n = len(ab_df)
    tb = _total_bases_from_events(ab_df['events'].dropna())
    return {'ba': _ba(hits, ab_n), 'hits': hits, 'ab': ab_n, 'slg': _slg(tb, ab_n)}


def _compute_pitch_type_table(df):
    results = []
    for pt_name in PITCH_TYPE_ORDER:
        pt_df = df[df['pitch_group'] == pt_name]
        if pt_df.empty:
            continue

        all_counts = _situation_stats(pt_df)
        if all_counts['ab'] == 0:
            continue

        first = pt_df[(pt_df['balls'] == 0) & (pt_df['strikes'] == 0)]
        early = pt_df[
            ((pt_df['balls'] == 0) & (pt_df['strikes'] == 0)) |
            ((pt_df['balls'] == 1) & (pt_df['strikes'] == 0)) |
            ((pt_df['balls'] == 0) & (pt_df['strikes'] == 1))
        ]
        two_k = pt_df[pt_df['strikes'] == 2]
        ahead = pt_df[pt_df['balls'] > pt_df['strikes']]
        behind = pt_df[pt_df['strikes'] > pt_df['balls']]
        risp = pt_df[pt_df['is_risp'] == 1]

        # Chase%
        oz = pt_df[pt_df['zone_int'] > 9]
        oz_swings = int(oz['is_swing'].sum())
        chase = _pct(oz_swings, len(oz))

        # Take% on strikes in zone
        iz = pt_df[pt_df['zone_int'].between(1, 9)]
        called_str = int((iz['description'] == 'called_strike').sum())
        iz_total = len(iz)
        take = _pct(called_str, iz_total)

        results.append({
            'pitch_type': pt_name,
            'all_counts': all_counts,
            'first_pitch': _situation_stats(first),
            'early_counts': _situation_stats(early),
            'two_strikes': _situation_stats(two_k),
            'hitter_ahead': _situation_stats(ahead),
            'hitter_behind': _situation_stats(behind),
            'with_risp': _situation_stats(risp),
            'chase_pct': chase,
            'take_pct': take,
            'total_pitches': len(pt_df),
        })

    return results


# ---------------------------------------------------------------------------
# By-count table
# ---------------------------------------------------------------------------

def _compute_by_count(df):
    counts = [
        (0, 0), (0, 1), (0, 2),
        (1, 0), (1, 1), (1, 2),
        (2, 0), (2, 1), (2, 2),
        (3, 0), (3, 1), (3, 2),
    ]

    result = {}
    totals = dict(swings=0, pitches=0, fb_hits=0, fb_ab=0,
                  other_hits=0, other_ab=0, tb=0, ab=0, h=0)

    for b, s in counts:
        key = f"{b}-{s}"
        cdf = df[(df['balls'] == b) & (df['strikes'] == s)]

        total_pitches = len(cdf)
        swings = int(cdf['is_swing'].sum())

        pa_df = cdf[cdf['has_event']]
        ab_df = pa_df[pa_df['is_ab']]
        hits = int(ab_df['is_hit'].sum())
        ab_n = len(ab_df)
        tb = _total_bases_from_events(ab_df['events'].dropna())

        fb = cdf[cdf['is_fastball']]
        fb_pa = fb[fb['has_event']]
        fb_ab = fb_pa[fb_pa['is_ab']]
        fb_hits = int(fb_ab['is_hit'].sum())
        fb_ab_n = len(fb_ab)

        ot = cdf[~cdf['is_fastball']]
        ot_pa = ot[ot['has_event']]
        ot_ab = ot_pa[ot_pa['is_ab']]
        ot_hits = int(ot_ab['is_hit'].sum())
        ot_ab_n = len(ot_ab)

        result[key] = {
            'swing_pct': _pct(swings, total_pitches),
            'swing_pitches': f"{swings}/{total_pitches}",
            'ba_fb': _ba(fb_hits, fb_ab_n),
            'ba_other': _ba(ot_hits, ot_ab_n),
            'slg': _slg(tb, ab_n),
            'ab': ab_n,
            'h': hits,
        }

        totals['swings'] += swings
        totals['pitches'] += total_pitches
        totals['fb_hits'] += fb_hits
        totals['fb_ab'] += fb_ab_n
        totals['other_hits'] += ot_hits
        totals['other_ab'] += ot_ab_n
        totals['tb'] += tb
        totals['ab'] += ab_n
        totals['h'] += hits

    result['all'] = {
        'swing_pct': _pct(totals['swings'], totals['pitches']),
        'swing_pitches': f"{totals['swings']}/{totals['pitches']}",
        'ba_fb': _ba(totals['fb_hits'], totals['fb_ab']),
        'ba_other': _ba(totals['other_hits'], totals['other_ab']),
        'slg': _slg(totals['tb'], totals['ab']),
        'ab': totals['ab'],
        'h': totals['h'],
    }

    return result


# ---------------------------------------------------------------------------
# Player search (DB-based)
# ---------------------------------------------------------------------------

def search_players_in_db(season, query_str, limit=20):
    """Search for batters in the Statcast DB by name."""
    db_path = _get_db_path(season)
    if not os.path.exists(db_path):
        return []

    conn = sqlite3.connect(db_path)
    q = """
        SELECT DISTINCT CAST(batter AS INTEGER) AS mlbamid, player_name AS name
        FROM pitches
        WHERE player_name LIKE ?
        LIMIT ?
    """
    df = pd.read_sql_query(q, conn, params=[f"%{query_str}%", limit])
    conn.close()

    return df.to_dict('records')
