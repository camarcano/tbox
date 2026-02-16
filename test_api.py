#!/usr/bin/env python3
"""Quick test of the API endpoints."""

import requests
import time
import subprocess
import sys

# Start Flask app in background
print("Starting Flask app...")
proc = subprocess.Popen(
    [sys.executable, "app.py"],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
)

# Give it time to start
time.sleep(3)

try:
    base_url = "http://localhost:5000"

    # Test 1: Health check (GET /)
    print("\n[TEST 1] GET /")
    resp = requests.get(f"{base_url}/")
    print(f"Status: {resp.status_code}")
    if resp.status_code == 200:
        print("✓ Homepage loads")
    else:
        print("✗ Failed to load homepage")

    # Test 2: Player search API
    print("\n[TEST 2] GET /api/players/search?q=aaron")
    resp = requests.get(f"{base_url}/api/players/search?q=aaron")
    print(f"Status: {resp.status_code}")
    data = resp.json()
    print(f"Results: {len(data.get('results', []))} players found")
    if data.get("results"):
        print(f"  - {data['results'][0]['name']} (score: {data['results'][0]['score']})")
        print("✓ Player search works")
    else:
        print("✗ No players found")

    # Test 3: Fetch dashboard (without FG CSV for speed)
    print("\n[TEST 3] POST /api/dashboard/fetch")
    config = {
        "season": 2025,
        "min_pa": 100,
        "date_start": "2025-09-01",
        "date_end": "2025-09-07",  # Just 1 week for speed
        "skip_exit_velo": True,
        "skip_date_range": False,
    }
    print(f"  Config: season={config['season']}, min_pa={config['min_pa']}")
    print("  (This will take ~2 minutes to fetch from Baseball Savant...)")

    resp = requests.post(f"{base_url}/api/dashboard/fetch", json=config)
    print(f"Status: {resp.status_code}")
    data = resp.json()

    if resp.status_code == 200:
        print(f"  - Total players: {data.get('total_players', 0)}")
        print(f"  - Columns: {len(data.get('columns', []))} stats")
        if data.get("session_id"):
            print(f"  - Session ID: {data['session_id'][:8]}...")
            print("✓ Dashboard fetch successful")
        else:
            print("✗ No session ID returned")
    else:
        print(f"✗ Error: {data.get('message', 'Unknown error')}")

finally:
    # Stop Flask app
    print("\nCleaning up...")
    proc.terminate()
    proc.wait(timeout=5)
    print("Done!")
