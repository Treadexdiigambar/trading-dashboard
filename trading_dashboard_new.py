import streamlit as st
import requests
import pandas as pd
import plotly.graph_objects as go
import time
import json
import os
import csv
import threading
import calendar
import math
from datetime import datetime, date, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# ── IST Time Helper — sab jagah IST use karo ─────────────────
def now_ist():
    """Hamesha IST (India Standard Time) datetime return karo"""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Kolkata"))
    except Exception:
        return datetime.utcnow() + timedelta(hours=5, minutes=30)

# ── OI History — 7 days data save/load ───────────────────────
OI_HISTORY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "oi_history")

def ensure_history_dir():
    os.makedirs(OI_HISTORY_DIR, exist_ok=True)

def cleanup_old_history():
    """2 weeks se purana data delete karo — month end pe automatically"""
    ensure_history_dir()
    cutoff_date = date.today() - timedelta(days=14)
    cleaned = 0
    try:
        for fname in os.listdir(OI_HISTORY_DIR):
            if not fname.endswith("_oi_history.csv"):
                continue
            fpath = os.path.join(OI_HISTORY_DIR, fname)
            rows_kept = []
            try:
                with open(fpath, "r") as f:
                    reader = csv.DictReader(f)
                    fieldnames = reader.fieldnames
                    for row in reader:
                        try:
                            row_date = date.fromisoformat(row["date"])
                            if row_date >= cutoff_date:
                                rows_kept.append(row)
                            else:
                                cleaned += 1
                        except:
                            rows_kept.append(row)
                # Write back
                with open(fpath, "w", newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(rows_kept)
            except Exception as e:
                print(f"[WARN] Cleanup failed for {fname}: {e}")
        if cleaned > 0:
            print(f"[INFO] Auto cleanup: {cleaned} purane records delete kiye (cutoff: {cutoff_date})")
    except Exception as e:
        print(f"[WARN] Cleanup error: {e}")

def get_history_file(instrument_name):
    """e.g. NIFTY_oi_history.csv"""
    safe_name = instrument_name.replace(" ", "_").replace("|", "_")
    return os.path.join(OI_HISTORY_DIR, f"{safe_name}_oi_history.csv")

def save_daily_oi(instrument_name, strike_data, spot, pcr, max_pain):
    """
    Har din 3:15 PM ke baad OI data save karo
    strike_data: {strike: {call_oi, put_oi}}
    """
    ensure_history_dir()
    today_str  = str(date.today())
    hist_file  = get_history_file(instrument_name)

    # Pehle existing data load karo
    existing = {}
    if os.path.exists(hist_file):
        try:
            with open(hist_file, "r") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    existing[row["date"]] = row
        except Exception as e:
            print(f"[WARN] History load failed: {e}")

    # Aaj ka data add/update karo
    # Summary row: date, spot, pcr, max_pain, top5 call strikes, top5 put strikes
    top_calls = sorted(strike_data.items(), key=lambda x: x[1]["call_oi"], reverse=True)[:5]
    top_puts  = sorted(strike_data.items(), key=lambda x: x[1]["put_oi"],  reverse=True)[:5]

    total_call_oi = sum(v["call_oi"] for v in strike_data.values())
    total_put_oi  = sum(v["put_oi"]  for v in strike_data.values())

    existing[today_str] = {
        "date":           today_str,
        "spot":           round(spot, 2) if spot else 0,
        "pcr":            round(pcr, 3)  if pcr  else 0,
        "max_pain":       max_pain or 0,
        "total_call_oi":  total_call_oi,
        "total_put_oi":   total_put_oi,
        "top_call_strike": top_calls[0][0] if top_calls else 0,
        "top_call_oi":     top_calls[0][1]["call_oi"] if top_calls else 0,
        "top_put_strike":  top_puts[0][0]  if top_puts  else 0,
        "top_put_oi":      top_puts[0][1]["put_oi"]   if top_puts  else 0,
    }

    # Sirf last 10 trading days rakho
    sorted_dates = sorted(existing.keys(), reverse=True)[:10]
    final_data   = {d: existing[d] for d in sorted_dates}

    # Save karo
    try:
        fieldnames = ["date","spot","pcr","max_pain","total_call_oi","total_put_oi",
                      "top_call_strike","top_call_oi","top_put_strike","top_put_oi"]
        with open(hist_file, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for d in sorted(final_data.keys(), reverse=True):
                writer.writerow(final_data[d])
        print(f"[INFO] OI history saved: {hist_file}")
    except Exception as e:
        print(f"[WARN] OI history save failed: {e}")

def load_oi_history(instrument_name, days=7):
    """Last N days ka OI history load karo"""
    hist_file = get_history_file(instrument_name)
    if not os.path.exists(hist_file):
        return []
    try:
        rows = []
        with open(hist_file, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)
        return rows[:days]
    except Exception as e:
        print(f"[WARN] OI history load failed: {e}")
        return []

# ── Load .env file if present (local development) ─────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed — use system env vars

# ── API Keys — environment variables se lo (SECURE) ───────────
# Setup: .env file banao project folder mein:
#   UPSTOX_API_KEY=your_api_key_here
#   UPSTOX_SECRET_KEY=your_secret_key_here
API_KEY    = os.environ.get("UPSTOX_API_KEY", "")
SECRET_KEY = os.environ.get("UPSTOX_SECRET_KEY", "")
TOKEN_FILE = "upstox_token.json"

# ── Market hours check ────────────────────────────────────────
def is_market_open():
    """Check karo market abhi khula hai ya nahi (IST 9:15 AM - 3:30 PM, Mon-Fri)"""
    try:
        from zoneinfo import ZoneInfo
        _now = datetime.now(ZoneInfo("Asia/Kolkata"))
    except Exception:
        _now = datetime.utcnow() + timedelta(hours=5, minutes=30)
    if _now.weekday() >= 5:  # Saturday=5, Sunday=6
        return False, "Weekend"
    market_open  = _now.replace(hour=9,  minute=15, second=0, microsecond=0)
    market_close = _now.replace(hour=15, minute=30, second=0, microsecond=0)
    if _now < market_open:
        return False, f"Pre-Market (Opens {market_open.strftime('%I:%M %p')})"
    elif _now > market_close:
        return False, "Market Closed (3:30 PM ke baad)"
    return True, "Market Open"

# ── VIX fetch function ────────────────────────────────────────
def get_india_vix():
    """India VIX NSE se fetch karo"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.nseindia.com/market-data/india-vix",
    }
    try:
        session = requests.Session()
        session.get("https://www.nseindia.com", headers=headers, timeout=3)
        session.get("https://www.nseindia.com/market-data/india-vix", headers=headers, timeout=3)
        ts   = int(time.time() * 1000)
        resp = session.get(
            f"https://www.nseindia.com/api/allIndices?_={ts}",
            headers=headers, timeout=8)
        if resp.status_code == 200:
            data = resp.json().get("data", [])
            for item in data:
                if "VIX" in str(item.get("index", "")).upper():
                    return {
                        "last":    item.get("last",    None),
                        "change":  item.get("variation", item.get("change", None)),
                        "pchange": item.get("percentChange", None),
                        "high":    item.get("high",    None),
                        "low":     item.get("low",     None),
                        "prev":    item.get("previousClose", None),
                    }
    except requests.exceptions.ConnectionError:
        print("[WARN] VIX fetch failed: NSE India blocked/unreachable on this network (DNS error)")
    except requests.exceptions.Timeout:
        print("[WARN] VIX fetch failed: Timeout")
    except Exception as e:
        print(f"[WARN] VIX fetch failed: {e}")
    return None

# ── Safe API call wrapper — internet error handle karo ────────
def safe_api_call(func, *args, fallback=None, **kwargs):
    """Network error pe crash nahi — graceful fallback dega"""
    try:
        return func(*args, **kwargs)
    except requests.exceptions.ConnectionError:
        return fallback
    except requests.exceptions.Timeout:
        return fallback
    except Exception as e:
        print(f"[WARN] API call failed: {e}")
        return fallback

st.set_page_config(page_title="Nifty & Bank Nifty Dashboard", page_icon="📈", layout="wide")

# ── API Key check — startup pe warn karo ──────────────────────
if not API_KEY or not SECRET_KEY:
    st.error("""
    ⚠️ **API Keys nahi mili!**

    Please environment variables set karo:
    ```
    UPSTOX_API_KEY=your_api_key_here
    UPSTOX_SECRET_KEY=your_secret_key_here
    ```
    Ya project folder mein `.env` file banao unhi values ke saath.
    """)
    st.stop()

st.markdown("""
<style>
    /* ══ GOOGLE FONT IMPORT ══ */
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&family=JetBrains+Mono:wght@400;600&display=swap');

    /* ══ BASE & BACKGROUND ══ */
    .main, .stApp,
    [data-testid="stAppViewContainer"] {
        background: radial-gradient(ellipse at 10% 0%, #0d1b2e 0%, #060a12 60%, #020408 100%) !important;
        font-family: 'Inter', sans-serif;
    }

    /* ══ HIDE STREAMLIT TOOLBAR / DEPLOY BAR — overlap fix ══ */
    [data-testid="stHeader"] {
        background: transparent !important;
        height: 0 !important;
        min-height: 0 !important;
        visibility: hidden !important;
    }
    [data-testid="stToolbar"],
    [data-testid="stDecoration"],
    #MainMenu,
    footer,
    header {
        visibility: hidden !important;
        height: 0 !important;
        min-height: 0 !important;
        display: none !important;
    }
    .stAppDeployButton { display: none !important; }
    [data-testid="stStatusWidget"] { display: none !important; }

    .block-container {
        padding-top: 1.4rem;
        padding-bottom: 60px;
        background: transparent;
        max-width: 1400px;
    }

    /* ══ GLOBAL SCROLLBAR ══ */
    ::-webkit-scrollbar { width: 4px; height: 4px; }
    ::-webkit-scrollbar-track { background: #0a0f1a; }
    ::-webkit-scrollbar-thumb { background: #1d4ed8; border-radius: 4px; }

    /* ══ SECTION HEADERS ══ */
    .sec-header {
        background: linear-gradient(90deg, rgba(29,78,216,0.12) 0%, rgba(6,10,18,0) 100%);
        border-radius: 8px;
        padding: 10px 18px;
        margin: 18px 0 12px;
        border-left: 3px solid #1d4ed8;
        color: #a8ccdf;
        font-size: 12px;
        font-weight: 800;
        letter-spacing: 3px;
        text-transform: uppercase;
        backdrop-filter: blur(8px);
    }

    /* ══ SPOT PRICE CARD ══ */
    .spot-card {
        background: linear-gradient(135deg, #0f1e35 0%, #091525 100%);
        border: 1px solid rgba(29,78,216,0.3);
        border-radius: 14px;
        padding: 14px 22px;
        box-shadow: 0 4px 24px rgba(0,0,0,0.5), inset 0 1px 0 rgba(255,255,255,0.04);
        backdrop-filter: blur(12px);
    }
    .spot-number {
        font-family: 'JetBrains Mono', monospace;
        font-size: 46px;
        font-weight: 900;
        color: #e8f4ff;
        line-height: 1;
        letter-spacing: -1px;
        text-shadow: 0 0 30px rgba(0,191,255,0.15);
    }
    .spot-label {
        font-size: 12px;
        color: #8ab8d8;
        letter-spacing: 3px;
        text-transform: uppercase;
        font-weight: 800;
    }
    .spot-change-up   { font-family:'JetBrains Mono',monospace; font-size:17px; font-weight:800; color:#00e676; }
    .spot-change-down { font-family:'JetBrains Mono',monospace; font-size:17px; font-weight:800; color:#ff5252; }

    /* ══ KEY LEVEL CARDS ══ */
    .key-level-support {
        background: linear-gradient(90deg, rgba(0,230,118,0.07) 0%, rgba(0,230,118,0.02) 100%);
        border-left: 3px solid #00e676;
        border-radius: 8px;
        padding: 11px 16px;
        margin: 6px 0;
        color: #00e676;
        font-family: 'JetBrains Mono', monospace;
        font-weight: 800;
        font-size: 20px;
        box-shadow: 0 2px 12px rgba(0,230,118,0.08);
        transition: all 0.2s;
    }
    .key-level-resistance {
        background: linear-gradient(90deg, rgba(255,82,82,0.07) 0%, rgba(255,82,82,0.02) 100%);
        border-left: 3px solid #ff5252;
        border-radius: 8px;
        padding: 11px 16px;
        margin: 6px 0;
        color: #ff5252;
        font-family: 'JetBrains Mono', monospace;
        font-weight: 800;
        font-size: 20px;
        box-shadow: 0 2px 12px rgba(255,82,82,0.08);
    }
    .fair-value {
        background: linear-gradient(135deg, rgba(255,214,0,0.08) 0%, rgba(255,214,0,0.03) 100%);
        border: 1.5px solid rgba(255,214,0,0.5);
        border-radius: 12px;
        padding: 14px;
        color: #ffd600;
        font-family: 'JetBrains Mono', monospace;
        font-weight: 800;
        text-align: center;
        font-size: 20px;
        box-shadow: 0 4px 20px rgba(255,214,0,0.08);
    }

    /* ══ FV TABLE ══ */
    .fv-neutral  { color: #8aa8c4; font-size: 14px; font-weight: 500; }
    .fv-mehnga   { color: #ff5252; font-size: 14px; font-weight: 800; }
    .fv-sasta    { color: #00e676; font-size: 14px; font-weight: 800; }
    .fv-fair     { color: #7aa0be; font-size: 14px; font-weight: 400; }
    .fv-atm-row  { background: rgba(255,214,0,0.07) !important; border-left: 3px solid #ffd600; }

    /* ══ SIGNAL COLORS ══ */
    .bullish { color:#00e676; font-size:22px; font-weight:900; text-shadow: 0 0 12px rgba(0,230,118,0.4); }
    .bearish { color:#ff5252; font-size:22px; font-weight:900; text-shadow: 0 0 12px rgba(255,82,82,0.4); }
    .neutral { color:#ffd600; font-size:22px; font-weight:900; text-shadow: 0 0 12px rgba(255,214,0,0.3); }
    .big-number { font-family:'JetBrains Mono',monospace; font-size:40px; font-weight:900; color:#e8f4ff; }

    /* ══ METRICS ══ */
    div[data-testid="stMetric"] {
        background: linear-gradient(135deg, #0f1e35 0%, #091525 100%);
        border-radius: 12px;
        padding: 16px;
        border: 1px solid rgba(29,78,216,0.2);
        box-shadow: 0 4px 20px rgba(0,0,0,0.4), inset 0 1px 0 rgba(255,255,255,0.04);
        transition: border-color 0.3s;
    }
    div[data-testid="stMetric"]:hover { border-color: rgba(29,78,216,0.5); }
    div[data-testid="stMetric"] label {
        font-size: 12px !important;
        color: #8ab8d8 !important;
        letter-spacing: 2.5px !important;
        text-transform: uppercase !important;
        font-weight: 800 !important;
        font-family: 'Inter', sans-serif !important;
    }
    div[data-testid="stMetric"] div {
        font-family: 'JetBrains Mono', monospace !important;
        font-size: 30px !important;
        font-weight: 900 !important;
        color: #e8f4ff !important;
    }

    /* ══ METRIC CARD (custom) ══ */
    .metric-card {
        background: linear-gradient(135deg, #0f1e35 0%, #091525 100%);
        border-radius: 12px;
        padding: 14px 16px;
        border: 1px solid rgba(29,78,216,0.18);
        box-shadow: 0 4px 20px rgba(0,0,0,0.35), inset 0 1px 0 rgba(255,255,255,0.03);
        transition: transform 0.15s, border-color 0.2s;
    }
    .metric-card:hover { transform: translateY(-1px); border-color: rgba(29,78,216,0.4); }

    /* ══ TABS ══ */
    .stTabs [data-baseweb="tab-list"] {
        background: rgba(9,21,37,0.8);
        border-radius: 10px;
        padding: 4px;
        border: 1px solid rgba(29,78,216,0.15);
        gap: 4px;
    }
    .stTabs [data-baseweb="tab"] {
        background: transparent;
        border: none;
        color: #6495b8;
        font-size: 13px;
        font-weight: 600;
        border-radius: 8px;
        padding: 8px 18px;
        transition: all 0.2s;
        font-family: 'Inter', sans-serif;
    }
    .stTabs [data-baseweb="tab"]:hover { background: rgba(29,78,216,0.08); color: #90b8d8; }
    .stTabs [aria-selected="true"] {
        background: linear-gradient(135deg, #1d4ed8 0%, #1e40af 100%) !important;
        color: white !important;
        box-shadow: 0 4px 12px rgba(29,78,216,0.4) !important;
    }

    /* ══ BUTTONS ══ */
    .stButton > button {
        background: linear-gradient(135deg, #1a2e50 0%, #111e38 100%);
        border: 1px solid rgba(29,78,216,0.25);
        color: #90b8d8;
        border-radius: 8px;
        font-family: 'Inter', sans-serif;
        font-size: 12px;
        font-weight: 600;
        transition: all 0.2s;
        box-shadow: 0 2px 8px rgba(0,0,0,0.3);
    }
    .stButton > button:hover {
        background: linear-gradient(135deg, #1d4ed8 0%, #1e40af 100%);
        border-color: #1d4ed8;
        color: white;
        box-shadow: 0 4px 16px rgba(29,78,216,0.4);
        transform: translateY(-1px);
    }

    /* ══ SELECTBOX ══ */
    .stSelectbox > div > div {
        background: #0f1e35;
        border: 1px solid rgba(29,78,216,0.25);
        border-radius: 8px;
        color: #90b8d8;
        font-family: 'Inter', sans-serif;
    }

    /* ══ EXPANDER ══ */
    .streamlit-expanderHeader,
    [data-testid="stExpander"] summary,
    details > summary {
        background: linear-gradient(90deg, #1a3a6e 0%, #0f2348 100%) !important;
        border-radius: 10px !important;
        border: 1.5px solid rgba(29,78,216,0.6) !important;
        color: #e8f4ff !important;
        font-family: 'Inter', sans-serif !important;
        font-weight: 700 !important;
        font-size: 14px !important;
        padding: 12px 18px !important;
    }
    [data-testid="stExpander"] summary:hover,
    details > summary:hover {
        background: linear-gradient(90deg, #1d4ed8 0%, #1a3a6e 100%) !important;
        border-color: #3b82f6 !important;
        color: #ffffff !important;
    }
    [data-testid="stExpander"] {
        border: 1px solid rgba(29,78,216,0.25) !important;
        border-radius: 10px !important;
        background: #0a1220 !important;
    }

    /* ══ DIVIDER ══ */
    hr {
        border: none;
        border-top: 1px solid rgba(29,78,216,0.12) !important;
        margin: 20px 0 !important;
    }

    /* ══ ALERTS ══ */
    .alert-bullish {
        background: linear-gradient(135deg, rgba(0,230,118,0.08) 0%, rgba(0,230,118,0.03) 100%);
        border: 1.5px solid rgba(0,230,118,0.5);
        border-radius: 12px;
        padding: 16px;
        font-size: 17px;
        font-weight: 800;
        color: #00e676;
        text-align: center;
        box-shadow: 0 4px 20px rgba(0,230,118,0.1);
    }
    .alert-bearish {
        background: linear-gradient(135deg, rgba(255,82,82,0.08) 0%, rgba(255,82,82,0.03) 100%);
        border: 1.5px solid rgba(255,82,82,0.5);
        border-radius: 12px;
        padding: 16px;
        font-size: 17px;
        font-weight: 800;
        color: #ff5252;
        text-align: center;
        box-shadow: 0 4px 20px rgba(255,82,82,0.1);
    }

    /* ══ BLINK ANIMATIONS ══ */
    @keyframes blink_green  { 0%,100%{opacity:1;box-shadow:0 0 6px #00e676} 50%{opacity:0.25;box-shadow:none} }
    @keyframes blink_red    { 0%,100%{opacity:1;box-shadow:0 0 6px #ff5252} 50%{opacity:0.25;box-shadow:none} }
    @keyframes blink_orange { 0%,100%{opacity:1;box-shadow:0 0 6px #ff8c00} 50%{opacity:0.25;box-shadow:none} }
    @keyframes blink_purple { 0%,100%{opacity:1;box-shadow:0 0 6px #a78bfa} 50%{opacity:0.25;box-shadow:none} }
    @keyframes blink_amber  { 0%,100%{opacity:1;box-shadow:0 0 6px #f59e0b} 50%{opacity:0.25;box-shadow:none} }
    .live-dot-green  { display:inline-block;width:7px;height:7px;border-radius:50%;background:#00e676;animation:blink_green  1.4s infinite;margin-right:5px; }
    .live-dot-red    { display:inline-block;width:7px;height:7px;border-radius:50%;background:#ff5252;animation:blink_red    1.4s infinite;margin-right:5px; }
    .live-dot-orange { display:inline-block;width:7px;height:7px;border-radius:50%;background:#ff8c00;animation:blink_orange 1.4s infinite;margin-right:5px; }
    .live-dot-purple { display:inline-block;width:7px;height:7px;border-radius:50%;background:#a78bfa;animation:blink_purple 1.4s infinite;margin-right:5px; }
    .live-dot-amber  { display:inline-block;width:7px;height:7px;border-radius:50%;background:#f59e0b;animation:blink_amber  1.4s infinite;margin-right:5px; }

    /* ══ DATAFRAME ══ */
    .stDataFrame { border-radius: 10px; overflow: hidden; border: 1px solid rgba(29,78,216,0.15) !important; }
    .stDataFrame thead tr th {
        background: #0f1e35 !important;
        color: #6495b8 !important;
        font-size: 11px !important;
        font-weight: 700 !important;
        letter-spacing: 1px !important;
        text-transform: uppercase !important;
        border-bottom: 1px solid rgba(29,78,216,0.2) !important;
    }
    .stDataFrame tbody tr:hover { background: rgba(29,78,216,0.06) !important; }

    /* ══ INFO / WARNING BOXES ══ */
    .stInfo, .stWarning, .stSuccess, .stError {
        border-radius: 10px !important;
        font-family: 'Inter', sans-serif !important;
        font-size: 13px !important;
    }

    /* ══ CAPTION ══ */
    .stCaption { color: #4e7a96 !important; font-size: 11px !important; }

    /* ══ SPINNER ══ */
    .stSpinner > div { border-top-color: #1d4ed8 !important; }

    /* ══ HEADER GLOW ACCENT ══ */
    @keyframes header_pulse {
        0%,100% { opacity: 0.6; }
        50%      { opacity: 1; }
    }
    .header-glow {
        animation: header_pulse 3s ease-in-out infinite;
    }

    /* ══ SMOOTH CARD HOVER ══ */
    @keyframes card_appear {
        from { opacity:0; transform: translateY(8px); }
        to   { opacity:1; transform: translateY(0); }
    }
    .animate-in { animation: card_appear 0.3s ease forwards; }

    h2, h3 { color: #c8dff5 !important; font-family: 'Inter', sans-serif !important; font-weight: 700 !important; }
</style>
""", unsafe_allow_html=True)

# ── Session state ─────────────────────────────────────────────
for key, val in [
    ("access_token", None),
    ("prev_prices", {}),
    ("fast_move_alerts", []),
    ("option_prev_prices", {}),
    ("sr_alerts", []),
    ("pcr_history", []),
    ("pcr_alerts", []),
    ("prev_oi_data", {}),
    ("trend_history", {}),
    ("notifications", []),
    ("notif_unread", 0),
    # ── Data Cache — refresh ke beech purana data dikhao ──
    ("cache_ltp",        {}),
    ("cache_quote",      {}),
    ("cache_ohlc",       {}),
    ("cache_chain",      {}),   # {instrument: (chain_data, expiry)}
    ("cache_expiries",   {}),   # {instrument: expiry_list}
    ("cache_vix",        None),
    ("cache_fii",        None),
    ("cache_timestamp",  ""),
    ("oi_wall_ticker",   []),   # [{name, resistance, res_oi, support, sup_oi, updated}]
    ("oi_wall_last_update", 0), # timestamp of last OI wall check
    # ── OI Timeline Snapshots — har timeframe ke liye ──
    ("oi_snapshots",     {}),   # {instrument_key: {timestamp: {strike: {call_oi, put_oi}}}}
]:
    if key not in st.session_state:
        st.session_state[key] = val

# Notifications file se load karo (page reload ke baad bhi rahe)
if not st.session_state["notifications"] and os.path.exists("notifications.json"):
    try:
        with open("notifications.json", "r") as _nf:
            _nd = json.load(_nf)
        st.session_state["notifications"] = _nd.get("notifications", [])
        st.session_state["notif_unread"]  = _nd.get("unread", 0)
    except Exception as e:
        print(f"[WARN] notifications.json load failed: {e}")

# ── Telegram Alert Functions (pehle define karo — add_notification mein use hoti hain) ──
TELEGRAM_FILE = "telegram_config.json"

def load_telegram_config():
    if os.path.exists(TELEGRAM_FILE):
        try:
            with open(TELEGRAM_FILE) as f:
                return json.load(f)
        except Exception as e:
            print(f"[WARN] load_telegram_config failed: {e}")
    return {"bot_token": "", "chat_id": "", "enabled": False}

def save_telegram_config(cfg):
    try:
        with open(TELEGRAM_FILE, "w") as f:
            json.dump(cfg, f)
    except Exception as e:
        print(f"[WARN] save_telegram_config failed: {e}")

def send_telegram(msg):
    cfg = load_telegram_config()
    if not cfg.get("enabled") or not cfg.get("bot_token") or not cfg.get("chat_id"):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage",
            data={"chat_id": cfg["chat_id"], "text": msg, "parse_mode": "HTML"},
            timeout=5
        )
    except Exception as e:
        print(f"[WARN] Telegram send failed: {e}")

# ── Notification helper ───────────────────────────────────────
NOTIF_FILE = "notifications.json"

def load_notifications():
    """File se notifications load karo"""
    if os.path.exists(NOTIF_FILE):
        try:
            with open(NOTIF_FILE, "r") as f:
                data = json.load(f)
            return data.get("notifications", []), data.get("unread", 0)
        except Exception as e:
            print(f"[WARN] load_notifications failed: {e}")
    return [], 0

def save_notifications(notifs, unread):
    """Notifications file mein save karo"""
    try:
        with open(NOTIF_FILE, "w") as f:
            json.dump({"notifications": notifs, "unread": unread}, f)
    except Exception as e:
        print(f"[WARN] save_notifications failed: {e}")

def add_notification(ntype, title, msg):
    """Bell mein naya alert add karo — sirf market open hone pe"""
    # ── Market band ho toh notification mat do ────────────────
    market_open, _ = is_market_open()
    if not market_open:
        return  # Market closed — koi notification nahi
    notif = {
        "type":  ntype,
        "title": title,
        "msg":   msg,
        "time":  now_ist().strftime("%d %b  %I:%M:%S %p")
    }
    existing = st.session_state.notifications
    # Duplicate avoid karo — same title last entry mein already hai toh skip
    if existing and existing[0].get("title") == title:
        return
    updated  = [notif] + existing[:49]  # max 50 rakhte hain
    unread   = min(st.session_state.notif_unread + 1, 99)
    st.session_state.notifications = updated
    st.session_state.notif_unread  = unread
    # File mein bhi save karo — page reload ke baad bhi rahe
    save_notifications(updated, unread)
    # Sound trigger
    st.markdown("<script>setTimeout(()=>playAlert(),300)</script>", unsafe_allow_html=True)
    # Telegram alert bhi bhejo
    send_telegram(f"🔔 <b>{title}</b>\n{msg}\n⏰ {notif['time']}")

# ── Token save/load ───────────────────────────────────────────
def save_token(token):
    with open(TOKEN_FILE, "w") as f:
        json.dump({"token": token, "date": str(date.today())}, f)

def load_token():
    if not os.path.exists(TOKEN_FILE):
        return None
    try:
        with open(TOKEN_FILE, "r") as f:
            data = json.load(f)
        if data.get("date") == str(date.today()):
            return data.get("token")
    except Exception as e:
        print(f"[WARN] load_token failed: {e}")
    return None

# ── Callback server ───────────────────────────────────────────
_captured_code = {"code": None}

class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        if "code" in params:
            _captured_code["code"] = params["code"][0]
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body style='background:#060e1a;color:#00e676;font-family:sans-serif;text-align:center;padding-top:80px'><h2>Login successful!</h2><p style='color:#ccc'>Yeh tab band karo.</p><script>setTimeout(()=>window.close(),2000)</script></body></html>")
        else:
            self.send_response(400); self.end_headers()
    def log_message(self, *a): pass

def start_callback_server(port):
    try:
        server = HTTPServer(("127.0.0.1", port), CallbackHandler)
        server.timeout = 5
        deadline = time.time() + 180
        while time.time() < deadline:
            server.handle_request()
            if _captured_code["code"]:
                break
    except OSError:
        pass

# ── API functions ─────────────────────────────────────────────
def get_access_token(auth_code):
    REDIRECT_URI = "https://trading-dashboard-eqcqbcuwrwfvovcmrsyqpp.streamlit.app"
    try:
        resp = requests.post(
            "https://api.upstox.com/v2/login/authorization/token",
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            data={
                "code":          auth_code,
                "client_id":     API_KEY,
                "client_secret": SECRET_KEY,
                "redirect_uri":  REDIRECT_URI,
                "grant_type":    "authorization_code"
            },
            timeout=15
        )
        if resp.status_code == 200:
            token = resp.json().get("access_token")
            if token:
                save_token(token)
                return token
            st.error("❌ Token field nahi mila response mein.")
            return None
        # Detailed error message
        try:
            err = resp.json()
            err_msg = err.get("message") or err.get("error_description") or str(err)
        except Exception:
            err_msg = resp.text[:200]
        if resp.status_code == 401:
            st.error(f"❌ Login Error 401 — Code expire/galat hai. Dobara Login button dabao. ({err_msg})")
        else:
            st.error(f"❌ Login Error: {resp.status_code} — {err_msg}")
        return None
    except requests.exceptions.Timeout:
        st.error("❌ Timeout — Dobara try karo.")
        return None
    except Exception as e:
        st.error(f"❌ Unexpected error: {e}")
        return None

def handle_401():
    st.session_state.access_token = None
    if os.path.exists(TOKEN_FILE): os.remove(TOKEN_FILE)
    st.error("⏰ Token expire! Dobara login karo...")
    st.rerun()

def get_ltp(token, keys):
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        resp = requests.get("https://api.upstox.com/v2/market-quote/ltp",
            params={"instrument_key": ",".join(keys)}, headers=headers, timeout=8)
        if resp.status_code == 401: handle_401()
        if resp.status_code == 200: return resp.json().get("data", {})
    except Exception as e:
        print(f"[WARN] get_ltp failed: {e}")
    return {}

def get_full_quote(token, keys):
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        resp = requests.get("https://api.upstox.com/v2/market-quote/quotes",
            params={"instrument_key": ",".join(keys)}, headers=headers, timeout=8)
        if resp.status_code == 401: handle_401()
        if resp.status_code == 200: return resp.json().get("data", {})
    except Exception as e:
        print(f"[WARN] get_full_quote failed: {e}")
    return {}

def get_ohlc(token, keys):
    """Aaj ka proper OHLC data — open, high, low, close (prev day)"""
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        resp = requests.get("https://api.upstox.com/v2/market-quote/ohlc",
            params={"instrument_key": ",".join(keys), "interval": "1d"}, headers=headers, timeout=8)
        if resp.status_code == 401: handle_401()
        if resp.status_code == 200: return resp.json().get("data", {})
    except Exception as e:
        print(f"[WARN] get_ohlc failed: {e}")
    return {}

def get_intraday_candles(token, instrument, interval="30minute"):
    """Intraday candles fetch karo — VWAP aur ATR ke liye"""
    try:
        instr_enc = instrument.replace("|", "%7C")
        headers   = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        resp = requests.get(
            f"https://api.upstox.com/v2/historical-candle/intraday/{instr_enc}/{interval}",
            headers=headers, timeout=8)
        if resp.status_code == 200:
            candles = resp.json().get("data", {}).get("candles", [])
            # candle = [timestamp, open, high, low, close, volume, oi]
            return candles
    except Exception as e:
        print(f"[WARN] Intraday candles fetch failed: {e}")
    return []

def calculate_vwap_atr(candles):
    """
    VWAP = Sum(Typical Price * Volume) / Sum(Volume)
    Typical Price = (High + Low + Close) / 3
    ATR = Average of True Range over all candles
    """
    if not candles:
        return None, None, None

    total_tp_vol = 0
    total_vol    = 0
    true_ranges  = []
    prev_close   = None

    for c in candles:
        try:
            ts, o, h, l, close, vol = c[0], c[1], c[2], c[3], c[4], c[5]
            vol = float(vol) if vol else 0
            h, l, close = float(h), float(l), float(close)

            # Typical Price
            tp = (h + l + close) / 3
            total_tp_vol += tp * vol
            total_vol    += vol

            # True Range
            if prev_close:
                tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
            else:
                tr = h - l
            true_ranges.append(tr)
            prev_close = close
        except Exception:
            continue

    vwap = round(total_tp_vol / total_vol, 2) if total_vol > 0 else None
    atr  = round(sum(true_ranges) / len(true_ranges), 2) if true_ranges else None

    # Last candle close
    last_close = float(candles[-1][4]) if candles else None

    return vwap, atr, last_close

def extract_ohlc(data, instrument):
    """OHLC endpoint se open, high, low, close nikalo"""
    key = instrument.replace("|", ":")
    q = data.get(key) or data.get(instrument) or {}
    ohlc = q.get("ohlc", {})
    return (
        ohlc.get("open",  None),
        ohlc.get("high",  None),
        ohlc.get("low",   None),
        ohlc.get("close", None),
    )

def extract_prev_close(data, instrument):
    """quotes endpoint se prev_close nikalo — yahi real previous day close hai"""
    key = instrument.replace("|", ":")
    q = data.get(key) or data.get(instrument) or {}
    # Upstox API fields — priority order mein try karo
    prev_close = (
        q.get("prev_close_price") or
        q.get("prev_close")       or
        q.get("close_price")      or
        q.get("last_close_price")
    )
    if not prev_close:
        # Fallback: net_change se calculate
        last = q.get("last_price", None)
        net  = q.get("net_change", None)
        if last and net is not None and net != 0:
            prev_close = round(last - net, 2)
    if not prev_close:
        # Last fallback: ohlc.close
        prev_close = q.get("ohlc", {}).get("close", None)
    return prev_close

def get_all_expiries(token, instrument):
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    def _fetch_expiries(key):
        try:
            resp = requests.get("https://api.upstox.com/v2/option/contract",
                params={"instrument_key": key}, headers=headers, timeout=10)
            if resp.status_code == 401: handle_401()
            if resp.status_code != 200: return []
            raw = resp.json().get("data", [])
            expiries = []
            for e in raw:
                exp = e.get("expiry") or e.get("expiry_date") or (e if isinstance(e, str) else None)
                if exp and exp not in expiries:
                    expiries.append(exp)
            return sorted(expiries)
        except Exception as ex:
            print(f"[WARN] get_all_expiries failed for {key}: {ex}")
            return []
    result = _fetch_expiries(instrument)
    # BSE Sensex ke liye alternate keys try karo
    if not result and "BSE_INDEX" in instrument:
        for alt in ["BSE_INDEX|Sensex", "BSE_INDEX|SENSEX50", "BSE_INDEX|BSX"]:
            result = _fetch_expiries(alt)
            if result:
                print(f"[INFO] Sensex expiries found with: {alt}")
                break
    return result

def get_option_chain(token, instrument, selected_expiry=None, all_exp=None):
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        if all_exp is None:
            all_exp = get_all_expiries(token, instrument)
        if not all_exp: return None, None
        expiry = selected_expiry if selected_expiry and selected_expiry in all_exp else all_exp[0]
        # BSE Sensex ke liye alternate keys try karo
        keys_to_try = [instrument]
        if "BSE_INDEX" in instrument:
            keys_to_try += ["BSE_INDEX|Sensex", "BSE_INDEX|SENSEX50", "BSE_INDEX|BSX"]
        for key in keys_to_try:
            try:
                chain = requests.get("https://api.upstox.com/v2/option/chain",
                    params={"instrument_key": key, "expiry_date": expiry}, headers=headers, timeout=12)
                if chain.status_code == 401: handle_401()
                if chain.status_code == 200:
                    data = chain.json().get("data", [])
                    if data:
                        if key != instrument:
                            print(f"[INFO] Sensex chain found with key: {key}")
                        return data, expiry
                print(f"[WARN] get_option_chain: status {chain.status_code} for {key}")
            except Exception:
                continue
        return None, expiry
    except Exception as e:
        print(f"[WARN] get_option_chain failed: {e}")
        return None, selected_expiry

def get_fii_dii_data():
    """FII/DII data — multiple sources try karo"""

    base_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
    }

    def parse_to_standard(category, date_str, buy, sell, net):
        return {
            "category": category,
            "date": date_str,
            "buyValue": float(buy or 0),
            "sellValue": float(sell or 0),
            "netValue": float(net or 0),
        }

    # ── Source 1: NSE India ────────────────────────────────────
    try:
        s = requests.Session()
        s.headers.update(base_headers)
        s.get("https://www.nseindia.com", timeout=5)
        s.get("https://www.nseindia.com/market-data/fii-dii-activity", timeout=5)
        ts   = int(time.time() * 1000)
        resp = s.get(f"https://www.nseindia.com/api/fiidiiTradeReact?_={ts}",
                     headers={**base_headers, "Referer": "https://www.nseindia.com/market-data/fii-dii-activity"},
                     timeout=8)
        if resp.status_code == 200:
            data = resp.json()
            if data and len(data) > 0:
                print("[INFO] FII/DII: NSE OK")
                return data
    except Exception as e:
        print(f"[WARN] FII/DII NSE: {e}")

    # ── Source 2: Trendlyne public API ────────────────────────
    try:
        resp2 = requests.get(
            "https://trendlyne.com/api/fii-dii-data/",
            headers={**base_headers, "Referer": "https://trendlyne.com/"},
            timeout=8)
        if resp2.status_code == 200:
            raw = resp2.json()
            result = []
            if isinstance(raw, dict):
                for cat in ["FII", "DII"]:
                    d = raw.get(cat, raw.get(cat.lower(), {}))
                    if d:
                        result.append(parse_to_standard(
                            cat,
                            d.get("date", now_ist().strftime("%d-%b-%Y")),
                            d.get("buy", 0), d.get("sell", 0), d.get("net", 0)
                        ))
            if result:
                print("[INFO] FII/DII: Trendlyne OK")
                return result
    except Exception as e:
        print(f"[WARN] FII/DII Trendlyne: {e}")

    # ── Source 3: Tickertape / Screener scrape ─────────────────
    try:
        resp3 = requests.get(
            "https://api.tickertape.in/stocks/fiidii",
            headers={**base_headers, "Referer": "https://www.tickertape.in/"},
            timeout=8)
        if resp3.status_code == 200:
            raw3 = resp3.json()
            data3 = raw3.get("data", raw3)
            if data3:
                result3 = []
                for item in (data3 if isinstance(data3, list) else [data3]):
                    cat = str(item.get("category", item.get("type", ""))).upper()
                    if cat in ["FII", "DII", "FPI"]:
                        if cat == "FPI": cat = "FII"
                        result3.append(parse_to_standard(
                            cat,
                            item.get("date", now_ist().strftime("%d-%b-%Y")),
                            item.get("buy", item.get("buyValue", 0)),
                            item.get("sell", item.get("sellValue", 0)),
                            item.get("net", item.get("netValue", 0))
                        ))
                if result3:
                    print("[INFO] FII/DII: Tickertape OK")
                    return result3
    except Exception as e:
        print(f"[WARN] FII/DII Tickertape: {e}")

    # ── Source 4: MoneyControl scrape ─────────────────────────
    try:
        resp4 = requests.get(
            "https://priceapi.moneycontrol.com/techCharts/indianMarket/index/history?symbol=NSE_FII&resolution=1D&from=1609459200&to=9999999999&countback=2&currencyCode=INR",
            headers={**base_headers, "Referer": "https://www.moneycontrol.com/"},
            timeout=8)
        if resp4.status_code == 200:
            raw4 = resp4.json()
            if raw4.get("s") == "ok":
                closes = raw4.get("c", [])
                if closes:
                    net_fii = float(closes[-1])
                    today_s = now_ist().strftime("%d-%b-%Y")
                    print("[INFO] FII/DII: MoneyControl partial OK")
                    return [
                        parse_to_standard("FII", today_s, max(0, net_fii), max(0, -net_fii), net_fii),
                        {"category": "DII", "date": today_s, "buyValue": 0, "sellValue": 0, "netValue": 0, "_source": "partial"}
                    ]
    except Exception as e:
        print(f"[WARN] FII/DII MoneyControl: {e}")

    # ── All sources failed — return unavailable marker ─────────
    today_s = now_ist().strftime("%d-%b-%Y")
    print("[WARN] FII/DII: All sources failed")
    return [
        {"category": "FII", "date": today_s, "buyValue": 0, "sellValue": 0, "netValue": 0, "_source": "unavailable"},
        {"category": "DII", "date": today_s, "buyValue": 0, "sellValue": 0, "netValue": 0, "_source": "unavailable"},
    ]

def extract_ltp(data, instrument):
    key = instrument.replace("|", ":")
    result = data.get(key) or data.get(instrument) or {}
    return result.get("last_price", None)

def extract_day_range(data, instrument):
    key = instrument.replace("|", ":")
    q = data.get(key) or data.get(instrument) or {}
    ohlc = q.get("ohlc", {})
    return ohlc.get("high", None), ohlc.get("low", None), ohlc.get("open", None), ohlc.get("close", None)

def calculate_analysis(chain_data, spot_price, expiry=None):
    if not chain_data: return None
    total_call_oi = total_put_oi = 0
    oi_data = []
    max_pain_data = {}
    atm_strike = None
    if spot_price:
        strikes = [item.get("strike_price", 0) for item in chain_data]
        if strikes:
            # Strike gap detect karo — Nifty=50, BankNifty=100
            sorted_strikes = sorted(set(strikes))
            strike_gap = 50  # default
            if len(sorted_strikes) >= 2:
                gaps = [sorted_strikes[i+1] - sorted_strikes[i] for i in range(min(5, len(sorted_strikes)-1))]
                strike_gap = min(gaps) if gaps else 50
            # Nearest strike gap multiple pe round karo
            rounded_spot = round(spot_price / strike_gap) * strike_gap
            atm_strike = min(strikes, key=lambda x: abs(x - rounded_spot))

    # Use first strike as identifier for instrument-specific OI tracking
    first_strike = chain_data[0].get("strike_price", 0) if chain_data else 0
    oi_key  = f"prev_oi_{int(first_strike)}"
    # Absolute path — TradingDashboard folder mein save hoga
    oi_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"oi_cache_{int(first_strike)}.json")

    # File se hamesha load karo — session state reliable nahi auto-refresh pe
    prev_oi = {}
    if os.path.exists(oi_file):
        try:
            with open(oi_file, "r") as f:
                raw = json.load(f)
            if raw.get("date") == str(date.today()):
                # Keys ko int mein convert karo — consistent matching ke liye
                for k, v in raw.get("data", {}).items():
                    try:
                        prev_oi[int(float(k))] = v
                    except:
                        prev_oi[k] = v
                print(f"[INFO] OI cache loaded: {len(prev_oi)} strikes")
            else:
                print(f"[INFO] OI cache expired — fresh start")
        except Exception as e:
            print(f"[WARN] OI cache load failed: {e}")
            prev_oi = {}
    curr_oi = {}

    for item in chain_data:
        strike      = item.get("strike_price", 0)
        call_oi     = item.get("call_options", {}).get("market_data", {}).get("oi", 0) or 0
        put_oi      = item.get("put_options",  {}).get("market_data", {}).get("oi", 0) or 0
        call_oi_chg = item.get("call_options", {}).get("market_data", {}).get("oi_day_change", 0) or 0
        put_oi_chg  = item.get("put_options",  {}).get("market_data", {}).get("oi_day_change", 0) or 0
        call_ltp    = item.get("call_options", {}).get("market_data", {}).get("ltp", 0) or 0
        put_ltp     = item.get("put_options",  {}).get("market_data", {}).get("ltp", 0) or 0

        # IV — multiple possible locations in Upstox API
        call_greeks = item.get("call_options", {}).get("option_greeks", {}) or {}
        put_greeks  = item.get("put_options",  {}).get("option_greeks", {}) or {}

        # Try iv, then implied_volatility, then vega
        call_iv = (call_greeks.get("iv") or
                   call_greeks.get("implied_volatility") or
                   item.get("call_options", {}).get("market_data", {}).get("iv") or 0)
        put_iv  = (put_greeks.get("iv") or
                   put_greeks.get("implied_volatility") or
                   item.get("put_options", {}).get("market_data", {}).get("iv") or 0)

        call_iv = float(call_iv) if call_iv else 0
        put_iv  = float(put_iv)  if put_iv  else 0

        # Debug: ATM ke paas IV log karo
        if spot_price and abs(strike - spot_price) <= 100:
            print(f"[DEBUG IV] Strike={strike} | Call IV={call_iv} | Put IV={put_iv} | Call Greeks={call_greeks}")

        # FIX: prev_oi mein float key bhi ho sakti hai — dono try karo
        prev_entry = (prev_oi.get(strike) or
                      prev_oi.get(float(strike)) or
                      prev_oi.get(int(strike)) or
                      prev_oi.get(str(strike)) or
                      prev_oi.get(str(float(strike))))

        if prev_entry:
            call_oi_chg = call_oi - prev_entry.get("call_oi", call_oi)
            put_oi_chg  = put_oi  - prev_entry.get("put_oi",  put_oi)
        elif call_oi_chg == 0 and put_oi_chg == 0:
            pass  # First run

        # Save current OI — int key use karo (consistent)
        curr_oi[int(strike)] = {"call_oi": int(call_oi), "put_oi": int(put_oi)}

        total_call_oi += call_oi
        total_put_oi  += put_oi
        max_pain_data[strike] = {"call_oi": call_oi, "put_oi": put_oi}
        oi_data.append({"Strike": strike, "Call OI": call_oi, "Put OI": put_oi,
                        "Call OI Change": call_oi_chg, "Put OI Change": put_oi_chg,
                        "Call LTP": call_ltp, "Put LTP": put_ltp,
                        "Call IV": call_iv, "Put IV": put_iv})

    # Save current OI — sirf file mein (reliable)
    if curr_oi:
        try:
            with open(oi_file, "w") as f:
                json.dump({"date": str(date.today()), "data": {str(k): v for k, v in curr_oi.items()}}, f)
            print(f"[INFO] OI cache saved: {len(curr_oi)} strikes")
        except Exception as e:
            print(f"[WARN] OI cache save failed: {e}")

    # ── OI Timeline Snapshots — multiple timeframes ke liye ──────
    # Har refresh pe current OI timestamp ke saath save karo
    if curr_oi:
        instr_name = "NIFTY" if first_strike < 30000 else ("BANKNIFTY" if first_strike < 60000 else "SENSEX")
        snap_key = f"oi_snap_{instr_name}"
        now_ts_snap = int(time.time())
        if "oi_snapshots" not in st.session_state:
            st.session_state["oi_snapshots"] = {}
        if snap_key not in st.session_state["oi_snapshots"]:
            st.session_state["oi_snapshots"][snap_key] = {}
        # Save snapshot with timestamp
        st.session_state["oi_snapshots"][snap_key][now_ts_snap] = dict(curr_oi)
        # Cleanup: sirf last 5 hours ke snapshots rakho (memory)
        cutoff = now_ts_snap - (5 * 3600)
        st.session_state["oi_snapshots"][snap_key] = {
            ts: snap for ts, snap in st.session_state["oi_snapshots"][snap_key].items()
            if ts >= cutoff
        }

    # ── Daily OI History save karo — 3:15 PM ke baad ─────────
    _now_ist = now_ist()
    if curr_oi and _now_ist.hour >= 15 and _now_ist.minute >= 15:
        try:
            save_daily_oi(
                instrument_name = "NIFTY" if first_strike < 30000 else ("BANKNIFTY" if first_strike < 60000 else "SENSEX"),
                strike_data     = curr_oi,
                spot            = spot_price,
                pcr             = total_put_oi / total_call_oi if total_call_oi > 0 else 0,
                max_pain        = None
            )
        except Exception as e:
            print(f"[WARN] Daily OI save failed: {e}")

        # ── Month end auto cleanup — last day of month ─────
        _t = now_ist()
        last_day = calendar.monthrange(_t.year, _t.month)[1]
        if _t.day == last_day:
            print(f"[INFO] Month end detected — running auto cleanup...")
            cleanup_old_history()
    pcr = total_put_oi / total_call_oi if total_call_oi > 0 else 0
    df  = pd.DataFrame(oi_data).sort_values("Strike")
    pain_values = {ts: sum(max(0, s-ts)*v["call_oi"] + max(0, ts-s)*v["put_oi"]
                           for s, v in max_pain_data.items())
                   for ts in sorted(max_pain_data)}
    max_pain_strike = min(pain_values, key=pain_values.get) if pain_values else None
    resistance_levels = []
    support_levels = []
    enhanced_levels = {}  # Extra info for display

    if atm_strike and not df.empty:
        df_above = df[df["Strike"] > atm_strike].copy()
        df_below = df[df["Strike"] < atm_strike].copy()
        avg_oi   = (df["Call OI"].mean() + df["Put OI"].mean()) / 2

        # ── 1. Base: Max OI levels — poore chain se (ATM restriction nahi) ──
        # Resistance = Top 3 Call OI (ATM ke upar wale strikes mein se sabse zyada)
        # Support    = Top 3 Put OI  (ATM ke neeche wale strikes mein se sabse zyada)
        # Agar ATM ke upar/neeche enough data nahi toh poore chain se lo
        if not df_above.empty:
            resistance_levels = df_above.nlargest(3, "Call OI")["Strike"].tolist()
        else:
            resistance_levels = df.nlargest(3, "Call OI")["Strike"].tolist()
        if not df_below.empty:
            support_levels = df_below.nlargest(3, "Put OI")["Strike"].tolist()
        else:
            support_levels = df.nlargest(3, "Put OI")["Strike"].tolist()

        # ── 2. OI Change Based (Dynamic) ─────────────────────
        # Fresh OI build ho raha = Active level
        dyn_resist = []
        dyn_support = []
        if not df_above.empty and "Call OI Change" in df_above.columns:
            df_above_chg = df_above[df_above["Call OI Change"] > 0]
            if not df_above_chg.empty:
                dyn_resist = df_above_chg.nlargest(2, "Call OI Change")["Strike"].tolist()
        if not df_below.empty and "Put OI Change" in df_below.columns:
            df_below_chg = df_below[df_below["Put OI Change"] > 0]
            if not df_below_chg.empty:
                dyn_support = df_below_chg.nlargest(2, "Put OI Change")["Strike"].tolist()

        # ── 3. OI Wall Detection ──────────────────────────────
        # OI > 2x average = WALL
        call_walls = df_above[df_above["Call OI"] > avg_oi * 2]["Strike"].tolist() if not df_above.empty else []
        put_walls  = df_below[df_below["Put OI"]  > avg_oi * 2]["Strike"].tolist() if not df_below.empty else []

        # ── 4. Volume Weighted Levels ─────────────────────────
        # OI * LTP = premium concentration (proxy for volume weight)
        if not df_above.empty:
            df_above["Call_Weight"] = df_above["Call OI"] * df_above["Call LTP"]
            vw_resist = df_above.nlargest(2, "Call_Weight")["Strike"].tolist()
        else:
            vw_resist = []

        if not df_below.empty:
            df_below["Put_Weight"] = df_below["Put OI"] * df_below["Put LTP"]
            vw_support = df_below.nlargest(2, "Put_Weight")["Strike"].tolist()
        else:
            vw_support = []

        # ── 5. Per-Strike PCR ─────────────────────────────────
        df["Strike_PCR"] = df["Put OI"] / df["Call OI"].replace(0, 1)
        # Refresh df_above and df_below with Strike_PCR column
        df_above = df[df["Strike"] >  atm_strike].copy() if atm_strike else df.copy()
        df_below = df[df["Strike"] <= atm_strike].copy() if atm_strike else df.copy()
        pcr_resist  = df_above[df_above["Strike_PCR"] < 0.5]["Strike"].tolist() if not df_above.empty else []
        pcr_support = df_below[df_below["Strike_PCR"] > 2.0]["Strike"].tolist() if not df_below.empty else []

        # ── Store enhanced info ───────────────────────────────
        enhanced_levels = {
            "dyn_resist":  sorted(set(dyn_resist))[:3],
            "dyn_support": sorted(set(dyn_support), reverse=True)[:3],
            "call_walls":  sorted(set(call_walls))[:3],
            "put_walls":   sorted(set(put_walls),  reverse=True)[:3],
            "vw_resist":   sorted(set(vw_resist))[:2],
            "vw_support":  sorted(set(vw_support), reverse=True)[:2],
            "pcr_resist":  sorted(set(pcr_resist))[:2],
            "pcr_support": sorted(set(pcr_support), reverse=True)[:2],
        }

    # ── ATM ±10 strikes ke unified sums — ek baar calculate, teen jagah use ──
    atm_call_chg = 0
    atm_put_chg  = 0
    max_call_oi_strike  = None
    max_put_oi_strike   = None
    max_call_chg_strike = None
    max_put_chg_strike  = None
    if atm_strike and not df.empty:
        atm_idx_list = df[df["Strike"] == atm_strike].index.tolist()
        if atm_idx_list:
            atm_pos  = df.index.get_loc(atm_idx_list[0])
            df_atm10 = df.iloc[max(0, atm_pos-10):min(len(df), atm_pos+11)]
            atm_call_chg = df_atm10["Call OI Change"].sum()
            atm_put_chg  = df_atm10["Put OI Change"].sum()
            max_call_oi_strike = int(df_atm10.loc[df_atm10["Call OI"].idxmax(), "Strike"])
            max_put_oi_strike  = int(df_atm10.loc[df_atm10["Put OI"].idxmax(),  "Strike"])

            # FIX: OI Change zero ho toh (pehla refresh) — OI pe fallback karo
            call_chg_sum = df_atm10["Call OI Change"].abs().sum()
            put_chg_sum  = df_atm10["Put OI Change"].abs().sum()

            if call_chg_sum > 0:
                max_call_chg_strike = int(df_atm10.loc[df_atm10["Call OI Change"].idxmax(), "Strike"])
            else:
                max_call_chg_strike = max_call_oi_strike  # Fallback: max OI strike use karo

            if put_chg_sum > 0:
                max_put_chg_strike = int(df_atm10.loc[df_atm10["Put OI Change"].idxmax(), "Strike"])
            else:
                max_put_chg_strike = max_put_oi_strike  # Fallback: max OI strike use karo
    fair_value_df = pd.DataFrame()
    if atm_strike and not df.empty:
        atm_idx = df[df["Strike"] == atm_strike].index
        if len(atm_idx) > 0:
            atm_pos = df.index.get_loc(atm_idx[0])
            fv = df.iloc[max(0, atm_pos-5):min(len(df), atm_pos+6)].copy()
            fv["Straddle Price"] = fv["Call LTP"] + fv["Put LTP"]

            if spot_price:
                from datetime import datetime as dt

                # T = time to expiry
                try:
                    from zoneinfo import ZoneInfo
                    now      = dt.now(ZoneInfo("Asia/Kolkata"))
                    exp_date = dt.strptime(expiry, "%Y-%m-%d").replace(tzinfo=ZoneInfo("Asia/Kolkata"))
                    days_left = (exp_date.date() - now.date()).days

                    if days_left == 0:
                        # Aaj expiry hai — intraday minutes remaining use karo
                        market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
                        mins_left    = max(1, (market_close - now).total_seconds() / 60)
                        T = mins_left / (365 * 24 * 60)
                        is_expiry_day = True
                        print(f"[INFO] Expiry day! Mins left = {mins_left:.0f}, T = {T:.6f}")
                    elif days_left < 0:
                        # Expiry nikal gayi (after 3:30 PM same day) — next expiry select hoga
                        T = 1 / 365.0
                        is_expiry_day = False
                    else:
                        # Normal: calendar days use karo (min 0.5 day)
                        T = max(days_left, 0.5) / 365.0
                        is_expiry_day = False
                except Exception:
                    from datetime import datetime as dt2
                    now       = dt2.now()
                    exp_date2 = dt2.strptime(expiry, "%Y-%m-%d")
                    days_left = (exp_date2.date() - now.date()).days
                    T = max(days_left, 0.5) / 365.0 if days_left > 0 else 1 / 365.0
                    is_expiry_day = (days_left == 0)

                r, q = 0.065, 0.0  # q=0: Nifty index options pe dividend yield separately adjust nahi hoti

                def bs_price(S, K, iv, option_type="call"):
                    if iv <= 0 or T <= 0: return 0
                    try:
                        iv_used = min(iv, 5.0)
                        sqrtT   = max(math.sqrt(T), 1e-8)
                        d1 = (math.log(S/K) + (r + 0.5*iv_used**2)*T) / (iv_used*sqrtT)
                        d2 = d1 - iv_used*sqrtT
                        def N(x): return 0.5*(1 + math.erf(x / math.sqrt(2)))
                        if option_type == "call":
                            bs = S*N(d1) - K*math.exp(-r*T)*N(d2)
                            return round(max(max(0, S - K), bs), 1)
                        else:
                            bs = K*math.exp(-r*T)*N(-d2) - S*N(-d1)
                            return round(max(max(0, K - S), bs), 1)
                    except Exception:
                        return 0

                # ── ATM se implied forward aur implied IV nikalo ──
                atm_row = fv[fv["Strike"] == atm_strike]
                implied_fwd = spot_price
                atm_iv      = 0.0

                if not atm_row.empty:
                    atm_c    = float(atm_row["Call LTP"].values[0])
                    atm_p    = float(atm_row["Put LTP"].values[0])
                    atm_c_iv = float(atm_row["Call IV"].values[0]) if atm_row["Call IV"].values[0] else 0
                    atm_p_iv = float(atm_row["Put IV"].values[0])  if atm_row["Put IV"].values[0]  else 0

                    # Implied forward from PCP
                    implied_fwd = atm_strike + atm_c - atm_p

                    # ATM IV = average of call & put IV (more stable than using one side)
                    if atm_c_iv > 0 and atm_p_iv > 0:
                        raw_iv  = (atm_c_iv + atm_p_iv) / 2
                        atm_iv  = (raw_iv / 100.0) if raw_iv > 2 else raw_iv
                    elif atm_c_iv > 0:
                        atm_iv  = (atm_c_iv / 100.0) if atm_c_iv > 2 else atm_c_iv
                    elif atm_p_iv > 0:
                        atm_iv  = (atm_p_iv / 100.0) if atm_p_iv > 2 else atm_p_iv

                print(f"[INFO] FV: ATM={atm_strike} | Fwd={implied_fwd:.1f} | ATM_IV={atm_iv:.4f} | Expiry={is_expiry_day}")

                c_fvs, p_fvs = [], []
                for _, row in fv.iterrows():
                    K     = float(row["Strike"])
                    c_ltp = float(row["Call LTP"])
                    p_ltp = float(row["Put LTP"])

                    if is_expiry_day:
                        # Expiry day: intrinsic value
                        c_fv = round(max(0, spot_price - K), 1)
                        p_fv = round(max(0, K - spot_price), 1)
                        # Agar ATM IV available hai toh BS se time value bhi add karo
                        if atm_iv > 0:
                            c_fv = bs_price(spot_price, K, atm_iv, "call")
                            p_fv = bs_price(spot_price, K, atm_iv, "put")
                    else:
                        if atm_iv > 0:
                            # Single ATM IV use karo — skew bias avoid hoga
                            c_fv = bs_price(spot_price, K, atm_iv, "call")
                            p_fv = bs_price(spot_price, K, atm_iv, "put")
                        else:
                            # Fallback: PCP from implied forward
                            c_fv = round(max(0, p_ltp + (implied_fwd - K)), 1)
                            p_fv = round(max(0, c_ltp - (implied_fwd - K)), 1)

                    c_fvs.append(c_fv)
                    p_fvs.append(p_fv)
                    print(f"[DEBUG FV] Strike={int(K)} | ATM_IV={atm_iv:.4f} | C_FV={c_fv} | P_FV={p_fv} | Expiry={is_expiry_day}")

                fv["Call Fair Value"] = c_fvs
                fv["Put Fair Value"]  = p_fvs
                fv["Fair Value"]      = (fv["Straddle Price"] / 2).round(2)
            else:
                fv["Fair Value"]      = (fv["Straddle Price"] / 2).round(2)
                fv["Call Fair Value"] = fv["Fair Value"]
                fv["Put Fair Value"]  = fv["Fair Value"]
            fair_value_df = fv
    return {"pcr": round(pcr, 3), "total_call_oi": total_call_oi, "total_put_oi": total_put_oi,
            "df": df, "max_pain": max_pain_strike,
            "resistance_levels": sorted(resistance_levels),
            "support_levels": sorted(support_levels, reverse=True),
            "enhanced_levels": enhanced_levels,
            "atm_strike": atm_strike, "fair_value_df": fair_value_df,
            # ── Unified ATM±10 sums — sirf ek baar calculate hote hain ──
            "atm_call_chg": atm_call_chg, "atm_put_chg": atm_put_chg,
            "max_call_oi_strike": max_call_oi_strike, "max_put_oi_strike": max_put_oi_strike,
            "max_call_chg_strike": max_call_chg_strike, "max_put_chg_strike": max_put_chg_strike}

def sentiment_label(pcr):
    if pcr >= 1.3: return "🟢 STRONG BULLISH", "bullish"
    elif pcr >= 1.0: return "🟢 BULLISH", "bullish"
    elif pcr >= 0.8: return "🟡 NEUTRAL", "neutral"
    elif pcr >= 0.6: return "🔴 BEARISH", "bearish"
    else: return "🔴 STRONG BEARISH", "bearish"

def fv_option_status(d):
    """
    Fair value option status
    ±2 threshold — Nifty options ke liye normal bid-ask spread ~1-2 hota hai
    """
    if d > 2:    return "🔴 MEHNGA"
    elif d < -2: return "🟢 SASTA"
    else:        return "⚪ FAIR"

# ══════════════════════════════════════════════════════════════
# HEADER
# ══════════════════════════════════════════════════════════════
# ── Notification bell sound JS ──
st.markdown("""
<audio id="notif-sound" preload="auto">
  <source src="https://cdn.jsdelivr.net/gh/freeCodeCamp/cdn/build/testable-projects-fcc/audio/BeepSound.wav" type="audio/wav">
</audio>
<script>
function playAlert() {
    try {
        var s = document.getElementById('notif-sound');
        if(s){ s.currentTime=0; s.play(); }
    } catch(e){}
}
</script>
""", unsafe_allow_html=True)

unread = st.session_state.notif_unread
notifs = st.session_state.notifications

# ── Market Status ─────────────────────────────────────────────
mkt_open, mkt_status = is_market_open()
mkt_color  = "#00e676" if mkt_open else "#ff5252"
mkt_dot    = "live-dot-green" if mkt_open else "live-dot-red"
mkt_icon   = "🟢" if mkt_open else "🔴"

# ── Header — Spot Price sabse bada ──
hcol1, hcol2, hcol3 = st.columns([5, 5, 1])
with hcol1:
    st.markdown(f"""
    <div class="spot-card">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
        <div class="spot-label header-glow">⚡ TRADEX DIIGAMBAR &nbsp;·&nbsp; LIVE TERMINAL</div>
        <div style="font-size:10px;color:#1d4ed8;letter-spacing:2px;font-weight:700;background:rgba(29,78,216,0.1);padding:3px 8px;border-radius:20px;border:1px solid rgba(29,78,216,0.3)">PRO</div>
      </div>
      <div style="display:flex;align-items:center;gap:28px;margin-top:6px">
        <div>
          <div style="font-size:15px;color:#8ab8d8;letter-spacing:4px;font-weight:900;text-transform:uppercase;margin-bottom:4px">NIFTY 50</div>
          <div class="spot-number" id="nifty-spot" style="font-size:52px;font-weight:900">—</div>
        </div>
        <div style="width:1px;height:52px;background:linear-gradient(180deg,transparent,rgba(29,78,216,0.5),transparent);margin:0 4px"></div>
        <div>
          <div style="font-size:15px;color:#8ab8d8;letter-spacing:4px;font-weight:900;text-transform:uppercase;margin-bottom:4px">BANK NIFTY</div>
          <div class="spot-number" id="bn-spot" style="font-size:36px;font-weight:900">—</div>
        </div>
      </div>
    </div>""", unsafe_allow_html=True)

with hcol2:
    st.markdown(f"""
    <div style="background:linear-gradient(135deg,#0f1e35 0%,#091525 100%);border:1px solid rgba(29,78,216,0.2);border-radius:14px;padding:12px 18px;height:100%;box-shadow:0 4px 24px rgba(0,0,0,0.4),inset 0 1px 0 rgba(255,255,255,0.03)">
      <div style="font-size:12px;color:#8ab8d8;letter-spacing:3px;font-weight:800;text-transform:uppercase;margin-bottom:8px">MARKET STATUS</div>
      <div style="font-size:17px;font-weight:800;color:{mkt_color};display:flex;align-items:center;gap:4px">
        <span class="{mkt_dot}"></span>{mkt_icon} {mkt_status}
      </div>
      <div style="font-size:11px;color:#4e7a96;margin-top:6px;display:flex;align-items:center;gap:6px">
        <span>{now_ist().strftime('%d %b %Y  %I:%M %p')}</span>
        <span style="color:rgba(0,230,118,0.7);font-weight:600;font-size:10px;background:rgba(0,230,118,0.07);padding:2px 7px;border-radius:10px;border:1px solid rgba(0,230,118,0.15)">⚡ 3s</span>
      </div>
    </div>""", unsafe_allow_html=True)

with hcol3:
    bell_label = f"🔔 {unread}" if unread > 0 else "🔔"
    bell_color = "#ff5252" if unread > 0 else "#ffd600"
    bell_bg    = "rgba(255,82,82,0.12)" if unread > 0 else "rgba(255,214,0,0.08)"
    bell_border= "rgba(255,82,82,0.4)"  if unread > 0 else "rgba(255,214,0,0.25)"
    st.markdown(f'<div style="margin-top:10px;text-align:center;font-size:20px;font-weight:bold;color:{bell_color};background:{bell_bg};border:1.5px solid {bell_border};border-radius:12px;padding:10px 6px;box-shadow:0 4px 16px rgba(0,0,0,0.3)">{bell_label}</div>', unsafe_allow_html=True)

# ── Notification Panel — Streamlit expander ──
icon_map  = {"breakout":"🚀","breakdown":"🔻","oi":"📊","pcr":"📈","sr":"⚠️"}
color_map = {"breakout":"#00e676","breakdown":"#ff5252","oi":"#a78bfa","pcr":"#ff8c00","sr":"#ffd600"}

bell_title = f"🔔 Notifications — {unread} unread" if unread > 0 else "🔔 Notifications"
with st.expander(bell_title, expanded=(unread > 0)):
    if notifs:
        for n in notifs[:20]:
            icon  = icon_map.get(n.get("type",""), "🔔")
            color = color_map.get(n.get("type",""), "#90b8d8")
            st.markdown(f"""
            <div style="background:linear-gradient(90deg,rgba(9,21,37,0.9) 0%,rgba(6,14,26,0.9) 100%);border-radius:10px;padding:12px 16px;margin:6px 0;border-left:3px solid {color};box-shadow:0 2px 12px rgba(0,0,0,0.3)">
              <div style="display:flex;justify-content:space-between;align-items:flex-start">
                <div style="font-size:13px;font-weight:700;color:{color}">{icon} {n.get('title','')}</div>
                <div style="font-size:10px;color:#4e7a96;white-space:nowrap;margin-left:10px">🕐 {n.get('time','')}</div>
              </div>
              <div style="font-size:12px;color:#7aa0be;margin-top:5px;line-height:1.5">{n.get('msg','')}</div>
            </div>""", unsafe_allow_html=True)
    else:
        st.markdown('<div style="text-align:center;color:#4e7a96;padding:16px;font-size:13px">📡 Koi alert nahi abhi — market monitor ho raha hai...</div>', unsafe_allow_html=True)

# ── Auto-login ────────────────────────────────────────────────
if not st.session_state.access_token:
    saved = load_token()
    if saved: st.session_state.access_token = saved

# ── URL se auto code capture (Streamlit Cloud redirect) ───────
if not st.session_state.access_token:
    try:
        query_params = st.query_params
        url_code = query_params.get("code", None)
        if url_code:
            # Code mile — turant token lo
            with st.spinner("✅ Upstox se code mila! Login ho raha hai..."):
                token = get_access_token(url_code)
                if token:
                    st.session_state.access_token = token
                    # URL clean karo — code hata do
                    st.query_params.clear()
                    st.rerun()
                else:
                    st.error("❌ Token nahi mila — Dobara login karo.")
                    st.query_params.clear()
    except Exception as e:
        print(f"[WARN] query_params read failed: {e}")

# ── Login page ────────────────────────────────────────────────
REDIRECT_URI = "https://trading-dashboard-eqcqbcuwrwfvovcmrsyqpp.streamlit.app"

if not st.session_state.access_token:
    st.markdown("---")
    st.subheader("🔐 Upstox Login")
    login_url = f"https://api.upstox.com/v2/login/authorization/dialog?response_type=code&client_id={API_KEY}&redirect_uri={REDIRECT_URI}"
    col_a, col_b = st.columns([2, 3])
    with col_a:
        st.link_button("🚀 Upstox Login Karo", login_url, use_container_width=True, type="primary")
    with col_b:
        st.info("**Sirf yeh karo:**\n1. Button dabao\n2. Upstox pe login karo\n3. Wapas aao → URL mein code dikhega → Auto login!")

    st.markdown("---")
    st.markdown("**⚙️ Manual Code — Agar automatic nahi hua:**")
    st.markdown("""
    <div style='background:#0d1929;border-radius:8px;padding:12px 16px;font-size:13px;color:#90b8d8;border:1px solid rgba(29,78,216,0.2);margin-bottom:10px'>
    1. Upstox login ke baad URL kuch aisa dikhega:<br>
    <code style='color:#00e676'>https://trading-dashboard-...streamlit.app/?code=<b>YAHAN_WALA_CODE_COPY_KARO</b></code><br><br>
    2. Sirf <b style='color:#ffd600'>?code= ke baad wala part</b> copy karo (pura lamba string)<br>
    3. Neeche box mein paste karo aur Submit dabao
    </div>
    """, unsafe_allow_html=True)

    manual_code = st.text_input(
        "Authorization Code paste karo:",
        placeholder="Upstox redirect URL mein ?code= ke baad wala code",
        key="manual_auth_code"
    )
    if st.button("🔑 Submit Code", type="primary"):
        code_clean = manual_code.strip()
        # Agar pura URL paste kiya toh code extract karo
        if "?code=" in code_clean:
            code_clean = code_clean.split("?code=")[-1].split("&")[0]
        elif "code=" in code_clean:
            code_clean = code_clean.split("code=")[-1].split("&")[0]
        if code_clean:
            with st.spinner("Token le raha hoon..."):
                token = get_access_token(code_clean)
                if token:
                    st.session_state.access_token = token
                    st.rerun()
                else:
                    st.error("❌ Token nahi mila — Code expire ho gaya hoga. Dobara login karo.")
        else:
            st.warning("⚠️ Code daalo pehle!")

    st.stop()

token = st.session_state.access_token

# ── Auto refresh always ON — no checkbox ─────────────────────
auto_refresh = True  # Always ON — 3 second refresh

# Buttons row
col_r1, col_terminal, col_r4, col_r5 = st.columns([4, 2, 2, 2])
with col_r4:
    if st.button("🔔 Mark Read"):
        st.session_state.notif_unread = 0
        save_notifications(st.session_state.notifications, 0)
        st.rerun()
with col_r5:
    if st.button("🗑️ Clear All"):
        st.session_state.notifications = []
        st.session_state.notif_unread  = 0
        save_notifications([], 0)
        st.rerun()
with col_r1:
    st.markdown('<div style="background:#00e67615;border:1px solid #00e67640;border-radius:6px;padding:5px 12px;font-size:12px;color:#00e676;display:inline-block">⚡ Auto Refresh ON — Har 3 second mein update hoga</div>', unsafe_allow_html=True)
with col_terminal:
    import pathlib
    terminal_path = pathlib.Path(__file__).parent / "trading-terminal-v5.html"
    terminal_abs  = terminal_path.resolve().as_uri()
    st.markdown(
        f'''<a href="{terminal_abs}" target="_blank" style="text-decoration:none;display:block;">
        <div style="background:#f59e0b20;border:1px solid #f59e0b60;border-radius:6px;
        padding:5px 12px;font-size:12px;color:#f59e0b;text-align:center;cursor:pointer;
        font-weight:bold;line-height:2;">🖥️ Live Terminal</div></a>''',
        unsafe_allow_html=True
    )

# ── Market Closed Banner ───────────────────────────────────────
if not mkt_open:
    _now_ist = now_ist()
    if _now_ist.weekday() >= 5:
        closed_reason = "🗓️ Aaj Weekend hai — Market band hai"
        closed_sub    = "Somwar 9:15 AM pe market khulega"
    elif _now_ist.hour < 9 or (_now_ist.hour == 9 and _now_ist.minute < 15):
        opens_in = (_now_ist.replace(hour=9, minute=15, second=0) - _now_ist)
        mins     = int(opens_in.total_seconds() // 60)
        closed_reason = f"🌅 Pre-Market — Market abhi band hai"
        closed_sub    = f"Aaj 9:15 AM pe khulega ({mins} minute mein)"
    else:
        closed_reason = "🌙 Market aaj ke liye band ho gaya"
        closed_sub    = "Kal subah 9:15 AM pe phir khulega"
    st.markdown(f"""
    <div style="background:#ff8c0018;border:1.5px solid #ff8c00;border-radius:10px;padding:14px 20px;margin:10px 0;display:flex;align-items:center;gap:16px">
      <div style="font-size:28px">🔴</div>
      <div>
        <div style="font-size:16px;font-weight:bold;color:#ff8c00">{closed_reason}</div>
        <div style="font-size:12px;color:#90b8d8;margin-top:3px">{closed_sub} &nbsp;|&nbsp; Last prices dikh rahe hain</div>
      </div>
    </div>""", unsafe_allow_html=True)

# ── Telegram Settings only in sidebar ────────────────────────
with st.sidebar:
    st.markdown("### 📱 Telegram Alerts")
    tg_cfg = load_telegram_config()
    tg_token = st.text_input("Bot Token:", value=tg_cfg.get("bot_token",""), type="password", key="tg_token")
    tg_chat  = st.text_input("Chat ID:", value=tg_cfg.get("chat_id",""), key="tg_chat")
    tg_on    = st.checkbox("Alerts ON", value=tg_cfg.get("enabled", False), key="tg_enabled")
    if st.button("💾 Save Telegram"):
        new_cfg = {"bot_token": tg_token, "chat_id": tg_chat, "enabled": tg_on}
        save_telegram_config(new_cfg)
        st.success("✅ Saved!")
    if st.button("🧪 Test Alert"):
        send_telegram("✅ Test Alert — Dashboard connected hai!")
        st.success("✅ Test bheja!")
    st.markdown("---")
    st.markdown("**Setup kaise karo:**")
    st.markdown("1. @BotFather se bot banao")
    st.markdown("2. @userinfobot se Chat ID lo")
    st.markdown("3. Upar fill karo aur Save karo")
st.markdown("---")

# ══════════════════════════════════════════════════════════════
# LIVE PRICES
# ══════════════════════════════════════════════════════════════
st.markdown('<div class="sec-header">⚡ Live Prices + Market Info</div>', unsafe_allow_html=True)

# ── Cache timestamp dikhao ────────────────────────────────────
if st.session_state.cache_timestamp:
    st.markdown(f'<div style="font-size:11px;color:#6495b8;margin-bottom:6px">🕐 Last update: <b style="color:#00bfff">{st.session_state.cache_timestamp}</b> — Naya data load ho raha hai...</div>', unsafe_allow_html=True)

# ── Naya data fetch karo ──────────────────────────────────────
with st.spinner("Live prices la raha hoon..."):
    new_ltp   = safe_api_call(get_ltp,       token, ["NSE_INDEX|Nifty 50", "NSE_INDEX|Nifty Bank", "BSE_INDEX|SENSEX"], fallback=None)
    new_quote = safe_api_call(get_full_quote, token, ["NSE_INDEX|Nifty 50", "NSE_INDEX|Nifty Bank", "BSE_INDEX|SENSEX"], fallback=None)
    new_ohlc  = safe_api_call(get_ohlc,      token, ["NSE_INDEX|Nifty 50", "NSE_INDEX|Nifty Bank", "BSE_INDEX|SENSEX"], fallback=None)

# ── Cache update — agar naya data aaya toh save karo ─────────
if new_ltp:   st.session_state.cache_ltp   = new_ltp
if new_quote: st.session_state.cache_quote = new_quote
if new_ohlc:  st.session_state.cache_ohlc  = new_ohlc
if new_ltp or new_quote:
    st.session_state.cache_timestamp = now_ist().strftime("%I:%M:%S %p")

# ── Use cache — naya ho ya purana ────────────────────────────
ltp_data   = st.session_state.cache_ltp
quote_data = st.session_state.cache_quote
ohlc_data  = st.session_state.cache_ohlc

if not ltp_data and not quote_data:
    st.markdown("""
    <div style="background:#ff525222;border:1.5px solid #ff5252;border-radius:10px;padding:16px;text-align:center;font-size:15px;color:#ff8888">
    🌐 <b>Internet Connection Error!</b><br>
    <span style="font-size:13px;color:#90b8d8">Upstox server se connect nahi ho pa raha.<br>
    WiFi/Data check karo aur <b>Refresh</b> dabao.</span>
    </div>""", unsafe_allow_html=True)
    if auto_refresh:
        time.sleep(5)
        st.rerun()
    st.stop()

nifty_price     = extract_ltp(ltp_data, "NSE_INDEX|Nifty 50")
banknifty_price = extract_ltp(ltp_data, "NSE_INDEX|Nifty Bank")
sensex_price    = extract_ltp(ltp_data, "BSE_INDEX|SENSEX")

# Aaj ka OHLC — dedicated endpoint se
nifty_open,  nifty_high,  nifty_low,  nifty_close  = extract_ohlc(ohlc_data, "NSE_INDEX|Nifty 50")
bnifty_open, bnifty_high, bnifty_low, bnifty_close = extract_ohlc(ohlc_data, "NSE_INDEX|Nifty Bank")
sx_open,     sx_high,     sx_low,     sx_close      = extract_ohlc(ohlc_data, "BSE_INDEX|SENSEX")

# Fallback — agar OHLC API kaam na kare
if not nifty_high:
    nifty_high, nifty_low, nifty_open, nifty_close = extract_day_range(quote_data, "NSE_INDEX|Nifty 50")
if not bnifty_high:
    bnifty_high, bnifty_low, bnifty_open, bnifty_close = extract_day_range(quote_data, "NSE_INDEX|Nifty Bank")
if not sx_high:
    sx_high, sx_low, sx_open, sx_close = extract_day_range(quote_data, "BSE_INDEX|SENSEX")

# Prev close — quotes endpoint se (real previous day close)
nifty_prev_close  = extract_prev_close(quote_data, "NSE_INDEX|Nifty 50")
bnifty_prev_close = extract_prev_close(quote_data, "NSE_INDEX|Nifty Bank")
sx_prev_close     = extract_prev_close(quote_data, "BSE_INDEX|SENSEX")

# ── Price change calculation ──────────────────────────────────
def get_change(current, high, low, open_p=None, close_p=None):
    if not current: return None, None
    base = close_p if close_p else (open_p if open_p else None)
    if not base: return None, None
    chg     = current - base
    chg_pct = (chg / base) * 100 if base > 0 else 0
    return round(chg, 2), round(chg_pct, 2)

n_chg,  n_pct  = get_change(nifty_price,     nifty_high,  nifty_low,  nifty_open,  nifty_prev_close)
bn_chg, bn_pct = get_change(banknifty_price, bnifty_high, bnifty_low, bnifty_open, bnifty_prev_close)
sx_chg, sx_pct = get_change(sensex_price,    sx_high,     sx_low,     sx_open,     sx_prev_close)

# ── Update Header Spot Prices ─────────────────────────────────
n_spot_display  = f"{nifty_price:,.0f}"     if nifty_price     else "—"
bn_spot_display = f"{banknifty_price:,.0f}" if banknifty_price else "—"
sx_spot_display = f"{sensex_price:,.0f}"    if sensex_price    else "—"
n_chg_col  = "#00e676" if (n_chg  is not None and n_chg  >= 0) else "#ff5252"
bn_chg_col = "#00e676" if (bn_chg is not None and bn_chg >= 0) else "#ff5252"
sx_chg_col = "#00e676" if (sx_chg is not None and sx_chg >= 0) else "#ff5252"
n_arrow    = "▲" if (n_chg  is not None and n_chg  >= 0) else "▼"
bn_arrow   = "▲" if (bn_chg is not None and bn_chg >= 0) else "▼"
sx_arrow   = "▲" if (sx_chg is not None and sx_chg >= 0) else "▼"
n_chg_str  = f"{n_arrow} {abs(n_chg):,.1f} ({abs(n_pct):.2f}%)"   if n_chg  is not None else ""
bn_chg_str = f"{bn_arrow} {abs(bn_chg):,.1f} ({abs(bn_pct):.2f}%)" if bn_chg is not None else ""
sx_chg_str = f"{sx_arrow} {abs(sx_chg):,.1f} ({abs(sx_pct):.2f}%)" if sx_chg is not None else ""

st.markdown(f"""
<div style="background:linear-gradient(90deg,#080e1c 0%,#060a14 100%);border:1px solid rgba(29,78,216,0.2);border-radius:12px;padding:12px 22px;margin-bottom:10px;box-shadow:0 4px 20px rgba(0,0,0,0.4)">
  <div style="display:flex;align-items:stretch;gap:28px">
    <div>
      <div style="font-size:11px;color:#8ab8d8;letter-spacing:3px;margin-bottom:3px;font-weight:700;text-transform:uppercase">NIFTY 50</div>
      <div style="font-family:'JetBrains Mono',monospace;font-size:40px;font-weight:900;color:#ffffff;line-height:1;letter-spacing:-1px">{n_spot_display}</div>
      <div style="font-family:'JetBrains Mono',monospace;font-size:13px;font-weight:700;color:{n_chg_col};margin-top:3px">{n_chg_str}</div>
    </div>
    <div style="width:1px;background:linear-gradient(180deg,transparent,rgba(29,78,216,0.4),transparent);margin:4px 0"></div>
    <div>
      <div style="font-size:11px;color:#8ab8d8;letter-spacing:3px;margin-bottom:3px;font-weight:700;text-transform:uppercase">BANK NIFTY</div>
      <div style="font-family:'JetBrains Mono',monospace;font-size:36px;font-weight:900;color:#e2e8f0;line-height:1;letter-spacing:-1px">{bn_spot_display}</div>
      <div style="font-family:'JetBrains Mono',monospace;font-size:13px;font-weight:700;color:{bn_chg_col};margin-top:3px">{bn_chg_str}</div>
    </div>
    <div style="width:1px;background:linear-gradient(180deg,transparent,rgba(255,170,50,0.4),transparent);margin:4px 0"></div>
    <div>
      <div style="font-size:11px;color:#ffb347;letter-spacing:3px;margin-bottom:3px;font-weight:700;text-transform:uppercase">BSE SENSEX</div>
      <div style="font-family:'JetBrains Mono',monospace;font-size:36px;font-weight:900;color:#ffe0a0;line-height:1;letter-spacing:-1px">{sx_spot_display}</div>
      <div style="font-family:'JetBrains Mono',monospace;font-size:13px;font-weight:700;color:{sx_chg_col};margin-top:3px">{sx_chg_str}</div>
    </div>
    <div style="margin-left:auto;display:flex;align-items:center">
      <div style="text-align:right">
        <div style="font-size:10px;color:#8ab8d8;letter-spacing:2px;text-transform:uppercase">Last Update</div>
        <div style="font-family:'JetBrains Mono',monospace;font-size:13px;font-weight:700;color:#00bfff">{st.session_state.cache_timestamp or "—"}</div>
        <div style="font-size:10px;color:#00e676;margin-top:3px">⚡ 3s Auto Refresh</div>
      </div>
    </div>
  </div>
</div>""", unsafe_allow_html=True)

# ── 4 columns: Nifty | BankNifty | Sensex | VIX ─────────────
col1, col2, col3, col4 = st.columns([2, 2, 2, 1])

with col1:
    n_display  = f"₹{nifty_price:,.2f}" if nifty_price else "⚠️ Unavailable"
    hl         = f"O: {nifty_open:,.0f} &nbsp;|&nbsp; H: {nifty_high:,.0f} &nbsp;|&nbsp; L: {nifty_low:,.0f} &nbsp;|&nbsp; PC: {nifty_prev_close:,.0f}" if nifty_high and nifty_prev_close else (f"O: {nifty_open:,.0f} | H: {nifty_high:,.0f} | L: {nifty_low:,.0f}" if nifty_high else "")
    n_positive  = n_chg is not None and n_chg >= 0
    n_chg_color = "#00e676" if n_positive else "#ff5252"
    n_arrow     = "▲" if n_positive else "▼"
    n_dot       = "live-dot-green" if n_positive else "live-dot-red"
    n_border    = "#00e676" if n_positive else "#ff5252"
    n_bg        = "#00e67618" if n_positive else "#ff525218"
    n_price_col = "#00e676" if n_positive else "#ff5252"
    chg_html    = f'<div style="font-size:16px;color:{n_chg_color};font-weight:800;margin-top:3px">{n_arrow} {abs(n_chg):,.2f} ({abs(n_pct):.2f}%)</div>' if n_chg is not None else ""
    st.markdown(f"""
    <div style="background:linear-gradient(135deg,{n_bg},rgba(6,10,18,0.95));border-radius:14px;padding:18px 20px;border:1.5px solid {n_border}40;border-left:4px solid {n_border};box-shadow:0 6px 28px rgba(0,0,0,0.45),inset 0 1px 0 rgba(255,255,255,0.04)">
      <div style="font-size:12px;color:#8ab8d8;text-transform:uppercase;letter-spacing:2px;margin-bottom:8px;font-weight:800">NIFTY 50 &nbsp;<span class="{n_dot}"></span><span style="color:{n_chg_color};font-size:8px;letter-spacing:1px">LIVE</span></div>
      <div style="font-family:'JetBrains Mono',monospace;font-size:36px;font-weight:900;color:{n_price_col};letter-spacing:-0.5px;line-height:1">{n_display}</div>
      {chg_html}
      <div style="font-size:10px;color:#7aa0be;margin-top:8px;font-family:'JetBrains Mono',monospace">{hl}</div>
    </div>""", unsafe_allow_html=True)

with col2:
    b_display  = f"₹{banknifty_price:,.2f}" if banknifty_price else "⚠️ Unavailable"
    bhl        = f"O: {bnifty_open:,.0f} &nbsp;|&nbsp; H: {bnifty_high:,.0f} &nbsp;|&nbsp; L: {bnifty_low:,.0f} &nbsp;|&nbsp; PC: {bnifty_prev_close:,.0f}" if bnifty_high and bnifty_prev_close else (f"O: {bnifty_open:,.0f} | H: {bnifty_high:,.0f} | L: {bnifty_low:,.0f}" if bnifty_high else "")
    b_positive  = bn_chg is not None and bn_chg >= 0
    b_chg_color = "#00e676" if b_positive else "#ff5252"
    b_arrow     = "▲" if b_positive else "▼"
    b_dot       = "live-dot-green" if b_positive else "live-dot-red"
    b_border    = "#00e676" if b_positive else "#ff5252"
    b_bg        = "#00e67618" if b_positive else "#ff525218"
    b_price_col = "#00e676" if b_positive else "#ff5252"
    bchg_html   = f'<div style="font-size:16px;color:{b_chg_color};font-weight:800;margin-top:3px">{b_arrow} {abs(bn_chg):,.2f} ({abs(bn_pct):.2f}%)</div>' if bn_chg is not None else ""
    st.markdown(f"""
    <div style="background:linear-gradient(135deg,{b_bg},rgba(6,10,18,0.95));border-radius:14px;padding:18px 20px;border:1.5px solid {b_border}40;border-left:4px solid {b_border};box-shadow:0 6px 28px rgba(0,0,0,0.45),inset 0 1px 0 rgba(255,255,255,0.04)">
      <div style="font-size:12px;color:#8ab8d8;text-transform:uppercase;letter-spacing:2px;margin-bottom:8px;font-weight:800">BANK NIFTY &nbsp;<span class="{b_dot}"></span><span style="color:{b_chg_color};font-size:8px;letter-spacing:1px">LIVE</span></div>
      <div style="font-family:'JetBrains Mono',monospace;font-size:36px;font-weight:900;color:{b_price_col};letter-spacing:-0.5px;line-height:1">{b_display}</div>
      {bchg_html}
      <div style="font-size:10px;color:#7aa0be;margin-top:8px;font-family:'JetBrains Mono',monospace">{bhl}</div>
    </div>""", unsafe_allow_html=True)

with col3:
    # ── BSE Sensex ─────────────────────────────────────────
    sx_display  = f"₹{sensex_price:,.2f}" if sensex_price else "⚠️ Unavailable"
    sxhl        = f"O: {sx_open:,.0f} &nbsp;|&nbsp; H: {sx_high:,.0f} &nbsp;|&nbsp; L: {sx_low:,.0f} &nbsp;|&nbsp; PC: {sx_prev_close:,.0f}" if sx_high and sx_prev_close else (f"O: {sx_open:,.0f} | H: {sx_high:,.0f} | L: {sx_low:,.0f}" if sx_high else "")
    sx_positive  = sx_chg is not None and sx_chg >= 0
    sx_chg_color = "#00e676" if sx_positive else "#ff5252"
    sx_dot       = "live-dot-green" if sx_positive else "live-dot-red"
    sx_border    = "#00e676" if sx_positive else "#ff5252"
    sx_bg_card   = "#00e67618" if sx_positive else "#ff525218"
    sx_price_col = "#00e676" if sx_positive else "#ff5252"
    sx_arrow2    = "▲" if sx_positive else "▼"
    sxchg_html   = f'<div style="font-size:16px;color:{sx_chg_color};font-weight:800;margin-top:3px">{sx_arrow2} {abs(sx_chg):,.2f} ({abs(sx_pct):.2f}%)</div>' if sx_chg is not None else ""
    st.markdown(f"""
    <div style="background:linear-gradient(135deg,{sx_bg_card},rgba(6,10,18,0.95));border-radius:14px;padding:18px 20px;border:1.5px solid {sx_border}40;border-left:4px solid #ff9500;box-shadow:0 6px 28px rgba(0,0,0,0.45),inset 0 1px 0 rgba(255,255,255,0.04)">
      <div style="font-size:12px;color:#ffb347;text-transform:uppercase;letter-spacing:2px;margin-bottom:8px;font-weight:800">BSE SENSEX &nbsp;<span class="{sx_dot}"></span><span style="color:{sx_chg_color};font-size:8px;letter-spacing:1px">LIVE</span></div>
      <div style="font-family:'JetBrains Mono',monospace;font-size:36px;font-weight:900;color:{sx_price_col};letter-spacing:-0.5px;line-height:1">{sx_display}</div>
      {sxchg_html}
      <div style="font-size:10px;color:#7aa0be;margin-top:8px;font-family:'JetBrains Mono',monospace">{sxhl}</div>
    </div>""", unsafe_allow_html=True)

with col4:
    # ── India VIX ──────────────────────────────────────────
    vix_data = safe_api_call(get_india_vix, fallback=None)
    if vix_data and vix_data.get("last"):
        vix_val   = vix_data["last"]
        vix_chg   = vix_data.get("pchange", 0) or 0
        vix_col   = "#ff5252" if vix_val > 20 else ("#ffd600" if vix_val > 15 else "#00e676")
        vix_mood  = "😱 HIGH FEAR" if vix_val > 20 else ("⚠️ CAUTION" if vix_val > 15 else "😊 LOW FEAR")
        vix_arrow = "▲" if vix_chg >= 0 else "▼"
        st.markdown(f"""
        <div style="background:linear-gradient(135deg,#0f1e35 0%,#091525 100%);border-radius:14px;padding:14px 16px;border:1.5px solid {vix_col}40;box-shadow:0 6px 24px rgba(0,0,0,0.4),0 0 20px {vix_col}08;margin-bottom:8px">
          <div style="font-size:13px;color:#90b8d8;text-transform:uppercase;letter-spacing:2px;margin-bottom:6px;font-weight:800">😨 INDIA VIX</div>
          <div style="font-family:'JetBrains Mono',monospace;font-size:30px;font-weight:900;color:{vix_col};line-height:1">{vix_val:.2f}</div>
          <div style="font-family:'JetBrains Mono',monospace;font-size:11px;color:{vix_col};margin-top:3px">{vix_arrow} {abs(vix_chg):.2f}%</div>
          <div style="font-size:10px;color:{vix_col};margin-top:6px;font-weight:700;background:rgba(0,0,0,0.2);border-radius:6px;padding:4px 8px;display:inline-block">{vix_mood}</div>
          <div style="font-size:9px;color:#4e7a96;margin-top:6px;font-family:'JetBrains Mono',monospace">H: {vix_data.get('high','--')} · L: {vix_data.get('low','--')}</div>
        </div>""", unsafe_allow_html=True)
    else:
        st.markdown('<div style="background:#0d1929;border-radius:10px;padding:12px;border:1px solid #ff8c0055;color:#ff8c00;font-size:11px">😨 VIX<br>⚠️ NSE blocked<br><span style="color:#6495b8;font-size:10px">Network pe NSE restricted hai</span></div>', unsafe_allow_html=True)

    # Watchlist removed

st.markdown("---")

# ══════════════════════════════════════════════════════════════
# ANALYSIS TABS
# ══════════════════════════════════════════════════════════════

tab1, tab2, tab3 = st.tabs(["📊 NIFTY Analysis", "🏦 BANK NIFTY Analysis", "📈 SENSEX Analysis"])

# ── SENSEX Tab — dedicated section (BSE pe options nahi hote) ──
for tab, instrument, name, spot in [
    (tab1, "NSE_INDEX|Nifty 50",   "NIFTY",      nifty_price),
    (tab2, "NSE_INDEX|Nifty Bank",  "BANK NIFTY", banknifty_price),
    (tab3, "BSE_INDEX|SENSEX",      "SENSEX",     sensex_price),
]:
    with tab:

        # ── Tab specific header ────────────────────────────
        tab_icon  = "📊" if name == "NIFTY" else ("🏦" if name == "BANK NIFTY" else "📈")
        tab_color = "#00bfff" if name == "NIFTY" else ("#a78bfa" if name == "BANK NIFTY" else "#ff9500")
        tab_price = f"₹{spot:,.2f}" if spot else "Loading..."
        tab_chg   = n_chg  if name == "NIFTY" else (bn_chg if name == "BANK NIFTY" else sx_chg)
        tab_pct   = n_pct  if name == "NIFTY" else (bn_pct if name == "BANK NIFTY" else sx_pct)
        tab_chg_color = "#00e676" if (tab_chg and tab_chg >= 0) else "#ff5252"
        tab_arrow     = "▲" if (tab_chg and tab_chg >= 0) else "▼"

        if tab_chg is not None:
            st.markdown(f"""
            <div style="background:#0d1929;border-radius:10px;padding:12px 18px;margin-bottom:12px;border:1px solid rgba(29,78,216,0.15);border-left:4px solid {tab_color}">
              <div style="display:flex;justify-content:space-between;align-items:center">
                <div>
                  <div style="font-size:10px;color:#6495b8;text-transform:uppercase;letter-spacing:2px;margin-bottom:2px">{tab_icon} {name} — Live Analysis</div>
                  <div style="font-size:28px;font-weight:900;color:{tab_color}">{tab_price}</div>
                </div>
                <div style="text-align:right">
                  <div style="font-size:18px;font-weight:800;color:{tab_chg_color}">{tab_arrow} {abs(tab_chg):,.2f} ({abs(tab_pct):.2f}%)</div>
                  <div style="font-size:11px;color:#6495b8;margin-top:2px">{mkt_icon} {mkt_status} &nbsp;|&nbsp; {now_ist().strftime('%I:%M %p')}</div>
                </div>
              </div>
            </div>""", unsafe_allow_html=True)
        else:
            st.markdown(f"""
            <div style="background:#0d1929;border-radius:10px;padding:12px 18px;margin-bottom:12px;border:1px solid rgba(29,78,216,0.15);border-left:4px solid {tab_color}">
              <div style="font-size:10px;color:#6495b8;text-transform:uppercase;letter-spacing:2px;margin-bottom:2px">{tab_icon} {name} — Live Analysis</div>
              <div style="font-size:28px;font-weight:900;color:{tab_color}">{tab_price}</div>
            </div>""", unsafe_allow_html=True)

        # ── EXPIRY SELECTOR ───────────────────────────────────
        exp_key      = f"expiry_{name}"
        exp_cache_key = f"exp_{instrument}"
        with st.spinner(f"{name} expiries fetch kar raha hoon..."):
            new_expiries = get_all_expiries(token, instrument)
        # Cache update
        if new_expiries:
            st.session_state.cache_expiries[exp_cache_key] = new_expiries
        all_expiries = st.session_state.cache_expiries.get(exp_cache_key, [])

        if all_expiries:
            col_exp1, col_exp2 = st.columns([3, 5])
            with col_exp1:
                selected_expiry = st.selectbox(
                    f"📅 {name} Expiry Select Karo:",
                    options=all_expiries,
                    index=0,
                    key=exp_key,
                    help="Nearest expiry default hai — aap weekly/monthly select kar sakte ho"
                )
            with col_exp2:
                st.markdown(f"""
                <div style="background:#0d1929;border-radius:8px;padding:10px 14px;border:1px solid rgba(29,78,216,0.15);margin-top:24px">
                  <span style="font-size:11px;color:#6495b8">Available Expiries: </span>
                  {'&nbsp;&nbsp;'.join([f'<span style="color:{"#00e676" if e == selected_expiry else "#6495b8"};font-size:12px;font-weight:{"bold" if e == selected_expiry else "normal"}">{e}</span>' for e in all_expiries[:8]])}
                </div>""", unsafe_allow_html=True)
        else:
            selected_expiry = None

        # ── Option chain with cache ───────────────────────────
        chain_cache_key = f"{instrument}_{selected_expiry}"
        with st.spinner(f"{name} data la raha hoon..."):
            new_chain, new_expiry = get_option_chain(token, instrument, selected_expiry, all_expiries)
        # Cache update — agar naya data aaya
        if new_chain:
            st.session_state.cache_chain[chain_cache_key] = (new_chain, new_expiry)
        # Use cache
        cached = st.session_state.cache_chain.get(chain_cache_key)
        chain_data = cached[0] if cached else None
        expiry     = cached[1] if cached else new_expiry

        if not chain_data:
            st.warning(f"⚠️ {name} data abhi available nahi.")
            continue
        result = calculate_analysis(chain_data, spot, expiry)
        if not result:
            continue

        # ── OI Wall Ticker update — har 3 min ────────────────
        now_ts = time.time()
        if now_ts - st.session_state.oi_wall_last_update >= 180 or not st.session_state.oi_wall_ticker:
            df_oi    = result["df"]
            atm_s    = result["atm_strike"]
            if atm_s is not None and not df_oi.empty:
                # Sabse badi Call OI wall (resistance)
                top_res_row = df_oi[df_oi["Strike"] > atm_s].nlargest(1, "Call OI")
                # Sabse badi Put OI wall (support)
                top_sup_row = df_oi[df_oi["Strike"] < atm_s].nlargest(1, "Put OI")

                res_strike = int(top_res_row["Strike"].values[0]) if not top_res_row.empty else None
                res_oi     = int(top_res_row["Call OI"].values[0]) if not top_res_row.empty else 0
                sup_strike = int(top_sup_row["Strike"].values[0]) if not top_sup_row.empty else None
                sup_oi     = int(top_sup_row["Put OI"].values[0]) if not top_sup_row.empty else 0

                # Update ticker list — existing entry replace karo agar same name hai
                ticker_list = [t for t in st.session_state.oi_wall_ticker if t["name"] != name]
                ticker_list.append({
                    "name":       name,
                    "resistance": res_strike,
                    "res_oi":     res_oi,
                    "support":    sup_strike,
                    "sup_oi":     sup_oi,
                    "atm":        atm_s,
                    "spot":       round(spot, 0) if spot else 0,
                    "updated":    now_ist().strftime("%I:%M %p"),
                })
                st.session_state.oi_wall_ticker   = ticker_list
                st.session_state.oi_wall_last_update = now_ts

        pcr = result["pcr"]
        label, css_class = sentiment_label(pcr)
        st.markdown(f'<div style="font-size:12px;color:#00e676;margin-bottom:8px">📅 Selected Expiry: <b>{expiry}</b></div>', unsafe_allow_html=True)

        # ── MARKET SENTIMENT ──────────────────────────────────
        st.markdown("---")
        st.markdown('<div class="sec-header" style="border-left:3px solid #e040fb">🧠 Market Sentiment</div>', unsafe_allow_html=True)
        c1, c2, c3, c4 = st.columns(4)
        with c1: st.metric("PCR Ratio", pcr)
        with c2: st.metric("Total Call OI", f"{result['total_call_oi']:,}")
        with c3: st.metric("Total Put OI",  f"{result['total_put_oi']:,}")
        with c4:
            st.markdown("**Sentiment**")
            st.markdown(f'<div class="{css_class}">{label}</div>', unsafe_allow_html=True)

        # ── KEY LEVELS ────────────────────────────────────────
        st.markdown('<div class="sec-header" style="border-left:3px solid #ff8c00">🎯 Key Levels</div>', unsafe_allow_html=True)

        # Row 1: Key Levels
        kc1, kc2, kc3 = st.columns(3)
        with kc1:
            st.markdown("**🔴 Resistance** *(Max Call OI)*")
            for r in result["resistance_levels"]:
                r_int = int(r)
                # OI value bhi dikhao
                r_oi = result["df"][result["df"]["Strike"] == r]["Call OI"].values
                r_oi_str = f'<span style="font-size:11px;color:#ff525280;margin-left:8px">{int(r_oi[0]):,} OI</span>' if len(r_oi) > 0 else ""
                st.markdown(f'<div class="key-level-resistance">🔴 {r_int:,}{r_oi_str}</div>', unsafe_allow_html=True)
        with kc2:
            st.markdown("**⭐ Max Pain**")
            if result["max_pain"]:
                st.markdown(f'<div class="fair-value">Max Pain<br><span style="font-size:32px;font-weight:900">{int(result["max_pain"]):,}</span></div>', unsafe_allow_html=True)
            if spot and result["atm_strike"]:
                st.markdown(f'<div style="margin-top:8px;padding:8px;background:#0d1929;border-radius:8px;text-align:center;border:1px solid rgba(29,78,216,0.15);color:white;font-size:13px">ATM: <b>{int(result["atm_strike"]):,}</b> | Spot: <b>{spot:,.0f}</b></div>', unsafe_allow_html=True)
        with kc3:
            st.markdown("**🟢 Support** *(Max Put OI)*")
            for s in result["support_levels"]:
                s_int = int(s)
                s_oi = result["df"][result["df"]["Strike"] == s]["Put OI"].values
                s_oi_str = f'<span style="font-size:11px;color:#00e67680;margin-left:8px">{int(s_oi[0]):,} OI</span>' if len(s_oi) > 0 else ""
                st.markdown(f'<div class="key-level-support">🟢 {s_int:,}{s_oi_str}</div>', unsafe_allow_html=True)

        st.markdown("---")

        # ══════════════════════════════════════════════
        # 🎯 STRIKE DETAIL + ⚖️ COMPARISON — Max Pain ke neeche
        # ══════════════════════════════════════════════
        if not result["df"].empty and result["atm_strike"]:
            atm_s2   = result["atm_strike"]
            df_all   = result["df"].copy()
            atm_pos2 = df_all.index.get_loc(df_all[df_all["Strike"] == atm_s2].index[0]) if not df_all[df_all["Strike"] == atm_s2].empty else len(df_all)//2
            all_strikes2 = sorted(df_all["Strike"].astype(int).tolist())
            atm_def2 = all_strikes2.index(int(atm_s2)) if int(atm_s2) in all_strikes2 else len(all_strikes2)//2

            def fmtn2(v):
                if v >= 10000000: return f"{v/10000000:.2f}Cr"
                elif v >= 100000: return f"{v/100000:.1f}L"
                elif v >= 1000:   return f"{v/1000:.1f}K"
                return f"{int(v):,}"

            # ── Single Strike Selector ────────────────────────
            st.markdown('<div class="sec-header" style="border-left:3px solid #f59e0b">🎯 Strike Detail — Call & Put Alag Select Karo</div>', unsafe_allow_html=True)

            # ── 2 alag selectors — Call aur Put independently ──
            sc1, sc2 = st.columns(2)
            with sc1:
                sel_call_strike = st.selectbox(
                    f"📞 CALL Strike ({name}):",
                    options=all_strikes2,
                    index=atm_def2,
                    key=f"call_sel_{name}",
                    format_func=lambda x: f"{'⭐ ' if x == int(atm_s2) else ''}{x:,}"
                )
            with sc2:
                sel_put_strike = st.selectbox(
                    f"📉 PUT Strike ({name}):",
                    options=all_strikes2,
                    index=atm_def2,
                    key=f"put_sel_{name}",
                    format_func=lambda x: f"{'⭐ ' if x == int(atm_s2) else ''}{x:,}"
                )

            # ── Call data from Call strike ──
            call_row = df_all[df_all["Strike"] == sel_call_strike]
            put_row  = df_all[df_all["Strike"] == sel_put_strike]

            # Call side
            if not call_row.empty:
                r_c = call_row.iloc[0]
                c_ltp_s2 = round(float(r_c["Call LTP"]), 1)
                c_oi_s2  = int(r_c["Call OI"])
                c_chg_s2 = int(r_c["Call OI Change"])
                c_iv_s2  = float(r_c["Call IV"])
                c_vol_s2 = 0
                if chain_data:
                    for item in chain_data:
                        if item.get("strike_price") == sel_call_strike:
                            c_vol_s2 = item.get("call_options", {}).get("market_data", {}).get("volume", 0) or 0
                            break
            else:
                c_ltp_s2 = c_oi_s2 = c_chg_s2 = c_iv_s2 = c_vol_s2 = 0

            # Put side
            if not put_row.empty:
                r_p = put_row.iloc[0]
                p_ltp_s2 = round(float(r_p["Put LTP"]), 1)
                p_oi_s2  = int(r_p["Put OI"])
                p_chg_s2 = int(r_p["Put OI Change"])
                p_iv_s2  = float(r_p["Put IV"])
                p_vol_s2 = 0
                if chain_data:
                    for item in chain_data:
                        if item.get("strike_price") == sel_put_strike:
                            p_vol_s2 = item.get("put_options", {}).get("market_data", {}).get("volume", 0) or 0
                            break
            else:
                p_ltp_s2 = p_oi_s2 = p_chg_s2 = p_iv_s2 = p_vol_s2 = 0

            c_chg_col2 = "#00e676" if c_chg_s2 > 0 else "#ff5252"
            p_chg_col2 = "#00e676" if p_chg_s2 > 0 else "#ff5252"

            # Strike type labels
            def strike_type(s, sp):
                if s > sp:   return "OTM Call / ITM Put", "#ff8c00"
                elif s < sp: return "ITM Call / OTM Put", "#00bfff"
                else:        return "⭐ ATM", "#ffd600"

            c_stype, c_scol = strike_type(sel_call_strike, spot)
            p_stype, p_scol = strike_type(sel_put_strike,  spot)

            sd1, sd2 = st.columns(2)
            with sd1:
                st.markdown(f"""
                <div style="background:#ff525212;border:1.5px solid #ff5252;border-radius:12px;padding:14px">
                  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
                    <div style="font-size:12px;color:#ff5252;font-weight:800;text-transform:uppercase">📞 CALL — {sel_call_strike:,}</div>
                    <div style="font-size:10px;color:{c_scol};background:{c_scol}18;padding:2px 8px;border-radius:10px;border:1px solid {c_scol}40">{c_stype}</div>
                  </div>
                  <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
                    <div style="background:#060e1a;border-radius:8px;padding:10px;text-align:center">
                      <div style="font-size:10px;color:#6495b8;margin-bottom:3px">💰 LTP</div>
                      <div style="font-family:'JetBrains Mono',monospace;font-size:26px;font-weight:900;color:#ff5252">₹{c_ltp_s2}</div>
                    </div>
                    <div style="background:#060e1a;border-radius:8px;padding:10px;text-align:center">
                      <div style="font-size:10px;color:#6495b8;margin-bottom:3px">📊 Volume</div>
                      <div style="font-family:'JetBrains Mono',monospace;font-size:20px;font-weight:700;color:#ff8888">{fmtn2(c_vol_s2)}</div>
                    </div>
                    <div style="background:#060e1a;border-radius:8px;padding:10px;text-align:center">
                      <div style="font-size:10px;color:#6495b8;margin-bottom:3px">📈 OI</div>
                      <div style="font-family:'JetBrains Mono',monospace;font-size:18px;font-weight:700;color:#ff5252">{fmtn2(c_oi_s2)}</div>
                    </div>
                    <div style="background:#060e1a;border-radius:8px;padding:10px;text-align:center">
                      <div style="font-size:10px;color:#6495b8;margin-bottom:3px">🔄 OI Change</div>
                      <div style="font-family:'JetBrains Mono',monospace;font-size:18px;font-weight:700;color:{c_chg_col2}">{"▲" if c_chg_s2>0 else "▼"} {fmtn2(abs(c_chg_s2))}</div>
                    </div>
                  </div>
                  <div style="margin-top:8px;padding:6px;background:#060e1a;border-radius:6px;text-align:center;font-size:11px;color:#6495b8">
                    IV: <b style="color:#ff5252">{c_iv_s2:.1f}%</b> &nbsp;|&nbsp;
                    {"🔴 Short Build Up" if c_chg_s2>0 else "🟠 Long Unwind" if c_chg_s2<0 else "⚪ No Change"}
                  </div>
                </div>""", unsafe_allow_html=True)

            with sd2:
                st.markdown(f"""
                <div style="background:#00e67612;border:1.5px solid #00e676;border-radius:12px;padding:14px">
                  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
                    <div style="font-size:12px;color:#00e676;font-weight:800;text-transform:uppercase">📉 PUT — {sel_put_strike:,}</div>
                    <div style="font-size:10px;color:{p_scol};background:{p_scol}18;padding:2px 8px;border-radius:10px;border:1px solid {p_scol}40">{p_stype}</div>
                  </div>
                  <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
                    <div style="background:#060e1a;border-radius:8px;padding:10px;text-align:center">
                      <div style="font-size:10px;color:#6495b8;margin-bottom:3px">💰 LTP</div>
                      <div style="font-family:'JetBrains Mono',monospace;font-size:26px;font-weight:900;color:#00e676">₹{p_ltp_s2}</div>
                    </div>
                    <div style="background:#060e1a;border-radius:8px;padding:10px;text-align:center">
                      <div style="font-size:10px;color:#6495b8;margin-bottom:3px">📊 Volume</div>
                      <div style="font-family:'JetBrains Mono',monospace;font-size:20px;font-weight:700;color:#88ff88">{fmtn2(p_vol_s2)}</div>
                    </div>
                    <div style="background:#060e1a;border-radius:8px;padding:10px;text-align:center">
                      <div style="font-size:10px;color:#6495b8;margin-bottom:3px">📈 OI</div>
                      <div style="font-family:'JetBrains Mono',monospace;font-size:18px;font-weight:700;color:#00e676">{fmtn2(p_oi_s2)}</div>
                    </div>
                    <div style="background:#060e1a;border-radius:8px;padding:10px;text-align:center">
                      <div style="font-size:10px;color:#6495b8;margin-bottom:3px">🔄 OI Change</div>
                      <div style="font-family:'JetBrains Mono',monospace;font-size:18px;font-weight:700;color:{p_chg_col2}">{"▲" if p_chg_s2>0 else "▼"} {fmtn2(abs(p_chg_s2))}</div>
                    </div>
                  </div>
                  <div style="margin-top:8px;padding:6px;background:#060e1a;border-radius:6px;text-align:center;font-size:11px;color:#6495b8">
                    IV: <b style="color:#00e676">{p_iv_s2:.1f}%</b> &nbsp;|&nbsp;
                    {"🟢 Long Build Up" if p_chg_s2>0 else "🟠 Short Cover" if p_chg_s2<0 else "⚪ No Change"}
                  </div>
                </div>""", unsafe_allow_html=True)

            # ── Interpretation — dono strikes ke basis pe ──
            same = sel_call_strike == sel_put_strike
            if same:
                if   c_chg_s2 > 0 and p_chg_s2 < 0: interp2, ic2 = "🔴 SHORT BUILD — Bears strong!", "#ff5252"
                elif p_chg_s2 > 0 and c_chg_s2 < 0: interp2, ic2 = "🟢 LONG BUILD — Bulls strong!",  "#00e676"
                elif c_chg_s2 > 0 and p_chg_s2 > 0: interp2, ic2 = "⚡ DONO BADH RAHE — Mixed signal", "#ffd600"
                elif c_chg_s2 < 0 and p_chg_s2 < 0: interp2, ic2 = "🏃 DONO GHATT RAHE — Exit signal", "#ff8c00"
                else:                                 interp2, ic2 = "⚪ Koi activity nahi",              "#6495b8"
            else:
                interp2 = f"📞 Call {sel_call_strike:,} &nbsp;vs&nbsp; 📉 Put {sel_put_strike:,} — Alag strikes compare ho rahe hain"
                ic2 = "#a78bfa"
            st.markdown(f'<div style="background:{ic2}15;border:1.5px solid {ic2};border-radius:8px;padding:10px;margin-top:8px;text-align:center;font-size:14px;font-weight:bold;color:{ic2}">{interp2}</div>', unsafe_allow_html=True)

        st.markdown("---")

        # ── FAIR VALUE TABLE ──────────────────────────────────
        st.markdown('<div class="sec-header" style="border-left:3px solid #60a5fa">💎 Fair Value — ATM ± 5 Strikes</div>', unsafe_allow_html=True)
        fv_df = result["fair_value_df"]
        atm   = result["atm_strike"]

        if not fv_df.empty and "Call Fair Value" in fv_df.columns:
            # Summary
            mehnga = sasta = fair_c = 0
            for _, row in fv_df.iterrows():
                for ltp_col, fv_col in [("Call LTP","Call Fair Value"),("Put LTP","Put Fair Value")]:
                    diff = row[ltp_col] - row[fv_col] if pd.notna(row[fv_col]) else 0
                    if diff > 5: mehnga += 1
                    elif diff < -5: sasta += 1
                    else: fair_c += 1

            sm1, sm2, sm3 = st.columns(3)
            with sm1:
                st.markdown(f'<div style="background:#2d0a0a;border:1px solid #ff525230;border-radius:10px;padding:12px;text-align:center"><div style="font-size:10px;color:#ff525260;text-transform:uppercase;margin-bottom:4px">🔴 MEHNGA</div><div style="font-size:24px;font-weight:bold;color:#ff5252">{mehnga}</div><div style="font-size:10px;color:#ff525260">Options costly hain</div></div>', unsafe_allow_html=True)
            with sm2:
                st.markdown(f'<div style="background:#0a2d15;border:1px solid #00e67630;border-radius:10px;padding:12px;text-align:center"><div style="font-size:10px;color:#00e67660;text-transform:uppercase;margin-bottom:4px">🟢 SASTA</div><div style="font-size:24px;font-weight:bold;color:#00e676">{sasta}</div><div style="font-size:10px;color:#00e67660">Options cheap hain</div></div>', unsafe_allow_html=True)
            with sm3:
                st.markdown(f'<div style="background:#1a1a2d;border:1px solid #6495b830;border-radius:10px;padding:12px;text-align:center"><div style="font-size:10px;color:#6495b8;text-transform:uppercase;margin-bottom:4px">⚪ FAIR</div><div style="font-size:24px;font-weight:bold;color:#90b8d8">{fair_c}</div><div style="font-size:10px;color:#6495b8">Fair price pe hain</div></div>', unsafe_allow_html=True)

            # Build table
            fv_rows = []
            for _, row in fv_df.iterrows():
                s        = int(row["Strike"])
                c_ltp    = round(float(row["Call LTP"]), 1)
                c_fv     = round(float(row["Call Fair Value"]), 1) if pd.notna(row["Call Fair Value"]) else 0
                p_ltp    = round(float(row["Put LTP"]), 1)
                p_fv     = round(float(row["Put Fair Value"]), 1) if pd.notna(row["Put Fair Value"]) else 0
                straddle = round(float(row.get("Straddle Price", c_ltp + p_ltp)), 1)
                c_diff   = round(c_ltp - c_fv, 1)
                p_diff   = round(p_ltp - p_fv, 1)

                fv_rows.append({
                    "Call LTP": f"{c_ltp:.1f}", "Call FV": f"{c_fv:.1f}",
                    "C Diff": f"{c_diff:+.1f}", "C Status": fv_option_status(c_diff),
                    "Strike": f"⭐{s}" if s == atm else str(s),
                    "Put LTP": f"{p_ltp:.1f}", "Put FV": f"{p_fv:.1f}",
                    "P Diff": f"{p_diff:+.1f}", "P Status": fv_option_status(p_diff),
                    "Straddle": f"{straddle:.1f}",
                })

            # ── Dark HTML table — no white background ─────────
            tbl_rows_html = ""
            for r in fv_rows:
                is_atm   = "⭐" in r["Strike"]
                row_bg   = "background:#1a1500;" if is_atm else ""
                row_bdr  = "border-left:3px solid #ffd600;" if is_atm else "border-left:3px solid transparent;"

                # C Diff color
                try:
                    cd = float(r["C Diff"].replace("+",""))
                    c_diff_col = "#ff5252" if cd > 2 else ("#00e676" if cd < -2 else "#6495b8")
                except: c_diff_col = "#6495b8"

                # P Diff color
                try:
                    pd_ = float(r["P Diff"].replace("+",""))
                    p_diff_col = "#ff5252" if pd_ > 2 else ("#00e676" if pd_ < -2 else "#6495b8")
                except: p_diff_col = "#6495b8"

                # Status tags
                def status_html(s):
                    if "MEHNGA" in s:
                        return '<span style="color:#ff5252;font-weight:800;font-size:12px">🔴 MEHNGA</span>'
                    elif "SASTA" in s:
                        return '<span style="color:#00e676;font-weight:800;font-size:12px">🟢 SASTA</span>'
                    return '<span style="color:#6495b8;font-size:12px">⚪ FAIR</span>'

                strike_disp = f'<span style="color:#ffd600;font-weight:900">{r["Strike"]}</span>' if is_atm else f'<span style="color:#60a5fa">{r["Strike"]}</span>'

                tbl_rows_html += f"""
                <tr style="{row_bg}{row_bdr}border-bottom:1px solid rgba(29,78,216,0.1);">
                  <td style="padding:7px 10px;color:#8ab8d8;font-family:'JetBrains Mono',monospace;font-size:12px">{r["Call LTP"]}</td>
                  <td style="padding:7px 10px;color:#6495b8;font-size:12px">{r["Call FV"]}</td>
                  <td style="padding:7px 10px;color:{c_diff_col};font-weight:700;font-size:12px">{r["C Diff"]}</td>
                  <td style="padding:7px 10px">{status_html(r["C Status"])}</td>
                  <td style="padding:7px 10px;text-align:center">{strike_disp}</td>
                  <td style="padding:7px 10px;color:#8ab8d8;font-family:'JetBrains Mono',monospace;font-size:12px">{r["Put LTP"]}</td>
                  <td style="padding:7px 10px;color:#6495b8;font-size:12px">{r["Put FV"]}</td>
                  <td style="padding:7px 10px;color:{p_diff_col};font-weight:700;font-size:12px">{r["P Diff"]}</td>
                  <td style="padding:7px 10px">{status_html(r["P Status"])}</td>
                  <td style="padding:7px 10px;color:#a78bfa;font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:700">{r["Straddle"]}</td>
                </tr>"""

            st.markdown(f"""
            <div style="overflow-x:auto;border-radius:12px;border:1px solid rgba(29,78,216,0.2);margin-top:10px">
              <table style="width:100%;border-collapse:collapse;background:#060e1a;font-family:'Inter',sans-serif">
                <thead>
                  <tr style="background:rgba(29,78,216,0.15);border-bottom:1px solid rgba(29,78,216,0.3)">
                    <th style="padding:8px 10px;text-align:left;color:#4e7a96;font-size:10px;text-transform:uppercase;letter-spacing:1px;font-weight:600">Call LTP</th>
                    <th style="padding:8px 10px;text-align:left;color:#4e7a96;font-size:10px;text-transform:uppercase;letter-spacing:1px;font-weight:600">Call FV</th>
                    <th style="padding:8px 10px;text-align:left;color:#4e7a96;font-size:10px;text-transform:uppercase;letter-spacing:1px;font-weight:600">C Diff</th>
                    <th style="padding:8px 10px;text-align:left;color:#4e7a96;font-size:10px;text-transform:uppercase;letter-spacing:1px;font-weight:600">C Status</th>
                    <th style="padding:8px 10px;text-align:center;color:#ffd600;font-size:10px;text-transform:uppercase;letter-spacing:1px;font-weight:600">Strike</th>
                    <th style="padding:8px 10px;text-align:left;color:#4e7a96;font-size:10px;text-transform:uppercase;letter-spacing:1px;font-weight:600">Put LTP</th>
                    <th style="padding:8px 10px;text-align:left;color:#4e7a96;font-size:10px;text-transform:uppercase;letter-spacing:1px;font-weight:600">Put FV</th>
                    <th style="padding:8px 10px;text-align:left;color:#4e7a96;font-size:10px;text-transform:uppercase;letter-spacing:1px;font-weight:600">P Diff</th>
                    <th style="padding:8px 10px;text-align:left;color:#4e7a96;font-size:10px;text-transform:uppercase;letter-spacing:1px;font-weight:600">P Status</th>
                    <th style="padding:8px 10px;text-align:left;color:#a78bfa;font-size:10px;text-transform:uppercase;letter-spacing:1px;font-weight:600">Straddle</th>
                  </tr>
                </thead>
                <tbody>{tbl_rows_html}</tbody>
              </table>
            </div>""", unsafe_allow_html=True)

            st.markdown("""<div style="display:flex;gap:10px;margin-top:6px;flex-wrap:wrap;font-size:12px">
              <span style="color:#ff5252;font-weight:bold">🔴 MEHNGA = Costly (avoid buying)</span>
              <span style="color:#00e676;font-weight:bold">🟢 SASTA = Cheap (buy opportunity)</span>
              <span style="color:#90b8d8">⚪ FAIR = Sahi price</span>
            </div>""", unsafe_allow_html=True)

        st.markdown("---")

        # ── BIG PLAYERS OI ────────────────────────────────────
        st.markdown('<div class="sec-header" style="border-left:3px solid #a78bfa">🐋 Big Players OI</div>', unsafe_allow_html=True)
        df = result["df"]
        if not df.empty and result["atm_strike"]:
            atm_idx_list = df[df["Strike"] == result["atm_strike"]].index.tolist()
            if atm_idx_list:
                atm_pos = df.index.get_loc(atm_idx_list[0])
                df_d    = df.iloc[max(0, atm_pos-10):min(len(df), atm_pos+11)]
                atm     = result["atm_strike"]
                max_pain = result["max_pain"]

                total_call  = int(df_d["Call OI"].sum())
                total_put   = int(df_d["Put OI"].sum())
                call_pct    = round(total_call / (total_call + total_put) * 100, 1) if (total_call+total_put) > 0 else 50
                put_pct     = round(100 - call_pct, 1)
                top_call_strike = result["max_call_oi_strike"]
                top_put_strike  = result["max_put_oi_strike"]

                if put_pct > call_pct + 10:
                    dom_color, dom_bg, dom_border = "#00e676", "#00e67622", "#00e676"
                    dominator = "🟢 PUT WRITERS DOMINANT"
                    dom_msg   = "Bulls in control — Market upar jaane ki zyada possibility"
                elif call_pct > put_pct + 10:
                    dom_color, dom_bg, dom_border = "#ff5252", "#ff525222", "#ff5252"
                    dominator = "🔴 CALL WRITERS DOMINANT"
                    dom_msg   = "Bears in control — Market neeche jaane ki possibility"
                else:
                    dom_color, dom_bg, dom_border = "#ffd600", "#ffd60022", "#ffd600"
                    dominator = "🟡 BALANCED"
                    dom_msg   = "Market range-bound — Koi clear dominator nahi"

                st.markdown(f"""
                <div style="background:{dom_bg};border:2px solid {dom_border};border-radius:12px;padding:14px 20px;margin:10px 0;text-align:center">
                  <div style="font-size:26px;font-weight:900;color:{dom_color}">{dominator}</div>
                  <div style="font-size:13px;color:#ccc;margin-top:5px">{dom_msg}</div>
                </div>""", unsafe_allow_html=True)

                ci1, ci2, ci3, ci4, ci5, ci6 = st.columns(6)
                with ci1: st.markdown(f'<div class="metric-card" style="text-align:center"><div style="font-size:10px;color:#888">ATM Strike</div><div style="font-size:24px;font-weight:900;color:#ffd600">{atm}</div></div>', unsafe_allow_html=True)
                with ci2: st.markdown(f'<div class="metric-card" style="text-align:center"><div style="font-size:10px;color:#888">Spot Price</div><div style="font-size:24px;font-weight:900;color:#00bfff">{spot:,.0f}</div></div>', unsafe_allow_html=True)
                with ci3: st.markdown(f'<div class="metric-card" style="text-align:center"><div style="font-size:10px;color:#888">Max Pain</div><div style="font-size:24px;font-weight:900;color:#ff9500">{max_pain}</div></div>', unsafe_allow_html=True)
                with ci4: st.markdown(f'<div class="metric-card" style="text-align:center"><div style="font-size:10px;color:#888">Call OI %</div><div style="font-size:24px;font-weight:900;color:#ff5252">{call_pct}%</div></div>', unsafe_allow_html=True)
                with ci5: st.markdown(f'<div class="metric-card" style="text-align:center"><div style="font-size:10px;color:#888">Put OI %</div><div style="font-size:24px;font-weight:900;color:#00e676">{put_pct}%</div></div>', unsafe_allow_html=True)
                with ci6: st.markdown(f'<div class="metric-card" style="text-align:center"><div style="font-size:10px;color:#888">PCR</div><div style="font-size:24px;font-weight:900;color:#a78bfa">{round(total_put/total_call,2) if total_call else 0}</div></div>', unsafe_allow_html=True)

                # OI bar
                st.markdown(f"""
                <div style="display:flex;justify-content:space-between;font-size:12px;color:#888;margin:10px 0 4px">
                  <span>🔴 Call Writers (Bears) — {call_pct}%</span>
                  <span>🟢 Put Writers (Bulls) — {put_pct}%</span>
                </div>
                <div style="height:14px;background:#ff525255;border-radius:7px;overflow:hidden;display:flex">
                  <div style="width:{call_pct}%;background:#ff5252;border-radius:7px 0 0 7px"></div>
                  <div style="flex:1;background:#00e676;border-radius:0 7px 7px 0"></div>
                </div>""", unsafe_allow_html=True)

                if top_call_strike and top_put_strike:
                    top_call_chg_strike = result["max_call_chg_strike"]
                    top_put_chg_strike  = result["max_put_chg_strike"]
                    # OI change available hai ya nahi check karo
                    oi_chg_available = result["atm_call_chg"] != 0 or result["atm_put_chg"] != 0
                    active_label = "" if oi_chg_available else " ⏳ (2+ refreshes ke baad)"
                    st.markdown(f"""
                    <div style="background:#0d1929;border-radius:8px;padding:10px 14px;margin:10px 0;font-size:13px;border-left:3px solid #ffd600;color:white">
                      🔴 Max Resistance: <b style="color:#ff5252">{top_call_strike}</b> &nbsp;|&nbsp;
                      🟢 Max Support: <b style="color:#00e676">{top_put_strike}</b> &nbsp;|&nbsp;
                      📦 Range: <b style="color:#ffd600">{top_put_strike} — {top_call_strike}</b>
                    </div>
                    <div style="background:#0d1929;border-radius:8px;padding:10px 14px;margin:4px 0 10px;font-size:13px;border-left:3px solid #a78bfa;color:white">
                      🔴 Active Writing: <b style="color:#ff5252">{top_call_chg_strike}</b> &nbsp;|&nbsp;
                      🟢 Active Support: <b style="color:#00e676">{top_put_chg_strike}</b> &nbsp;|&nbsp;
                      📊 Active Range: <b style="color:#a78bfa">{top_put_chg_strike} — {top_call_chg_strike}</b>
                      <span style="color:#6495b8;font-size:11px">{active_label}</span>
                    </div>""", unsafe_allow_html=True)

                # ── Chart toggle: OI vs OI Change ─────────────
                chart_mode = st.radio(
                    "📊 Chart Mode:",
                    ["OI (Total)", "OI Change (Increase/Decrease)"],
                    horizontal=True,
                    key=f"chart_mode_{name}"
                )

                # df_d columns for chart
                df_d = df_d.copy()
                df_d["TF Call OI Change"] = df_d["Call OI Change"]
                df_d["TF Put OI Change"]  = df_d["Put OI Change"]
                net_tf_call = int(df_d["TF Call OI Change"].sum())
                net_tf_put  = int(df_d["TF Put OI Change"].sum())
                nc_col = "#ff5252" if net_tf_call >= 0 else "#00e676"
                np_col = "#00e676" if net_tf_put  >= 0 else "#ff5252"
                nc_lbl = "Bears active" if net_tf_call > 0 else "Bears covering"
                np_lbl = "Bulls active" if net_tf_put  > 0 else "Bulls covering"
                tf_label_str = "Last refresh"

                # OI Chart
                fig_oi = go.Figure()

                if chart_mode == "OI (Total)":
                    # ── Original OI bars ──────────────────────
                    fig_oi.add_trace(go.Bar(
                        x=df_d["Strike"], y=df_d["Call OI"],
                        name="Call OI (Resistance)",
                        marker_color="#ff5252", marker_opacity=0.85
                    ))
                    fig_oi.add_trace(go.Bar(
                        x=df_d["Strike"], y=df_d["Put OI"],
                        name="Put OI (Support)",
                        marker_color="#00e676", marker_opacity=0.85
                    ))
                    chart_title = f"<b>{name} OI — Big Players Position</b>"
                    y_title = "Open Interest"

                else:
                    # ── OI Change — Timeframe based ───────────
                    call_inc = df_d["TF Call OI Change"].clip(lower=0)
                    call_dec = df_d["TF Call OI Change"].clip(upper=0).abs()
                    put_inc  = df_d["TF Put OI Change"].clip(lower=0)
                    put_dec  = df_d["TF Put OI Change"].clip(upper=0).abs()

                    fig_oi.add_trace(go.Bar(
                        x=df_d["Strike"], y=call_inc,
                        name="Call OI Increase ▲",
                        marker=dict(color="#ff5252", opacity=0.9),
                        hovertemplate="Strike: %{x}<br>Call Increase: +%{y:,.0f}<extra></extra>"
                    ))
                    fig_oi.add_trace(go.Bar(
                        x=df_d["Strike"], y=call_dec,
                        name="Call OI Decrease ▼",
                        marker=dict(color="#ff5252", opacity=0.3,
                                    pattern=dict(shape="/", fgcolor="#ff5252", bgcolor="rgba(255,82,82,0.1)")),
                        hovertemplate="Strike: %{x}<br>Call Decrease: -%{y:,.0f}<extra></extra>"
                    ))
                    fig_oi.add_trace(go.Bar(
                        x=df_d["Strike"], y=put_inc,
                        name="Put OI Increase ▲",
                        marker=dict(color="#00e676", opacity=0.9),
                        hovertemplate="Strike: %{x}<br>Put Increase: +%{y:,.0f}<extra></extra>"
                    ))
                    fig_oi.add_trace(go.Bar(
                        x=df_d["Strike"], y=put_dec,
                        name="Put OI Decrease ▼",
                        marker=dict(color="#00e676", opacity=0.3,
                                    pattern=dict(shape="\\", fgcolor="#00e676", bgcolor="rgba(0,230,118,0.1)")),
                        hovertemplate="Strike: %{x}<br>Put Decrease: -%{y:,.0f}<extra></extra>"
                    ))

                    # Net OI change summary — timeframe based
                    net_col_c = "#ff5252" if net_tf_call >= 0 else "#00e676"
                    net_col_p = "#00e676" if net_tf_put  >= 0 else "#ff5252"
                    st.markdown(f"""
                    <div style="display:flex;gap:16px;margin-bottom:8px;flex-wrap:wrap">
                      <div style="background:#ff525215;border:1px solid #ff525240;border-radius:8px;padding:8px 16px;font-size:13px">
                        🔴 Call OI Net Change: <b style="color:{net_col_c}">{"+" if net_tf_call>=0 else ""}{net_tf_call:,}</b>
                        <span style="color:#6495b8;font-size:11px;margin-left:8px">{nc_lbl}</span>
                      </div>
                      <div style="background:#00e67615;border:1px solid #00e67640;border-radius:8px;padding:8px 16px;font-size:13px">
                        🟢 Put OI Net Change: <b style="color:{net_col_p}">{"+" if net_tf_put>=0 else ""}{net_tf_put:,}</b>
                        <span style="color:#6495b8;font-size:11px;margin-left:8px">{np_lbl}</span>
                      </div>
                    </div>
                    """, unsafe_allow_html=True)

                    chart_title = f"<b>{name} OI Change — Kitna Badha / Ghata</b>"
                    y_title = "OI Change"

                fig_oi.add_vline(x=atm, line_width=2, line_dash="dash", line_color="#ffd600",
                                 annotation_text=f"ATM {atm}", annotation_font_color="#ffd600")
                if spot:
                    fig_oi.add_vline(x=spot, line_width=1.5, line_dash="dot", line_color="#00bfff",
                                     annotation_text=f"Spot {spot:,.0f}", annotation_font_color="#00bfff")
                if max_pain:
                    fig_oi.add_vline(x=max_pain, line_width=2, line_dash="longdash", line_color="#ff9500",
                                     annotation_text=f"⭐ Max Pain {max_pain}", annotation_font_color="#ff9500")
                fig_oi.update_layout(
                    title=dict(text=chart_title, font=dict(size=14, color="#90b8d8", family="Inter")),
                    barmode="group",
                    paper_bgcolor="rgba(0,0,0,0)",
                    plot_bgcolor="#060e1a",
                    font=dict(color="#7aa0be", family="Inter"),
                    height=440,
                    margin=dict(l=10, r=10, t=50, b=10),
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1,
                                font=dict(size=11, color="#90b8d8"),
                                bgcolor="rgba(0,0,0,0)"),
                    xaxis=dict(title="Strike Price", gridcolor="rgba(29,78,216,0.1)", zeroline=False,
                               tickfont=dict(size=10, family="JetBrains Mono"), tickcolor="#4e7a96"),
                    yaxis=dict(title=y_title, gridcolor="rgba(29,78,216,0.1)", zeroline=False,
                               tickfont=dict(size=10, family="JetBrains Mono"), tickcolor="#4e7a96"),
                    bargap=0.15,
                )
                st.plotly_chart(fig_oi, use_container_width=True)

                # Legend explanation
                if chart_mode == "OI Change (Increase/Decrease)":
                    st.markdown("""<div style="display:flex;gap:16px;margin-top:4px;font-size:11px;flex-wrap:wrap">
                      <span>🔴 <b>Solid Red</b> = Call OI Badha (Bears position add kar rahe)</span>
                      <span>🔴 <b>Light Red</b> = Call OI Ghata (Bears exit kar rahe)</span>
                      <span>🟢 <b>Solid Green</b> = Put OI Badha (Bulls position add kar rahe)</span>
                      <span>🟢 <b>Light Green</b> = Put OI Ghata (Bulls exit kar rahe)</span>
                    </div>""", unsafe_allow_html=True)

                # ══ TEJI / MANDI SCANNER ══
                st.markdown('<div class="sec-header" style="border-left:3px solid #a78bfa">📋 OI & OI Change Table</div>', unsafe_allow_html=True)

                total_chg = abs(result["atm_call_chg"]) + abs(result["atm_put_chg"])
                if total_chg == 0:
                    st.markdown("""
                    <div style="background:#1d4ed812;border:1px solid #1d4ed8;border-radius:8px;padding:8px 14px;font-size:13px;color:#60a5fa">
                    ℹ️ <b>Pehla refresh hai</b> — OI Change track ho raha hai. <b>Ek baar aur Refresh dabao</b> — phir live OI Change dikhega!
                    </div>""", unsafe_allow_html=True)

                oi_raw = df_d[["Strike","Call OI","Call OI Change","Put OI","Put OI Change","Call LTP","Put LTP"]].copy()
                oi_raw["Strike"] = oi_raw["Strike"].astype(int)

                if not fv_df.empty and "Call Fair Value" in fv_df.columns:
                    fv_merge = fv_df[["Strike","Call Fair Value","Put Fair Value"]].copy()
                    fv_merge["Strike"] = fv_merge["Strike"].astype(int)
                    oi_raw = oi_raw.merge(fv_merge, on="Strike", how="left")
                else:
                    oi_raw["Call Fair Value"] = None
                    oi_raw["Put Fair Value"]  = None

                def get_buildup(price, oi_chg, option_type="call"):
                    if oi_chg > 100 and price > 0:
                        return "🟢 Long Build Up" if option_type == "put" else "🔴 Short Build Up"
                    elif oi_chg < -100 and price > 0:
                        return "🟡 Short Cover" if option_type == "put" else "🟠 Long Unwind"
                    return "—"

                def get_writer_buyer(oi_chg, option_type, spot_chg):
                    """
                    Call OI ▲ + Price ▲ = Call Writers (Bears resistance bana rahe)
                    Call OI ▲ + Price ▼ = Call Buyers (Bulls hedge)
                    Put OI  ▲ + Price ▲ = Put Writers (Bulls support bana rahe) ← IMPORTANT
                    Put OI  ▲ + Price ▼ = Put Buyers (Bears hedge/momentum)
                    OI ▼ = Unwinding/Exit
                    """
                    if abs(oi_chg) < 100:
                        return "—"
                    if option_type == "put":
                        if oi_chg > 0:
                            if spot_chg > 0:   return "✍️ Put Writers"   # Bulls support bana rahe
                            elif spot_chg < 0: return "🛒 Put Buyers"    # Bears momentum
                            else:              return "📈 Put OI Add"
                        else:
                            if spot_chg > 0:   return "🏃 Put Exit"      # Put buyers exit
                            else:              return "⚠️ Put Unwind"    # Put writers exit
                    else:  # call
                        if oi_chg > 0:
                            if spot_chg < 0:   return "✍️ Call Writers"  # Bears resistance bana rahe
                            elif spot_chg > 0: return "🛒 Call Buyers"   # Bulls momentum
                            else:              return "📈 Call OI Add"
                        else:
                            if spot_chg < 0:   return "🏃 Call Exit"     # Call buyers exit
                            else:              return "⚠️ Call Unwind"   # Call writers exit

                # Spot change from prev session (approximate from day open)
                instr_key2  = "NSE_INDEX|Nifty 50" if name == "NIFTY" else ("NSE_INDEX|Nifty Bank" if name == "BANK NIFTY" else "BSE_INDEX|SENSEX")
                day_o, _, _, _ = extract_day_range(quote_data, instr_key2)
                spot_chg2 = (spot - day_o) if (spot and day_o) else 0

                oi_raw["Call Signal"]     = oi_raw.apply(lambda r: get_buildup(r["Call LTP"], r["Call OI Change"], "call"), axis=1)
                oi_raw["Put Signal"]      = oi_raw.apply(lambda r: get_buildup(r["Put LTP"],  r["Put OI Change"],  "put"),  axis=1)
                oi_raw["Call Who"]        = oi_raw["Call OI Change"].apply(lambda x: get_writer_buyer(x, "call", spot_chg2))
                oi_raw["Put Who"]         = oi_raw["Put OI Change"].apply(lambda x: get_writer_buyer(x, "put",  spot_chg2))

                oi_table = oi_raw[["Call Who","Call Signal","Call OI Change","Call OI","Strike","Put OI","Put OI Change","Put Signal","Put Who"]].copy()
                oi_table["Call OI"]        = oi_table["Call OI"].apply(lambda x: f"{int(x):,}")
                oi_table["Put OI"]         = oi_table["Put OI"].apply(lambda x: f"{int(x):,}")
                oi_table["Call OI Change"] = oi_table["Call OI Change"].apply(lambda x: f"+{int(x):,}" if x > 0 else f"{int(x):,}")
                oi_table["Put OI Change"]  = oi_table["Put OI Change"].apply(lambda x: f"+{int(x):,}" if x > 0 else f"{int(x):,}")
                oi_table.columns = ["📊 Call Who","Call Signal","Call OI Chg","Call OI","Strike","Put OI","Put OI Chg","Put Signal","📊 Put Who"]

                max_call_oi = oi_raw["Call OI"].max()
                max_put_oi  = oi_raw["Put OI"].max()

                def style_oi_row(row):
                    raw    = oi_raw.loc[row.name]
                    styles = [""] * len(row)
                    if raw["Strike"] == atm:
                        styles = ["background-color:#ffd60022;font-weight:bold"] * len(row)
                    # Call OI col = index 3, Put OI = index 5
                    if raw["Call OI"] == max_call_oi:
                        styles[3] = "background-color:#ff5252;color:white;font-weight:bold"
                    if raw["Put OI"] == max_put_oi:
                        styles[5] = "background-color:#00cc55;color:white;font-weight:bold"
                    return styles

                def color_chg(val):
                    if isinstance(val, str):
                        if val.startswith("+") and val != "+0": return "color:#00e676;font-weight:bold"
                        elif val.startswith("-"): return "color:#ff5252;font-weight:bold"
                        # Who column colors
                        elif "Writers" in val:  return "color:#a78bfa;font-weight:bold"
                        elif "Buyers"  in val:  return "color:#ff8c00;font-weight:bold"
                        elif "Exit"    in val:  return "color:#6495b8"
                        elif "Unwind"  in val:  return "color:#ff5252"
                    return ""

                st.dataframe(oi_table.style.apply(style_oi_row, axis=1)
                             .map(color_chg, subset=["Call OI Chg","Put OI Chg","📊 Call Who","📊 Put Who"]), use_container_width=True, hide_index=True)
                st.markdown("""<div style="display:flex;gap:16px;margin-top:6px;font-size:11px;flex-wrap:wrap">
                  <span style="color:#ff6666">🔴 Max Call OI = Resistance</span>
                  <span style="color:#00e676">🟢 Max Put OI = Support</span>
                  <span style="color:#ffd600">🟡 ATM Strike</span>
                  <span style="color:#a78bfa">✍️ Writers = Position add kar rahe</span>
                  <span style="color:#ff8c00">🛒 Buyers = Lete hain direction ke liye</span>
                </div>""", unsafe_allow_html=True)

                # ── 7 DAYS OI HISTORY ─────────────────────────
                st.markdown("---")
                st.markdown('<div class="sec-header" style="border-left:3px solid #60a5fa">📅 7 Days OI History</div>', unsafe_allow_html=True)

                hist_name    = "NIFTY" if name == "NIFTY" else ("BANKNIFTY" if name == "BANK NIFTY" else "SENSEX")
                history_data = load_oi_history(hist_name, days=7)

                if history_data:
                    # HTML table
                    rows_html = ""
                    for i, row in enumerate(history_data):
                        try:
                            d          = row.get("date", "")
                            h_spot     = float(row.get("spot", 0))
                            h_pcr      = float(row.get("pcr",  0))
                            h_mp       = row.get("max_pain", "—")
                            h_tc_oi    = int(float(row.get("total_call_oi", 0)))
                            h_tp_oi    = int(float(row.get("total_put_oi",  0)))
                            h_top_c    = int(float(row.get("top_call_strike", 0)))
                            h_top_p    = int(float(row.get("top_put_strike",  0)))
                            h_top_c_oi = int(float(row.get("top_call_oi", 0)))
                            h_top_p_oi = int(float(row.get("top_put_oi",  0)))

                            pcr_color  = "#00e676" if h_pcr >= 1.0 else ("#ff5252" if h_pcr <= 0.8 else "#ffd600")
                            pcr_label  = "Bullish" if h_pcr >= 1.0 else ("Bearish" if h_pcr <= 0.8 else "Neutral")
                            row_bg     = "#0d1929" if i % 2 == 0 else "#060e1a"
                            is_today   = d == str(date.today())
                            row_border = "border-left:3px solid #ffd600;" if is_today else ""
                            today_mark = " 🔵 Today" if is_today else ""

                            def fmt_oi_h(v):
                                if v >= 10000000: return f"{v/10000000:.1f}Cr"
                                elif v >= 100000: return f"{v/100000:.1f}L"
                                elif v >= 1000:   return f"{v/1000:.1f}K"
                                return str(v)

                            rows_html += f"""
                            <tr style="background:{row_bg};{row_border}">
                              <td style="padding:8px;color:#90b8d8;font-size:12px">{d}{today_mark}</td>
                              <td style="padding:8px;color:#00bfff;font-weight:bold;font-size:12px">{h_spot:,.0f}</td>
                              <td style="padding:8px;color:{pcr_color};font-weight:bold;font-size:12px">{h_pcr:.2f} <span style="font-size:10px">({pcr_label})</span></td>
                              <td style="padding:8px;color:#ff8c00;font-size:12px">{h_mp}</td>
                              <td style="padding:8px;font-size:11px">
                                <span style="color:#ff5252">{h_top_c:,}</span>
                                <span style="color:#6495b8;font-size:10px"> ({fmt_oi_h(h_top_c_oi)})</span>
                              </td>
                              <td style="padding:8px;font-size:11px">
                                <span style="color:#ff5252">{fmt_oi_h(h_tc_oi)}</span>
                              </td>
                              <td style="padding:8px;font-size:11px">
                                <span style="color:#00e676">{h_top_p:,}</span>
                                <span style="color:#6495b8;font-size:10px"> ({fmt_oi_h(h_top_p_oi)})</span>
                              </td>
                              <td style="padding:8px;font-size:11px">
                                <span style="color:#00e676">{fmt_oi_h(h_tp_oi)}</span>
                              </td>
                            </tr>"""
                        except Exception as e:
                            continue

                    header_tbl = """<div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse;font-size:12px"><thead><tr style="background:rgba(29,78,216,0.15);border-bottom:1px solid #30363d"><th style="padding:8px;text-align:left;color:#6495b8;font-size:11px">📅 Date</th><th style="padding:8px;text-align:left;color:#00bfff;font-size:11px">🔵 Spot</th><th style="padding:8px;text-align:left;color:#a78bfa;font-size:11px">📊 PCR</th><th style="padding:8px;text-align:left;color:#ff8c00;font-size:11px">⭐ Max Pain</th><th style="padding:8px;text-align:left;color:#ff5252;font-size:11px">🔴 Top Call Strike</th><th style="padding:8px;text-align:left;color:#ff5252;font-size:11px">📈 Total Call OI</th><th style="padding:8px;text-align:left;color:#00e676;font-size:11px">🟢 Top Put Strike</th><th style="padding:8px;text-align:left;color:#00e676;font-size:11px">📈 Total Put OI</th></tr></thead><tbody>"""
                    st.markdown(header_tbl + rows_html + "</tbody></table></div>", unsafe_allow_html=True)

                    st.markdown(f'<div style="font-size:11px;color:#6495b8;margin-top:6px">💾 Data save hota hai: <b style="color:#00bfff">{OI_HISTORY_DIR}</b> mein | Har din 3:15 PM ke baad automatically</div>', unsafe_allow_html=True)

                else:
                    st.markdown("""
                    <div style="background:#1d4ed812;border:1px solid #1d4ed8;border-radius:8px;padding:12px 16px;font-size:13px;color:#60a5fa">
                      📅 <b>Abhi koi history nahi</b> — Dashboard pehli baar 3:15 PM ke baad OI data save karega!<br>
                      <span style="font-size:11px;color:#6495b8">Har trading day ka closing OI automatically save hoga.</span>
                    </div>""", unsafe_allow_html=True)

                # ══════════════════════════════════════════════
                # 🎯 STRIKE PRICE SELECTOR — Detail View
                # ══════════════════════════════════════════════
                # ── DAY RANGE ─────────────────────────────────
                st.markdown("---")
                st.markdown('<div class="sec-header" style="border-left:3px solid #00bfff">📊 Day Range</div>', unsafe_allow_html=True)
                instr_key = "NSE_INDEX|Nifty 50" if name == "NIFTY" else ("NSE_INDEX|Nifty Bank" if name == "BANK NIFTY" else "BSE_INDEX|SENSEX")
                day_high, day_low, day_open, day_close = extract_day_range(quote_data, instr_key)
                if day_high and day_low and spot:
                    range_size = day_high - day_low
                    spot_pos   = round((spot - day_low) / range_size * 100, 1) if range_size > 0 else 50
                    st.markdown(f"""
                    <div style="background:#0d1929;border-radius:10px;padding:14px 18px;border:1px solid rgba(29,78,216,0.15)">
                      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
                        <div style="text-align:center">
                          <div style="font-size:10px;color:#6495b8;margin-bottom:3px">DAY LOW</div>
                          <div style="font-size:24px;font-weight:900;color:#00e676">{day_low:,.2f}</div>
                        </div>
                        <div style="text-align:center">
                          <div style="font-size:10px;color:#6495b8;margin-bottom:3px">SPOT</div>
                          <div style="font-size:22px;font-weight:bold;color:#00bfff">{spot:,.2f}</div>
                        </div>
                        <div style="text-align:center">
                          <div style="font-size:10px;color:#6495b8;margin-bottom:3px">RANGE</div>
                          <div style="font-size:18px;font-weight:bold;color:#ffd600">{range_size:,.2f} pts</div>
                        </div>
                        <div style="text-align:center">
                          <div style="font-size:10px;color:#6495b8;margin-bottom:3px">DAY HIGH</div>
                          <div style="font-size:24px;font-weight:900;color:#ff5252">{day_high:,.2f}</div>
                        </div>
                      </div>
                      <div style="background:#060e1a;border-radius:6px;height:12px;overflow:hidden;position:relative">
                        <div style="width:{spot_pos}%;background:linear-gradient(90deg,#00e676,#00bfff);height:100%;border-radius:6px;transition:width 0.5s"></div>
                      </div>
                      <div style="display:flex;justify-content:space-between;font-size:10px;color:#6495b8;margin-top:4px">
                        <span>Low</span><span style="color:#00bfff">Spot at {spot_pos}% of range</span><span>High</span>
                      </div>
                    </div>""", unsafe_allow_html=True)
                else:
                    st.info("⏳ Day range data market hours mein aayega")

st.markdown("---")
st.markdown('<div class="sec-header" style="border-left:3px solid #60a5fa">🏦 FII / DII Activity</div>', unsafe_allow_html=True)

fetch_time = now_ist().strftime("%d %b %Y, %I:%M:%S %p")
today_str  = now_ist().strftime("%d-%b-%Y")

col_fii1, col_fii2 = st.columns([8, 2])
with col_fii2:
    if st.button("🔄 FII/DII Refresh"): st.rerun()

with st.spinner("FII/DII data la raha hoon..."):
    fii_dii = safe_api_call(get_fii_dii_data, fallback=None)

if fii_dii:
    try:
        df_fii = pd.DataFrame(fii_dii)
        df_fii.columns = [c.lower().strip() for c in df_fii.columns]
        data_dates = df_fii["date"].astype(str).tolist() if "date" in df_fii.columns else []
        is_stale = all(today_str.lower() not in d.lower() for d in data_dates) if data_dates else True

        if is_stale:
            st.markdown(f'<div style="background:#ff990022;border:1.5px solid #ff9900;border-radius:8px;padding:10px 16px;margin-bottom:10px;font-size:13px;color:#ff9900">⚠️ <b>Purana data ({data_dates[0] if data_dates else ""})</b> — NSE aaj ka data ({today_str}) EOD (3:30 PM ke baad) update karta hai.</div>', unsafe_allow_html=True)
        else:
            st.markdown(f'<div style="font-size:12px;color:#00e676;margin-bottom:8px">✅ Aaj ka data — {fetch_time}</div>', unsafe_allow_html=True)

        for _, row in df_fii.iterrows():
            category = str(row.get("category","")).upper()
            date_val = row.get("date","")
            buy_val  = float(row.get("buyvalue",  0) or 0)
            sell_val = float(row.get("sellvalue", 0) or 0)
            net_val  = float(row.get("netvalue",  0) or 0)
            is_unavailable = str(row.get("_source","")) == "unavailable"

            # Agar data unavailable hai — placeholder card dikhao
            if is_unavailable:
                icon = "🏦" if "FII" in category else "🏢"
                st.markdown(f"""
                <div style="background:linear-gradient(135deg,#0f1e35 0%,#080e1e 100%);border-radius:14px;padding:18px 22px;margin:10px 0;border:1px solid rgba(255,153,0,0.2)">
                  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
                    <div style="font-size:17px;font-weight:800;color:#c8dff5">{icon} {category}</div>
                    <div style="font-size:10px;color:#ff9900;background:rgba(255,153,0,0.1);padding:3px 10px;border-radius:20px;border:1px solid rgba(255,153,0,0.3)">⏳ Data unavailable</div>
                  </div>
                  <div style="font-size:12px;color:#ff9900;background:#ff990015;border:1px solid #ff990030;border-radius:8px;padding:10px 14px">
                    NSE India ka server Streamlit Cloud pe data restrict karta hai.<br>
                    <span style="color:#6495b8;font-size:11px">Local machine pe run karo toh sahi data aayega — ya thodi der mein Refresh try karo.</span>
                  </div>
                </div>""", unsafe_allow_html=True)
                continue

            is_pos   = net_val >= 0
            net_color  = "#00e676" if is_pos else "#ff5252"
            net_bg     = "#00e67622" if is_pos else "#ff525222"
            net_border = "#00e676" if is_pos else "#ff5252"
            arrow      = "▲" if is_pos else "▼"
            icon       = "🏦" if "FII" in category else "🏢"
            action     = "BUY kar rahe hain 🟢" if is_pos else "SELL kar rahe hain 🔴"
            st.markdown(f"""
            <div style="background:linear-gradient(135deg,#0f1e35 0%,#080e1e 100%);border-radius:14px;padding:18px 22px;margin:10px 0;border:1px solid rgba(29,78,216,0.15);box-shadow:0 6px 24px rgba(0,0,0,0.4)">
              <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
                <div style="font-size:17px;font-weight:800;color:#c8dff5;letter-spacing:-0.3px">{icon} {category}</div>
                <div style="font-size:10px;color:#4e7a96;background:rgba(29,78,216,0.08);padding:3px 10px;border-radius:20px;border:1px solid rgba(29,78,216,0.2)">📅 {date_val}</div>
              </div>
              <div style="font-size:12px;color:{net_color};margin-bottom:12px;font-weight:700;background:{net_bg};border:1px solid {net_border}30;border-radius:8px;padding:6px 12px">→ {category} net {action} &nbsp;(₹{abs(net_val):,.2f} Cr)</div>
              <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px">
                <div style="background:rgba(0,230,118,0.05);border:1px solid rgba(0,230,118,0.15);border-radius:10px;padding:12px;text-align:center">
                  <div style="font-size:9px;color:#5a8aaa;letter-spacing:2px;font-weight:600;margin-bottom:4px">BUY</div>
                  <div style="font-family:'JetBrains Mono',monospace;font-size:21px;font-weight:900;color:#00e676">₹{buy_val:,.0f}Cr</div>
                </div>
                <div style="background:rgba(255,82,82,0.05);border:1px solid rgba(255,82,82,0.15);border-radius:10px;padding:12px;text-align:center">
                  <div style="font-size:9px;color:#5a8aaa;letter-spacing:2px;font-weight:600;margin-bottom:4px">SELL</div>
                  <div style="font-family:'JetBrains Mono',monospace;font-size:21px;font-weight:900;color:#ff5252">₹{sell_val:,.0f}Cr</div>
                </div>
                <div style="background:{net_bg};border:1.5px solid {net_border}50;border-radius:10px;padding:12px;text-align:center;box-shadow:0 0 16px {net_border}12">
                  <div style="font-size:9px;color:#5a8aaa;letter-spacing:2px;font-weight:600;margin-bottom:4px">NET</div>
                  <div style="font-family:'JetBrains Mono',monospace;font-size:23px;font-weight:900;color:{net_color}">{arrow} ₹{abs(net_val):,.0f}Cr</div>
                </div>
              </div>
            </div>""", unsafe_allow_html=True)
    except Exception as e:
        st.warning(f"FII/DII display error: {e}")
else:
    st.markdown(f"""
    <div style="background:#1a120022;border:1.5px solid #ff990050;border-radius:10px;padding:16px 20px;font-size:13px">
      <div style="color:#ff9900;font-weight:700;margin-bottom:6px">⚠️ FII/DII Data Abhi Available Nahi</div>
      <div style="color:#8ab8d8;font-size:12px;line-height:1.6">
        NSE India ka server Streamlit Cloud pe direct access block karta hai.<br>
        <b style="color:#ffd600">Solutions:</b><br>
        &nbsp; 1. <b style="color:#60a5fa">Local machine pe run karo</b> — wahan sahi kaam karega<br>
        &nbsp; 2. Thodi der baad <b>🔄 FII/DII Refresh</b> dabao<br>
        &nbsp; 3. NSE website pe manually check karo:
        <a href="https://www.nseindia.com/market-data/fii-dii-activity" target="_blank"
           style="color:#60a5fa">nseindia.com/market-data/fii-dii-activity</a>
      </div>
    </div>""", unsafe_allow_html=True)

st.markdown("---")
st.caption("⚠️ Sirf educational purpose. Trading apni responsibility par karein.")

# ══════════════════════════════════════════════════════════════
# 📓 TRADE JOURNAL
# ══════════════════════════════════════════════════════════════
TRADE_JOURNAL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trade_journal.csv")
TRADE_JOURNAL_FIELDS = ["id","date","symbol","expiry","strike","type","action","entry","exit","qty","pnl","pct","strategy","notes"]

def load_trades():
    if not os.path.exists(TRADE_JOURNAL_FILE):
        return []
    try:
        trades = []
        with open(TRADE_JOURNAL_FILE, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                trades.append(row)
        return trades
    except Exception as e:
        print(f"[WARN] Trade journal load failed: {e}")
        return []

def save_trades(trades):
    try:
        with open(TRADE_JOURNAL_FILE, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=TRADE_JOURNAL_FIELDS)
            writer.writeheader()
            writer.writerows(trades)
    except Exception as e:
        print(f"[WARN] Trade journal save failed: {e}")

def delete_trade_by_id(trade_id):
    trades = load_trades()
    trades = [t for t in trades if str(t["id"]) != str(trade_id)]
    save_trades(trades)

st.markdown("---")
st.markdown('<div class="sec-header" style="border-left:3px solid #a78bfa">📓 Trade Journal</div>', unsafe_allow_html=True)

# ── Stats ─────────────────────────────────────────────────────
all_trades = load_trades()
total_trades = len(all_trades)
total_pnl    = sum(float(t["pnl"]) for t in all_trades) if all_trades else 0
wins         = sum(1 for t in all_trades if float(t["pnl"]) >= 0)
win_rate     = round((wins / total_trades) * 100, 1) if total_trades > 0 else 0
best_trade   = max((float(t["pnl"]) for t in all_trades), default=0)

col_tj1, col_tj2, col_tj3, col_tj4 = st.columns(4)
with col_tj1:
    st.markdown(f"""<div class="metric-card" style="text-align:center">
        <div style="font-size:10px;color:#6495b8;letter-spacing:2px;text-transform:uppercase;margin-bottom:4px">Total Trades</div>
        <div style="font-size:28px;font-weight:900;color:#60a5fa;font-family:'JetBrains Mono',monospace">{total_trades}</div>
    </div>""", unsafe_allow_html=True)
with col_tj2:
    pnl_color = "#00e676" if total_pnl >= 0 else "#ff5252"
    pnl_sign  = "+" if total_pnl >= 0 else ""
    st.markdown(f"""<div class="metric-card" style="text-align:center">
        <div style="font-size:10px;color:#6495b8;letter-spacing:2px;text-transform:uppercase;margin-bottom:4px">Total P&amp;L</div>
        <div style="font-size:28px;font-weight:900;color:{pnl_color};font-family:'JetBrains Mono',monospace">{pnl_sign}₹{total_pnl:,.0f}</div>
    </div>""", unsafe_allow_html=True)
with col_tj3:
    wr_color = "#00e676" if win_rate >= 50 else "#ff5252"
    st.markdown(f"""<div class="metric-card" style="text-align:center">
        <div style="font-size:10px;color:#6495b8;letter-spacing:2px;text-transform:uppercase;margin-bottom:4px">Win Rate</div>
        <div style="font-size:28px;font-weight:900;color:{wr_color};font-family:'JetBrains Mono',monospace">{win_rate}%</div>
    </div>""", unsafe_allow_html=True)
with col_tj4:
    best_color = "#00e676" if best_trade >= 0 else "#ff5252"
    st.markdown(f"""<div class="metric-card" style="text-align:center">
        <div style="font-size:10px;color:#6495b8;letter-spacing:2px;text-transform:uppercase;margin-bottom:4px">Best Trade</div>
        <div style="font-size:28px;font-weight:900;color:{best_color};font-family:'JetBrains Mono',monospace">₹{best_trade:,.0f}</div>
    </div>""", unsafe_allow_html=True)

st.markdown("<br>", unsafe_allow_html=True)

# ── Add New Trade Form ─────────────────────────────────────────
st.markdown("""
<div style="background:linear-gradient(90deg,#1d4ed820 0%,#1d4ed805 100%);
    border:1.5px solid #1d4ed880;border-radius:10px;
    padding:10px 18px;margin-bottom:6px;
    display:flex;align-items:center;gap:10px">
  <span style="font-size:18px">➕</span>
  <span style="color:#60a5fa;font-size:14px;font-weight:700;letter-spacing:0.5px">Naya Trade Add Karo</span>
  <span style="color:#4e7a96;font-size:11px;margin-left:auto">Neeche arrow dabao ▼</span>
</div>""", unsafe_allow_html=True)

with st.expander("➕ Trade Form Kholein", expanded=False):
    with st.form("trade_journal_form", clear_on_submit=True):
        fcol1, fcol2, fcol3 = st.columns(3)
        with fcol1:
            tj_date     = st.date_input("Date", value=date.today(), key="tj_date")
            tj_symbol   = st.selectbox("Index / Stock", ["NIFTY","BANKNIFTY","FINNIFTY","SENSEX","Other"], key="tj_sym")
            tj_expiry   = st.text_input("Expiry (e.g. 17 Apr 2025)", key="tj_exp")
        with fcol2:
            tj_strike   = st.number_input("Strike", min_value=0, step=50, key="tj_strike")
            tj_type     = st.selectbox("CE / PE", ["CE","PE"], key="tj_type")
            tj_action   = st.selectbox("BUY / SELL", ["BUY","SELL"], key="tj_action")
        with fcol3:
            tj_entry    = st.number_input("Entry Price", min_value=0.0, step=0.05, format="%.2f", key="tj_entry")
            tj_exit     = st.number_input("Exit Price", min_value=0.0, step=0.05, format="%.2f", key="tj_exit")
            tj_qty      = st.number_input("Lot Size / Qty", min_value=1, step=1, key="tj_qty")

        fcol4, fcol5 = st.columns(2)
        with fcol4:
            tj_strategy = st.selectbox("Strategy", ["Intraday","Scalping","Swing","Straddle","Strangle","Bull Call Spread","Bear Put Spread","Other"], key="tj_strat")
        with fcol5:
            tj_notes    = st.text_area("Notes / Reason", placeholder="Calls SASTA tha, trend bullish tha...", height=80, key="tj_notes")

        # Live P&L preview
        if tj_entry > 0 and tj_exit > 0 and tj_qty > 0:
            diff_preview  = (tj_exit - tj_entry) if tj_action == "BUY" else (tj_entry - tj_exit)
            pnl_preview   = round(diff_preview * tj_qty, 2)
            pct_preview   = round((diff_preview / tj_entry) * 100, 2) if tj_entry > 0 else 0
            cap_preview   = round(tj_entry * tj_qty, 2)
            pnl_col_prev  = "#00e676" if pnl_preview >= 0 else "#ff5252"
            pnl_sign_prev = "+" if pnl_preview >= 0 else ""
            st.markdown(f"""
            <div style="background:#060e1a;border:1px solid #1d4ed840;border-radius:8px;padding:10px 16px;display:flex;gap:32px;margin-top:6px">
                <div><div style="font-size:10px;color:#6495b8">Estimated P&amp;L</div>
                     <div style="font-size:18px;font-weight:900;color:{pnl_col_prev};font-family:'JetBrains Mono',monospace">{pnl_sign_prev}₹{pnl_preview:,.0f}</div></div>
                <div><div style="font-size:10px;color:#6495b8">Return %</div>
                     <div style="font-size:18px;font-weight:900;color:{pnl_col_prev};font-family:'JetBrains Mono',monospace">{pnl_sign_prev}{pct_preview:.1f}%</div></div>
                <div><div style="font-size:10px;color:#6495b8">Capital Used</div>
                     <div style="font-size:18px;font-weight:900;color:#90b8d8;font-family:'JetBrains Mono',monospace">₹{cap_preview:,.0f}</div></div>
            </div>""", unsafe_allow_html=True)

        submitted = st.form_submit_button("✅ Trade Save Karo", use_container_width=True)
        if submitted:
            if tj_entry <= 0 or tj_exit <= 0 or tj_qty <= 0 or tj_strike <= 0:
                st.error("❌ Strike, Entry, Exit aur Qty zaroor bharo!")
            else:
                diff    = (tj_exit - tj_entry) if tj_action == "BUY" else (tj_entry - tj_exit)
                pnl_val = round(diff * tj_qty, 2)
                pct_val = round((diff / tj_entry) * 100, 2) if tj_entry > 0 else 0
                new_trade = {
                    "id":       int(time.time() * 1000),
                    "date":     str(tj_date),
                    "symbol":   tj_symbol,
                    "expiry":   tj_expiry,
                    "strike":   tj_strike,
                    "type":     tj_type,
                    "action":   tj_action,
                    "entry":    tj_entry,
                    "exit":     tj_exit,
                    "qty":      tj_qty,
                    "pnl":      pnl_val,
                    "pct":      pct_val,
                    "strategy": tj_strategy,
                    "notes":    tj_notes,
                }
                existing = load_trades()
                existing.insert(0, new_trade)
                save_trades(existing)
                st.success(f"✅ Trade saved! P&L: {'+'if pnl_val>=0 else ''}₹{pnl_val:,.0f}")
                st.rerun()

# ── Trade History Table ────────────────────────────────────────
st.markdown('<div style="font-size:11px;color:#6495b8;letter-spacing:2px;text-transform:uppercase;margin:14px 0 8px">📋 Trade History</div>', unsafe_allow_html=True)

# Filter buttons
tj_filter_col1, tj_filter_col2, tj_filter_col3, tj_filter_col4, tj_filter_col5 = st.columns([1,1,1,1,4])
with tj_filter_col1:
    show_all    = st.button("All", key="tjf_all",    use_container_width=True)
with tj_filter_col2:
    show_profit = st.button("✅ Profit", key="tjf_profit", use_container_width=True)
with tj_filter_col3:
    show_loss   = st.button("❌ Loss",   key="tjf_loss",   use_container_width=True)
with tj_filter_col4:
    show_today  = st.button("📅 Aaj",   key="tjf_today",  use_container_width=True)

# Filter state
if "tj_filter" not in st.session_state:
    st.session_state.tj_filter = "all"
if show_all:    st.session_state.tj_filter = "all"
if show_profit: st.session_state.tj_filter = "profit"
if show_loss:   st.session_state.tj_filter = "loss"
if show_today:  st.session_state.tj_filter = "today"

# Apply filter
display_trades = load_trades()
if st.session_state.tj_filter == "profit":
    display_trades = [t for t in display_trades if float(t["pnl"]) >= 0]
elif st.session_state.tj_filter == "loss":
    display_trades = [t for t in display_trades if float(t["pnl"]) < 0]
elif st.session_state.tj_filter == "today":
    today_str_tj = str(date.today())
    display_trades = [t for t in display_trades if t["date"] == today_str_tj]

if not display_trades:
    st.markdown('<div style="background:#0f1e3580;border:1px solid #1d4ed830;border-radius:8px;padding:20px;text-align:center;color:#6495b8;font-size:13px">Koi trade nahi mila — upar form se add karo</div>', unsafe_allow_html=True)
else:
    # Build HTML table
    rows_html = ""
    for t in display_trades:
        pnl_f    = float(t["pnl"])
        pct_f    = float(t["pct"])
        pnl_col  = "#00e676" if pnl_f >= 0 else "#ff5252"
        pnl_sign = "+" if pnl_f >= 0 else ""
        act_col  = "#00e676" if t["action"] == "BUY" else "#ff5252"
        type_col = "#60a5fa" if t["type"] == "CE" else "#c084fc"
        rows_html += f"""
        <tr style="border-bottom:1px solid rgba(29,78,216,0.08)">
          <td style="padding:7px 10px;color:#8ab8d8;font-size:11px">{t['date']}</td>
          <td style="padding:7px 10px;color:#60a5fa;font-weight:700;font-size:12px">{t['symbol']}</td>
          <td style="padding:7px 10px;color:#e8f4ff;font-size:12px">{t['strike']}<br><span style="font-size:9px;color:#4e7a96">{t.get('expiry','')}</span></td>
          <td style="padding:7px 10px"><span style="color:{type_col};background:{type_col}20;border:1px solid {type_col}50;border-radius:4px;padding:2px 7px;font-size:10px;font-weight:700">{t['type']}</span></td>
          <td style="padding:7px 10px"><span style="color:{act_col};background:{act_col}20;border:1px solid {act_col}50;border-radius:4px;padding:2px 7px;font-size:10px;font-weight:700">{t['action']}</span></td>
          <td style="padding:7px 10px;color:#90b8d8;font-family:'JetBrains Mono',monospace;font-size:12px">₹{float(t['entry']):.1f}</td>
          <td style="padding:7px 10px;color:#90b8d8;font-family:'JetBrains Mono',monospace;font-size:12px">₹{float(t['exit']):.1f}</td>
          <td style="padding:7px 10px;color:#8ab8d8;font-size:12px">{t['qty']}</td>
          <td style="padding:7px 10px;color:{pnl_col};font-weight:900;font-family:'JetBrains Mono',monospace;font-size:13px">{pnl_sign}₹{pnl_f:,.0f}</td>
          <td style="padding:7px 10px;color:{pnl_col};font-weight:700;font-size:12px">{pnl_sign}{pct_f:.1f}%</td>
          <td style="padding:7px 10px;color:#a78bfa;font-size:10px">{t.get('strategy','')}</td>
          <td style="padding:7px 10px;color:#6495b8;font-size:10px;max-width:120px">{t.get('notes','—')[:40]}</td>
        </tr>"""

    st.markdown(f"""
    <div style="overflow-x:auto;border-radius:10px;border:1px solid rgba(29,78,216,0.15)">
    <table style="width:100%;border-collapse:collapse;background:#0a1220;font-family:'Inter',sans-serif">
      <thead>
        <tr style="background:rgba(29,78,216,0.12);border-bottom:1px solid rgba(29,78,216,0.25)">
          <th style="padding:8px 10px;text-align:left;color:#4e7a96;font-size:10px;text-transform:uppercase;letter-spacing:1px">Date</th>
          <th style="padding:8px 10px;text-align:left;color:#4e7a96;font-size:10px;text-transform:uppercase;letter-spacing:1px">Symbol</th>
          <th style="padding:8px 10px;text-align:left;color:#4e7a96;font-size:10px;text-transform:uppercase;letter-spacing:1px">Strike</th>
          <th style="padding:8px 10px;text-align:left;color:#4e7a96;font-size:10px;text-transform:uppercase;letter-spacing:1px">Type</th>
          <th style="padding:8px 10px;text-align:left;color:#4e7a96;font-size:10px;text-transform:uppercase;letter-spacing:1px">Action</th>
          <th style="padding:8px 10px;text-align:left;color:#4e7a96;font-size:10px;text-transform:uppercase;letter-spacing:1px">Entry</th>
          <th style="padding:8px 10px;text-align:left;color:#4e7a96;font-size:10px;text-transform:uppercase;letter-spacing:1px">Exit</th>
          <th style="padding:8px 10px;text-align:left;color:#4e7a96;font-size:10px;text-transform:uppercase;letter-spacing:1px">Qty</th>
          <th style="padding:8px 10px;text-align:left;color:#4e7a96;font-size:10px;text-transform:uppercase;letter-spacing:1px">P&amp;L</th>
          <th style="padding:8px 10px;text-align:left;color:#4e7a96;font-size:10px;text-transform:uppercase;letter-spacing:1px">Return</th>
          <th style="padding:8px 10px;text-align:left;color:#4e7a96;font-size:10px;text-transform:uppercase;letter-spacing:1px">Strategy</th>
          <th style="padding:8px 10px;text-align:left;color:#4e7a96;font-size:10px;text-transform:uppercase;letter-spacing:1px">Notes</th>
        </tr>
      </thead>
      <tbody>{rows_html}</tbody>
    </table>
    </div>""", unsafe_allow_html=True)

    # Delete trade option
    st.markdown("<br>", unsafe_allow_html=True)
    with st.expander("🗑️ Trade Delete Karo"):
        trade_options = {f"{t['date']} | {t['symbol']} {t['strike']} {t['type']} | P&L: ₹{float(t['pnl']):,.0f}": t["id"] for t in display_trades}
        selected_trade = st.selectbox("Trade select karo:", list(trade_options.keys()), key="tj_del_select")
        if st.button("🗑️ Delete Selected Trade", key="tj_del_btn"):
            delete_trade_by_id(trade_options[selected_trade])
            st.success("✅ Trade delete ho gaya!")
            st.rerun()

    # Export CSV button
    if all_trades:
        import io
        csv_buf = io.StringIO()
        writer  = csv.DictWriter(csv_buf, fieldnames=TRADE_JOURNAL_FIELDS)
        writer.writeheader()
        writer.writerows(all_trades)
        st.download_button(
            label="📥 Export Trade Journal (CSV)",
            data=csv_buf.getvalue(),
            file_name=f"trade_journal_{date.today()}.csv",
            mime="text/csv",
            key="tj_export"
        )

# ══════════════════════════════════════════════════════════════
# 📡 OI WALL TICKER — Bottom sticky scrolling bar
# ══════════════════════════════════════════════════════════════
ticker_data = st.session_state.get("oi_wall_ticker", [])
if ticker_data:
    # Build ticker text items
    ticker_items = []
    for t in ticker_data:
        name_t  = t["name"]
        res     = f"{t['resistance']:,}" if t["resistance"] else "—"
        res_oi  = f"{t['res_oi']//1000}K" if t["res_oi"] >= 1000 else str(t["res_oi"])
        sup     = f"{t['support']:,}" if t["support"] else "—"
        sup_oi  = f"{t['sup_oi']//1000}K" if t["sup_oi"] >= 1000 else str(t["sup_oi"])
        spot_t  = f"{int(t['spot']):,}" if t["spot"] else "—"
        upd     = t.get("updated", "")

        ticker_items.append(
            f'<span style="color:#90b8d8;margin:0 8px">|</span>'
            f'<span style="color:#ffd600;font-weight:bold">{name_t}</span>'
            f'<span style="color:#90b8d8;font-size:11px;margin-left:6px">Spot:{spot_t}</span>'
            f'<span style="color:#90b8d8;margin:0 6px">▸</span>'
            f'<span style="color:#ff5252">🧱 Resistance: <b>{res}</b></span>'
            f'<span style="color:#ff525280;font-size:11px;margin-left:3px">({res_oi} OI)</span>'
            f'<span style="color:#90b8d8;margin:0 10px">⟷</span>'
            f'<span style="color:#00e676">🧱 Support: <b>{sup}</b></span>'
            f'<span style="color:#00e67680;font-size:11px;margin-left:3px">({sup_oi} OI)</span>'
            f'<span style="color:#6495b8;font-size:10px;margin-left:8px">🕐{upd}</span>'
        )

    ticker_html = "".join(ticker_items) * 3  # 3x repeat for smooth loop

    mins_left = max(0, 180 - int(time.time() - st.session_state.get("oi_wall_last_update", 0)))
    next_upd  = f"{mins_left//60}:{mins_left%60:02d}" if mins_left > 0 else "updating..."

    st.markdown(f"""
    <div style="
        position:fixed; bottom:0; left:0; right:0; z-index:9999;
        background:linear-gradient(90deg,#020810 0%,#06101c 50%,#020810 100%);
        border-top:1px solid rgba(29,78,216,0.4);
        padding:0;
        height:38px;
        overflow:hidden;
        box-shadow:0 -4px 24px rgba(0,0,0,0.7),0 -1px 0 rgba(29,78,216,0.2);
    ">
      <!-- Next update countdown -->
      <div style="
          position:absolute; right:12px; top:50%; transform:translateY(-50%);
          font-size:9px; color:#1d4ed8; z-index:10000; background:#020810;
          padding:3px 10px; border-left:1px solid rgba(29,78,216,0.2);
          font-family:'JetBrains Mono',monospace; letter-spacing:1px;
      ">🔄 {next_upd}</div>

      <!-- Scrolling ticker -->
      <div style="
          display:flex; align-items:center;
          height:38px;
          white-space:nowrap;
          animation: ticker_scroll 40s linear infinite;
          font-family:'JetBrains Mono',monospace;
          font-size:12px;
          padding-right:120px;
      ">
        <span style="color:#1d4ed8;font-weight:700;margin-right:14px;font-size:10px;letter-spacing:2px;text-transform:uppercase">
          📡 OI WALL
        </span>
        {ticker_html}
      </div>
    </div>

    <style>
    @keyframes ticker_scroll {{
        0%   {{ transform: translateX(100vw); }}
        100% {{ transform: translateX(-100%); }}
    }}
    .block-container {{ padding-bottom: 55px !important; }}
    </style>
    """, unsafe_allow_html=True)
else:
    st.markdown("""
    <div style="
        position:fixed; bottom:0; left:0; right:0; z-index:9999;
        background:#0a0e1a; border-top:2px solid rgba(29,78,216,0.15);
        height:36px; display:flex; align-items:center; padding:0 16px;
        font-size:12px; color:#6495b8;
    ">
      📡 OI Wall Ticker — Option chain load hone ke baad yahan dikhega...
    </div>
    <style>.block-container {{ padding-bottom: 50px !important; }}</style>
    """, unsafe_allow_html=True)

if auto_refresh:
    time.sleep(3)
    st.rerun()