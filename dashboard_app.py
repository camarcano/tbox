#!/usr/bin/env python3
"""
Streamlit UI for the Hitter Dashboard.

Run with:
    streamlit run dashboard_app.py
"""

import datetime

import streamlit as st
import pandas as pd

from hitter_dashboard import build_dashboard

st.set_page_config(page_title="Hitter Dashboard", layout="wide")
st.title("Hitter Dashboard")

# ── Sidebar configuration ──────────────────────────────────────────────

with st.sidebar:
    st.header("Configuration")

    season = st.number_input("Season", min_value=2015, max_value=2030,
                             value=2025, step=1)
    min_pa = st.number_input("Minimum PA", min_value=1, max_value=600,
                             value=50, step=10)

    st.subheader("Season-long date window")
    # Season dates are implicitly full-season; these control exit-velo range
    skip_exit_velo = st.checkbox("Skip exit-velo buckets (faster)", value=False)

    st.subheader("Date-range stats window")
    skip_date_range = st.checkbox("Skip date-range stats", value=False)
    dr_start = st.date_input("Start date",
                             value=datetime.date(season, 8, 1),
                             min_value=datetime.date(season, 3, 1),
                             max_value=datetime.date(season, 11, 30))
    dr_end = st.date_input("End date",
                           value=datetime.date(season, 10, 1),
                           min_value=datetime.date(season, 3, 1),
                           max_value=datetime.date(season, 11, 30))

    st.subheader("FanGraphs auction CSV")
    fg_file = st.file_uploader("Upload FG auction export", type=["csv"])

    fg_csv_path = None
    if fg_file is not None:
        fg_csv_path = "/tmp/_fg_auction_upload.csv"
        with open(fg_csv_path, "wb") as f:
            f.write(fg_file.getvalue())

    fetch_btn = st.button("Fetch Dashboard", type="primary",
                          use_container_width=True)

# ── Main area ───────────────────────────────────────────────────────────

if fetch_btn:
    progress = st.empty()
    log_area = st.expander("Fetch log", expanded=True)
    log_lines = []

    def ui_log(msg):
        log_lines.append(str(msg))
        with log_area:
            st.text("\n".join(log_lines))

    with st.spinner("Fetching data..."):
        try:
            df = build_dashboard(
                season=int(season),
                fg_csv=fg_csv_path,
                min_pa=int(min_pa),
                output=None,  # don't write CSV automatically
                date_start=dr_start.isoformat(),
                date_end=dr_end.isoformat(),
                skip_exit_velo=skip_exit_velo,
                skip_date_range=skip_date_range,
                log=ui_log,
            )
            st.session_state["dashboard"] = df
            st.success(f"Done — {len(df)} players loaded.")
        except Exception as exc:
            st.error(f"Error: {exc}")

# Show results if available
if "dashboard" in st.session_state:
    df = st.session_state["dashboard"]

    st.subheader(f"Dashboard ({len(df)} players)")
    st.dataframe(df, use_container_width=True, height=600)

    # Download button
    csv_bytes = df.to_csv(index=False).encode("utf-8")
    st.download_button(
        label="Download CSV",
        data=csv_bytes,
        file_name="hitter_dashboard.csv",
        mime="text/csv",
    )
