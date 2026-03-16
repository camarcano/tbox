#!/usr/bin/env python3
"""
Flask API for Hitter Dashboard.

Endpoints:
  POST /api/dashboard/fetch - Fetch dashboard data with config
  GET /api/progress/stream - Server-sent events for progress updates
  GET /api/players/search - Fuzzy search players
  GET /api/download/csv - Download dashboard CSV
  GET /api/download/excel - Download dashboard Excel
  GET / - Serve index.html
"""

import json
import os
import queue
import threading
import traceback
import uuid
from io import BytesIO
from datetime import datetime

from flask import (Flask, Response, request, send_file, jsonify,
                    stream_with_context)
from openpyxl import Workbook
from openpyxl.utils.dataframe import dataframe_to_rows

from hitter_dashboard import build_dashboard
from pitcher_dashboard import build_pitcher_dashboard
from scouting_report import generate_scouting_report, search_players_in_db
try:
    from scouting_pdf import generate_scouting_pdf
except ImportError:
    generate_scouting_pdf = None
from build_statcast_db import build_db, get_db_path
from player_mapper import PlayerMapper

# ─────────────────────────────────────────────────────────────────────────────
# Flask Setup
# ─────────────────────────────────────────────────────────────────────────────

app = Flask(__name__, template_folder=".", static_folder="static")
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB max upload

# Session storage (in-memory cache)
sessions = {}

# Active fetch jobs: job_id -> ProgressLog
active_jobs = {}

# Player mapper for fuzzy search
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
player_mapper = PlayerMapper(os.path.join(_BASE_DIR, "SFBB Player ID Map - PLAYERIDMAP.csv"))

# ─────────────────────────────────────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────────────────────────────────────


class ProgressLog:
    """Thread-safe progress logger that feeds SSE clients."""

    def __init__(self):
        self.messages = []
        self.q = queue.Queue()
        self.done = False
        self.error = None

    def __call__(self, msg, **kwargs):
        """Log a message. Accepts extra kwargs for compat."""
        text = str(msg).strip()
        if not text:
            return
        self.messages.append(text)
        self.q.put(text)
        print(text, flush=True)

    def finish(self, error=None):
        self.error = error
        self.done = True
        self.q.put(None)  # sentinel


def create_session_id():
    return str(uuid.uuid4())


def df_to_json_records(df):
    """Convert DataFrame to list of dicts, properly handling NaN -> null."""
    return json.loads(df.to_json(orient="records"))


def save_fg_csv(uploaded_file):
    """Save uploaded FG CSV to temp location."""
    if not uploaded_file:
        return None
    import tempfile
    temp_dir = tempfile.gettempdir()
    temp_path = os.path.join(temp_dir, f"_fg_auction_{uuid.uuid4()}.csv")
    uploaded_file.save(temp_path)
    return temp_path


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────


@app.route("/")
def index():
    """Serve hitter dashboard page."""
    html_path = os.path.join(os.path.dirname(__file__), "static", "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()
    return Response(html, mimetype="text/html; charset=utf-8")


@app.route("/pitchers")
def pitchers():
    """Serve pitcher dashboard page."""
    html_path = os.path.join(os.path.dirname(__file__), "static", "pitcher.html")
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()
    return Response(html, mimetype="text/html; charset=utf-8")


@app.route("/api/dashboard/fetch", methods=["POST"])
def fetch_dashboard():
    """
    Start a dashboard fetch job (runs in background thread).
    Accepts JSON or multipart/form-data (when FG CSV is uploaded).
    """
    # Parse params from either JSON or form-data
    if request.content_type and "multipart/form-data" in request.content_type:
        data = request.form
    else:
        data = request.get_json() or {}

    season = int(data.get("season", 2025))
    min_pa = int(data.get("min_pa", 50))
    date_start = data.get("date_start", "2025-08-01")
    date_end = data.get("date_end", "2025-10-01")
    skip_exit_velo = data.get("skip_exit_velo", False)
    skip_date_range = data.get("skip_date_range", False)

    # Handle string booleans from form-data
    if isinstance(skip_exit_velo, str):
        skip_exit_velo = skip_exit_velo.lower() in ("true", "1")
    if isinstance(skip_date_range, str):
        skip_date_range = skip_date_range.lower() in ("true", "1")

    fg_csv_path = None
    if "fg_csv" in request.files and request.files["fg_csv"].filename:
        fg_csv_path = save_fg_csv(request.files["fg_csv"])

    job_id = create_session_id()
    progress = ProgressLog()
    active_jobs[job_id] = progress

    def run_fetch():
        try:
            df = build_dashboard(
                season=season,
                fg_csv=fg_csv_path,
                min_pa=min_pa,
                output=None,
                date_start=date_start,
                date_end=date_end,
                skip_exit_velo=skip_exit_velo,
                skip_date_range=skip_date_range,
                log=progress,
            )
            sessions[job_id] = {
                "df": df,
                "created": datetime.now(),
                "config": {
                    "season": season,
                    "min_pa": min_pa,
                    "date_start": date_start,
                    "date_end": date_end,
                },
            }
            progress.finish()
        except Exception as e:
            traceback.print_exc()
            progress.finish(error=str(e))

    thread = threading.Thread(target=run_fetch, daemon=True)
    thread.start()

    return jsonify({"job_id": job_id, "status": "started"})


@app.route("/api/progress/<job_id>")
def progress_stream(job_id):
    """Server-sent events endpoint for real-time progress."""
    progress = active_jobs.get(job_id)
    if not progress:
        return jsonify({"error": "Job not found"}), 404

    @stream_with_context
    def generate():
        print(f"[SSE] Client connected for job {job_id}", flush=True)
        while True:
            try:
                msg = progress.q.get(timeout=30)
            except queue.Empty:
                print("[SSE] keep-alive", flush=True)
                yield ":\n\n"
                continue

            if msg is None:
                if progress.error:
                    evt = {"type": "error",
                           "message": progress.error}
                elif job_id in sessions:
                    df = sessions[job_id]["df"]
                    evt = {
                        "type": "done",
                        "session_id": job_id,
                        "total_players": len(df),
                        "columns": list(df.columns),
                        "data": df_to_json_records(df),
                    }
                else:
                    evt = {"type": "error", "message": "No data"}
                yield f"data: {json.dumps(evt)}\n\n"
                return
            else:
                evt = {"type": "log", "message": msg}
                yield f"data: {json.dumps(evt)}\n\n"

    resp = Response(
        generate(),
        mimetype="text/event-stream",
    )
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    resp.headers["Connection"] = "keep-alive"
    return resp


@app.route("/api/players/search")
def search_players():
    """Fuzzy search for players."""
    q = request.args.get("q", "")
    threshold = int(request.args.get("threshold", 60))
    limit = int(request.args.get("limit", 20))

    if not q or len(q) < 2:
        return jsonify({"results": []})

    results = player_mapper.lookup_fuzzy(
        q, threshold=threshold, limit=limit
    )
    return jsonify({"results": results})


@app.route("/api/download/csv")
def download_csv():
    """Download dashboard data as CSV."""
    session_id = request.args.get("session_id")
    if session_id not in sessions:
        return jsonify({"error": "Session not found"}), 404

    df = sessions[session_id]["df"]
    csv_bytes = df.to_csv(index=False).encode("utf-8")
    return send_file(
        BytesIO(csv_bytes),
        mimetype="text/csv",
        as_attachment=True,
        download_name="hitter_dashboard.csv",
    )


@app.route("/api/download/excel")
def download_excel():
    """Download dashboard data as Excel."""
    session_id = request.args.get("session_id")
    if session_id not in sessions:
        return jsonify({"error": "Session not found"}), 404

    df = sessions[session_id]["df"]

    wb = Workbook()
    ws = wb.active
    ws.title = "Dashboard"

    for r_idx, row in enumerate(
        dataframe_to_rows(df, index=False, header=True), 1
    ):
        for c_idx, value in enumerate(row, 1):
            ws.cell(row=r_idx, column=c_idx, value=value)

    for column in ws.columns:
        max_length = 0
        column_letter = column[0].column_letter
        for cell in column:
            try:
                if len(str(cell.value)) > max_length:
                    max_length = len(str(cell.value))
            except Exception:
                pass
        adjusted_width = min(max_length + 2, 50)
        ws.column_dimensions[column_letter].width = adjusted_width

    excel_bytes = BytesIO()
    wb.save(excel_bytes)
    excel_bytes.seek(0)

    return send_file(
        excel_bytes,
        mimetype=(
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        ),
        as_attachment=True,
        download_name="hitter_dashboard.xlsx",
    )


@app.route("/api/sessions/<session_id>")
def get_session(session_id):
    """Get session data."""
    if session_id not in sessions:
        return jsonify({"error": "Session not found"}), 404

    session = sessions[session_id]
    df = session["df"]

    return jsonify({
        "session_id": session_id,
        "created": session["created"].isoformat(),
        "config": session["config"],
        "total_players": len(df),
        "columns": list(df.columns),
        "data": df_to_json_records(df),
    })


# ─────────────────────────────────────────────────────────────────────────────
# Statcast DB Builder
# ─────────────────────────────────────────────────────────────────────────────


@app.route("/api/statcast/build", methods=["POST"])
def build_statcast():
    """Start building the Statcast pitch database."""
    data = request.get_json() or {}
    season = int(data.get("season", 2025))

    job_id = create_session_id()
    progress = ProgressLog()
    active_jobs[job_id] = progress

    def run_build():
        try:
            build_db(
                season=season,
                db_path=None,
                log=progress,
            )
            progress.finish()
        except Exception as e:
            traceback.print_exc()
            progress.finish(error=str(e))

    thread = threading.Thread(target=run_build, daemon=True)
    thread.start()

    return jsonify({"job_id": job_id, "status": "started"})


@app.route("/api/statcast/status")
def statcast_status():
    """Check if a Statcast DB exists for a season."""
    season = request.args.get("season", 2025, type=int)
    db_path = get_db_path(season)
    exists = os.path.exists(db_path)
    info = {}
    if exists:
        import sqlite3
        conn = sqlite3.connect(db_path)
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM pitches")
            info["total_pitches"] = cur.fetchone()[0]
            cur.execute(
                "SELECT value FROM metadata "
                "WHERE key='last_date_fetched'"
            )
            row = cur.fetchone()
            if row:
                info["last_date"] = row[0]
        except Exception:
            pass
        conn.close()
    return jsonify({
        "exists": exists,
        "season": season,
        "db_path": db_path,
        **info,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Pitcher Dashboard
# ─────────────────────────────────────────────────────────────────────────────


@app.route("/api/pitcher/fetch", methods=["POST"])
def fetch_pitcher_dashboard():
    """Start a pitcher dashboard fetch job (runs in background thread)."""
    data = request.get_json() or {}

    season   = int(data.get("season", 2025))
    min_bf   = int(data.get("min_bf", 100))
    min_ip   = float(data.get("min_ip", 20))
    h1_start = data.get("h1_start", "2025-04-01")
    h1_end   = data.get("h1_end",   "2025-07-31")
    h2_start = data.get("h2_start", "2025-08-01")
    h2_end   = data.get("h2_end",   "2025-10-01")

    job_id = create_session_id()
    progress = ProgressLog()
    active_jobs[job_id] = progress

    def run_fetch():
        try:
            df = build_pitcher_dashboard(
                season=season,
                min_bf=min_bf,
                min_ip=min_ip,
                h1_start=h1_start,
                h1_end=h1_end,
                h2_start=h2_start,
                h2_end=h2_end,
                log=progress,
            )
            sessions[job_id] = {
                "df": df,
                "created": datetime.now(),
                "config": {
                    "season": season,
                    "min_bf": min_bf,
                    "min_ip": min_ip,
                    "h1_start": h1_start,
                    "h1_end": h1_end,
                    "h2_start": h2_start,
                    "h2_end": h2_end,
                },
            }
            progress.finish()
        except Exception as e:
            traceback.print_exc()
            progress.finish(error=str(e))

    thread = threading.Thread(target=run_fetch, daemon=True)
    thread.start()

    return jsonify({"job_id": job_id, "status": "started"})


# ─────────────────────────────────────────────────────────────────────────────
# Scouting Reports
# ─────────────────────────────────────────────────────────────────────────────


@app.route("/scouting")
def scouting():
    """Serve scouting report page."""
    html_path = os.path.join(
        os.path.dirname(__file__), "static", "scouting.html"
    )
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()
    return Response(html, mimetype="text/html; charset=utf-8")


@app.route("/api/scouting/report", methods=["POST"])
def scouting_report():
    """Generate a scouting report for a batter."""
    data = request.get_json() or {}
    season = int(data.get("season", 2025))
    batter_id = int(data.get("batter_id", 0))
    p_throws = data.get("p_throws", "ALL")
    start_date = data.get("start_date")
    end_date = data.get("end_date")

    if not batter_id:
        return jsonify({"error": "batter_id is required"}), 400

    try:
        report = generate_scouting_report(
            season=season,
            batter_id=batter_id,
            p_throws=p_throws,
            start_date=start_date,
            end_date=end_date,
        )
        return jsonify(report)
    except (FileNotFoundError, ValueError) as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/scouting/pdf", methods=["POST"])
def scouting_pdf():
    """Generate a full 2-page PDF scouting report."""
    data = request.get_json() or {}
    season = int(data.get("season", 2025))
    batter_id = int(data.get("batter_id", 0))
    start_date = data.get("start_date")
    end_date = data.get("end_date")

    if not batter_id:
        return jsonify({"error": "batter_id is required"}), 400

    if generate_scouting_pdf is None:
        return jsonify({"error": "PDF export requires fpdf2: pip install fpdf2"}), 500

    try:
        pdf_buf = generate_scouting_pdf(
            season=season,
            batter_id=batter_id,
            start_date=start_date,
            end_date=end_date,
        )
        return send_file(
            pdf_buf,
            mimetype="application/pdf",
            as_attachment=True,
            download_name=f"scouting_{batter_id}.pdf",
        )
    except (FileNotFoundError, ValueError) as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/scouting/search")
def scouting_search():
    """Search for batters in the Statcast DB."""
    q = request.args.get("q", "")
    season = request.args.get("season", 2025, type=int)
    if not q or len(q) < 2:
        return jsonify({"results": []})
    results = search_players_in_db(season, q, limit=20)
    return jsonify({"results": results})


# ─────────────────────────────────────────────────────────────────────────────
# Error Handlers
# ─────────────────────────────────────────────────────────────────────────────


@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(500)
def internal_error(e):
    return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=5000,
        threaded=True,
        use_debugger=True,
        use_reloader=False,
    )
