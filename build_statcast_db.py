#!/usr/bin/env python3
"""
Build a local SQLite database of all Statcast pitch events for a season.

Downloads pitch-level data from Baseball Savant day-by-day and stores
it in a SQLite database. Supports incremental updates (resume from
where it left off).

Usage:
    python build_statcast_db.py --season 2025
    python build_statcast_db.py --season 2025 --db data/statcast_2025.db
    python build_statcast_db.py --season 2025 --update

The resulting DB is used by hitter_dashboard.py for instant EV bucket
queries instead of making hundreds of HTTP requests.
"""

import argparse
import datetime
import os
import sqlite3
import time

import pandas as pd

from hitter_dashboard import _statcast_search_csv

# -------------------------------------------------------------------
# Constants
# -------------------------------------------------------------------

DEFAULT_DB_DIR = "data"
SEASON_START_MONTH_DAY = (3, 20)  # Spring training / early season
SEASON_END_MONTH_DAY = (11, 5)    # Post-season buffer

SLEEP_BETWEEN_REQUESTS = 1.5  # seconds


# -------------------------------------------------------------------
# Database setup
# -------------------------------------------------------------------

def get_db_path(season, db_path=None):
    """Return the database file path for a given season."""
    if db_path:
        return db_path
    os.makedirs(DEFAULT_DB_DIR, exist_ok=True)
    return os.path.join(DEFAULT_DB_DIR, f"statcast_{season}.db")


def init_db(conn):
    """Create tables and indexes if they don't exist."""
    cur = conn.cursor()

    # Metadata table for tracking progress
    cur.execute("""
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    # Pitches table - created dynamically from first CSV batch
    # (column set varies slightly by season)
    # We check if it exists and create indexes after first insert.

    conn.commit()


def ensure_pitches_table(conn, columns):
    """Create the pitches table from the CSV column names.

    Uses TEXT type for all columns to avoid type mismatches,
    then casts at query time. The primary key handles dedup.
    """
    cur = conn.cursor()

    # Check if table already exists
    cur.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='pitches'"
    )
    if cur.fetchone():
        return  # Already exists

    # Build CREATE TABLE with all columns as TEXT,
    # plus a composite unique constraint for dedup.
    col_defs = []
    for col in columns:
        safe_col = f'"{col}"'
        col_defs.append(f"{safe_col} TEXT")

    create_sql = (
        "CREATE TABLE pitches (\n"
        + ",\n".join(col_defs)
        + ")"
    )
    cur.execute(create_sql)

    # Create indexes for common queries
    index_cols = [
        ("idx_pitches_batter", "batter"),
        ("idx_pitches_game_date", "game_date"),
        ("idx_pitches_launch_speed", "launch_speed"),
        ("idx_pitches_game_pk", "game_pk"),
    ]
    for idx_name, col in index_cols:
        if col in columns:
            cur.execute(
                f'CREATE INDEX IF NOT EXISTS {idx_name} '
                f'ON pitches("{col}")'
            )

    # Unique index for dedup
    dedup_cols = [
        c for c in
        ["game_pk", "at_bat_number", "pitch_number",
         "batter", "pitcher"]
        if c in columns
    ]
    if len(dedup_cols) >= 3:
        cols_quoted = ", ".join(f'"{c}"' for c in dedup_cols)
        cur.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS idx_pitches_dedup "
            f"ON pitches({cols_quoted})"
        )

    conn.commit()


# -------------------------------------------------------------------
# Metadata helpers
# -------------------------------------------------------------------

def get_meta(conn, key, default=None):
    cur = conn.cursor()
    cur.execute(
        "SELECT value FROM metadata WHERE key = ?", (key,)
    )
    row = cur.fetchone()
    return row[0] if row else default


def set_meta(conn, key, value):
    conn.execute(
        "INSERT OR REPLACE INTO metadata (key, value) "
        "VALUES (?, ?)",
        (key, str(value)),
    )
    conn.commit()


# -------------------------------------------------------------------
# Core: fetch and store
# -------------------------------------------------------------------

def insert_pitches(conn, df):
    """Insert a DataFrame of pitches into the DB.

    Uses INSERT OR IGNORE to skip duplicates (handled by
    the unique index on the dedup columns).
    """
    if df.empty:
        return 0

    columns = list(df.columns)
    placeholders = ", ".join(["?"] * len(columns))
    cols_quoted = ", ".join(f'"{c}"' for c in columns)

    sql = (
        f"INSERT OR IGNORE INTO pitches ({cols_quoted}) "
        f"VALUES ({placeholders})"
    )

    # Convert to list of tuples, replacing NaN with None
    rows = df.where(df.notna(), None).values.tolist()

    cur = conn.cursor()
    cur.executemany(sql, rows)
    conn.commit()
    return cur.rowcount


def build_db(season, db_path=None, log=print):
    """Download all pitch events for a season into SQLite.

    Supports incremental updates: checks metadata for last
    fetched date and resumes from there.
    """
    db_file = get_db_path(season, db_path)
    log(f"Database: {db_file}")
    log(f"Season: {season}")

    conn = sqlite3.connect(db_file)
    init_db(conn)

    # Determine date range
    d_start = datetime.date(
        season, *SEASON_START_MONTH_DAY
    )
    d_end = min(
        datetime.date(season, *SEASON_END_MONTH_DAY),
        datetime.date.today(),
    )

    # Check for resume point
    last_fetched = get_meta(conn, "last_date_fetched")
    if last_fetched:
        resume_date = (
            datetime.date.fromisoformat(last_fetched)
            + datetime.timedelta(days=1)
        )
        if resume_date > d_start:
            d_start = resume_date
            log(f"Resuming from {d_start} "
                f"(last fetched: {last_fetched})")

    if d_start > d_end:
        total = get_meta(conn, "total_pitches", "0")
        log(f"Database is up to date. "
            f"Total pitches: {total}")
        conn.close()
        return db_file

    # Count existing pitches
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM pitches")
        total_pitches = cur.fetchone()[0]
    except sqlite3.OperationalError:
        total_pitches = 0

    log(f"Fetching {d_start} to {d_end} "
        f"({(d_end - d_start).days + 1} days)...")
    log(f"Existing pitches in DB: {total_pitches}")

    table_created = total_pitches > 0
    current = d_start
    days_fetched = 0
    errors = 0

    while current <= d_end:
        date_str = current.isoformat()
        # Use same date for start and end to get exactly
        # one day of data
        log(f"  {date_str}...", flush=True)

        try:
            df = _statcast_search_csv(
                season, date_str, date_str
            )

            if df.empty:
                log(f"  {date_str}: no data")
            else:
                # Create table on first successful fetch
                if not table_created:
                    ensure_pitches_table(conn, list(df.columns))
                    table_created = True

                inserted = insert_pitches(conn, df)
                total_pitches += inserted
                log(f"  {date_str}: {len(df)} pitches "
                    f"({inserted} new, "
                    f"total: {total_pitches:,})")

            # Update resume point
            set_meta(conn, "last_date_fetched", date_str)
            set_meta(conn, "season", str(season))
            set_meta(conn, "total_pitches", str(total_pitches))
            errors = 0  # Reset error counter on success

        except Exception as e:
            errors += 1
            log(f"  {date_str}: ERROR - {e}")
            if errors >= 5:
                log("  Too many consecutive errors, stopping.")
                break

        days_fetched += 1
        current += datetime.timedelta(days=1)

        # Rate limiting
        if current <= d_end:
            time.sleep(SLEEP_BETWEEN_REQUESTS)

    log(f"\nDone! {days_fetched} days fetched.")
    log(f"Total pitches in DB: {total_pitches:,}")
    log(f"Database: {db_file}")

    conn.close()
    return db_file


# -------------------------------------------------------------------
# CLI
# -------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Build Statcast pitch database from "
                    "Baseball Savant",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--season", type=int, default=2025,
        help="MLB season year (default: 2025)",
    )
    parser.add_argument(
        "--db", type=str, default=None,
        help="Database file path "
             "(default: data/statcast_<season>.db)",
    )
    parser.add_argument(
        "--update", action="store_true",
        help="Only fetch new days since last run",
    )

    args = parser.parse_args()

    build_db(
        season=args.season,
        db_path=args.db,
        log=print,
    )


if __name__ == "__main__":
    main()
