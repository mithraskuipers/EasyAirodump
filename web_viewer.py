#!/usr/bin/env python3
"""
airodump-ng Live Viewer
- Discovers wireless interfaces
- Big touch-friendly buttons to select one
- Starts airodump-ng in background
- Real-time dashboard sorted by strongest signal
- Accessible on the local network
"""

import csv
import json
import os
import signal
import subprocess
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# ── Config ────────────────────────────────────────────────────────────────
PORT = 8080
HOST = "0.0.0.0"
CSV_PREFIX = "scan"
REFRESH_MS = 250
SCRIPT_DIR = Path(__file__).resolve().parent

# Global state
airodump_proc = None
selected_iface = None

# ── Helpers ───────────────────────────────────────────────────────────────
def get_default_route_iface():
    """Interface used for the default IPv4 route (internet)."""
    try:
        out = subprocess.check_output(
            ["ip", "-4", "route", "show", "default"],
            text=True, stderr=subprocess.DEVNULL,
        )
        for line in out.splitlines():
            parts = line.split()
            if "dev" in parts:
                return parts[parts.index("dev") + 1]
    except Exception:
        pass
    return None


def ifaces_with_ipv4():
    """Set of interfaces that currently have a global IPv4 address."""
    result = set()
    try:
        out = subprocess.check_output(
            ["ip", "-4", "-o", "addr", "show", "scope", "global"],
            text=True, stderr=subprocess.DEVNULL,
        )
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                result.add(parts[1])
    except Exception:
        pass
    return result


def get_monitor_ifaces():
    """Interfaces already in monitor mode (from iw)."""
    monitors = set()
    try:
        out = subprocess.check_output(
            ["iw", "dev"], text=True, stderr=subprocess.DEVNULL
        )
        current = None
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("Interface "):
                current = line.split()[1]
            elif "type monitor" in line and current:
                monitors.add(current)
                current = None
    except Exception:
        pass
    return monitors


def get_wlan_interfaces():
    """
    Return wireless interfaces that are safe to use with airodump-ng.
    Skips the default-route interface and any interface that has an IPv4
    address (i.e. currently used for connectivity), unless it is already
    in monitor mode.
    """
    ifaces = []
    seen = set()
    net = Path("/sys/class/net")
    if net.exists():
        for iface in sorted(net.iterdir()):
            name = iface.name
            if name == "lo":
                continue
            if (iface / "wireless").is_dir() or (iface / "phy80211").is_dir():
                ifaces.append(name)
                seen.add(name)
    try:
        out = subprocess.check_output(
            ["ip", "-o", "link", "show"], text=True, stderr=subprocess.DEVNULL
        )
        for line in out.splitlines():
            parts = line.split(": ")
            if len(parts) >= 2:
                name = parts[1].split("@")[0]
                if name not in seen and any(
                    name.startswith(p) for p in ("wlan", "wlp", "wlx", "rt", "mon")
                ):
                    ifaces.append(name)
                    seen.add(name)
    except Exception:
        pass

    default_iface = get_default_route_iface()
    with_ip = ifaces_with_ipv4()
    monitors = get_monitor_ifaces()

    safe = []
    skipped = []
    for name in ifaces:
        # Always allow true monitor-mode interfaces
        if name in monitors:
            safe.append(name)
            continue
        # Skip internet / connected interfaces
        if name == default_iface or name in with_ip:
            skipped.append(name)
            continue
        safe.append(name)

    # If filtering removed everything, fall back to full list
    # (user can still type a name manually)
    if not safe and ifaces:
        return ifaces
    return safe


def find_latest_csv():
    files = sorted(
        SCRIPT_DIR.glob(f"{CSV_PREFIX}-*.csv"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return files[0] if files else None


def parse_airodump_csv(path):
    aps, stations = [], []
    if not path or not path.exists():
        return aps, stations
    try:
        text = path.read_text(encoding="utf-8", errors="replace").replace("\0", "")
    except Exception:
        return aps, stations

    section = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("BSSID, First time seen"):
            section = "ap"
            continue
        if line.startswith("Station MAC, First time seen"):
            section = "sta"
            continue
        if section is None:
            continue
        try:
            parts = [p.strip() for p in next(csv.reader([line]))]
        except Exception:
            continue

        try:
            if section == "ap" and len(parts) >= 14:
                try:
                    pwr = int(parts[8])
                except ValueError:
                    pwr = -999
                aps.append({
                    "bssid": parts[0],
                    "first": parts[1],
                    "last": parts[2],
                    "channel": parts[3],
                    "speed": parts[4],
                    "privacy": parts[5],
                    "cipher": parts[6],
                    "auth": parts[7],
                    "power": pwr,
                    "beacons": parts[9],
                    "iv": parts[10],
                    "essid": parts[13] if len(parts) > 13 else "",
                })
            elif section == "sta" and len(parts) >= 6:
                try:
                    pwr = int(parts[3])
                except ValueError:
                    pwr = -999
                stations.append({
                    "mac": parts[0],
                    "first": parts[1],
                    "last": parts[2],
                    "power": pwr,
                    "packets": parts[4],
                    "bssid": parts[5],
                    "probes": parts[6] if len(parts) > 6 else "",
                })
        except Exception:
            continue

    aps.sort(key=lambda x: x["power"], reverse=True)
    stations.sort(key=lambda x: x["power"], reverse=True)
    return aps, stations


def start_airodump(iface):
    global airodump_proc, selected_iface
    stop_airodump()

    # Clean old files
    for f in SCRIPT_DIR.glob(f"{CSV_PREFIX}-*"):
        try:
            f.unlink()
        except Exception:
            pass

    cmd = [
        "airodump-ng",
        "--output-format", "csv",
        "--write-interval", "1",
        "-w", str(SCRIPT_DIR / CSV_PREFIX),
        iface,
    ]
    airodump_proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        preexec_fn=os.setsid if hasattr(os, "setsid") else None,
    )
    selected_iface = iface
    time.sleep(1.5)
    if airodump_proc.poll() is not None:
        airodump_proc = None
        selected_iface = None
        return False
    return True


def stop_airodump():
    global airodump_proc, selected_iface
    if airodump_proc and airodump_proc.poll() is None:
        try:
            os.killpg(os.getpgid(airodump_proc.pid), signal.SIGTERM)
        except Exception:
            try:
                airodump_proc.terminate()
            except Exception:
                pass
        try:
            airodump_proc.wait(timeout=3)
        except Exception:
            try:
                airodump_proc.kill()
            except Exception:
                pass
    airodump_proc = None
    selected_iface = None


def is_scanning():
    return airodump_proc is not None and airodump_proc.poll() is None


# ── HTML ──────────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>airodump Live</title>
<style>
  :root {
    --bg: #0d1117;
    --card: #161b22;
    --border: #30363d;
    --text: #e6edf3;
    --muted: #8b949e;
    --green: #3fb950;
    --yellow: #d29922;
    --red: #f85149;
    --blue: #58a6ff;
    --accent: #1f6feb;
    --btn: #238636;
    --btn-hover: #2ea043;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    padding: 1rem;
    -webkit-tap-highlight-color: transparent;
  }

  /* ── Select screen ── */
  #select-screen { max-width: 480px; margin: 0 auto; padding-top: 2rem; }
  #select-screen h1 {
    font-size: 1.6rem;
    text-align: center;
    margin-bottom: 0.4rem;
  }
  #select-screen .sub {
    text-align: center;
    color: var(--muted);
    margin-bottom: 2rem;
    font-size: 0.95rem;
  }
  .iface-btn {
    display: block;
    width: 100%;
    padding: 1.4rem 1.2rem;
    margin-bottom: 1rem;
    font-size: 1.35rem;
    font-weight: 600;
    border: 2px solid var(--border);
    border-radius: 14px;
    background: var(--card);
    color: var(--text);
    cursor: pointer;
    text-align: center;
    transition: all 0.15s ease;
    -webkit-appearance: none;
  }
  .iface-btn:active, .iface-btn:hover {
    background: var(--accent);
    border-color: var(--accent);
    transform: scale(0.98);
  }
  .iface-btn.recommended {
    border-color: var(--green);
    box-shadow: 0 0 0 1px var(--green);
  }
  .iface-btn .tag {
    display: block;
    font-size: 0.75rem;
    font-weight: 400;
    color: var(--green);
    margin-top: 0.3rem;
  }
  .manual-row {
    display: flex;
    gap: 0.6rem;
    margin-top: 1.5rem;
  }
  .manual-row input {
    flex: 1;
    padding: 1rem;
    font-size: 1.1rem;
    border-radius: 12px;
    border: 2px solid var(--border);
    background: var(--card);
    color: var(--text);
    outline: none;
  }
  .manual-row input:focus { border-color: var(--accent); }
  .manual-row button {
    padding: 1rem 1.2rem;
    font-size: 1.1rem;
    font-weight: 600;
    border: none;
    border-radius: 12px;
    background: var(--btn);
    color: #fff;
    cursor: pointer;
  }
  .manual-row button:active { background: var(--btn-hover); }
  .status-msg {
    text-align: center;
    margin-top: 1.5rem;
    color: var(--muted);
    font-size: 0.9rem;
    min-height: 1.4em;
  }
  .status-msg.error { color: var(--red); }
  .status-msg.ok { color: var(--green); }

  /* ── Dashboard ── */
  #dash-screen { display: none; }
  header {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.6rem;
    margin-bottom: 1rem;
    padding-bottom: 0.8rem;
    border-bottom: 1px solid var(--border);
  }
  header h1 { font-size: 1.2rem; font-weight: 600; flex: 1; }
  .badge {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 0.35rem 0.7rem;
    font-size: 0.8rem;
    color: var(--muted);
  }
  .badge strong { color: var(--green); }
  .dot {
    width: 9px; height: 9px; border-radius: 50%;
    display: inline-block; margin-right: 5px;
    background: var(--green);
    box-shadow: 0 0 6px var(--green);
  }
  .dot.off { background: var(--red); box-shadow: 0 0 6px var(--red); }
  .btn-stop {
    padding: 0.55rem 1rem;
    font-size: 0.9rem;
    font-weight: 600;
    border: none;
    border-radius: 10px;
    background: var(--red);
    color: #fff;
    cursor: pointer;
  }
  .btn-stop:active { opacity: 0.85; }

  .topn-bar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.6rem;
    margin-bottom: 0.8rem;
    padding: 0.7rem 0.9rem;
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 10px;
  }
  .topn-bar label {
    font-size: 0.85rem;
    color: var(--muted);
    white-space: nowrap;
  }
  .topn-bar input[type=range] {
    flex: 1;
    min-width: 120px;
    max-width: 220px;
    height: 28px;
    cursor: pointer;
    accent-color: var(--accent);
  }
  .topn-bar .topn-val {
    font-weight: 700;
    font-size: 1.1rem;
    color: var(--green);
    min-width: 1.6em;
    text-align: center;
  }
  .topn-bar .topn-all {
    padding: 0.4rem 0.75rem;
    font-size: 0.8rem;
    font-weight: 600;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: transparent;
    color: var(--text);
    cursor: pointer;
  }
  .topn-bar .topn-all.active {
    background: var(--accent);
    border-color: var(--accent);
  }
  .filter-toggles {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    margin-bottom: 0.8rem;
  }
  .toggle-btn {
    padding: 0.45rem 0.85rem;
    font-size: 0.82rem;
    font-weight: 600;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: var(--card);
    color: var(--muted);
    cursor: pointer;
    -webkit-appearance: none;
  }
  .toggle-btn.on {
    background: #21262d;
    border-color: var(--green);
    color: var(--green);
  }
  .toggle-btn:active { opacity: 0.85; }

  h2 {
    margin: 1.2rem 0 0.5rem;
    font-size: 0.95rem;
    color: var(--muted);
    font-weight: 500;
  }
  .table-wrap {
    overflow-x: auto;
    border-radius: 10px;
    border: 1px solid var(--border);
    -webkit-overflow-scrolling: touch;
  }
  table {
    width: 100%;
    border-collapse: collapse;
    background: var(--card);
    font-size: 0.82rem;
    min-width: 600px;
  }
  th, td {
    padding: 0.55rem 0.65rem;
    text-align: left;
    border-bottom: 1px solid var(--border);
    white-space: nowrap;
  }
  th {
    background: #21262d;
    color: var(--muted);
    font-weight: 500;
    position: sticky;
    top: 0;
  }
  tr:hover td { background: #1c2128; }
  .pwr-bar {
    display: inline-block;
    height: 7px;
    border-radius: 3px;
    vertical-align: middle;
    margin-right: 5px;
  }
  .essid { color: var(--blue); max-width: 140px; overflow: hidden; text-overflow: ellipsis; }
  .bssid { color: var(--muted); font-size: 0.78rem; }
  .enc-open { color: var(--red); }
  .enc-wpa { color: var(--green); }
  .enc-wep { color: var(--yellow); }
  .empty { color: var(--muted); padding: 1.5rem; text-align: center; }

  tr.ap-row { cursor: pointer; }
  tr.ap-row:hover { background: rgba(255,255,255,0.05); }
  tr.ap-row:active { background: rgba(255,255,255,0.09); }

  #channel-canvas { cursor: pointer; }

  #mac-toast {
    position: fixed;
    bottom: 24px;
    left: 50%;
    transform: translateX(-50%) translateY(12px);
    background: #1f2733;
    color: #e6edf3;
    border: 1px solid var(--border);
    padding: 0.55rem 1rem;
    border-radius: 8px;
    font-size: 0.85rem;
    box-shadow: 0 6px 18px rgba(0,0,0,0.35);
    opacity: 0;
    pointer-events: none;
    transition: opacity 0.18s ease, transform 0.18s ease;
    z-index: 999;
  }
  #mac-toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }

  /* ── View / band toggle ── */
  .view-bar {
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    align-items: center;
    gap: 0.6rem;
    margin-bottom: 0.4rem;
  }
  .view-toggle, .band-toggle { display: flex; gap: 0.5rem; }
  #band-toggle.hidden { display: none; }

  /* ── Channel graph ── */
  .graph-wrap {
    display: none;
    border: 1px solid var(--border);
    border-radius: 10px;
    background: var(--card);
    padding: 0.8rem;
  }
  .graph-wrap.show { display: block; }
  #channel-canvas {
    width: 100%;
    height: 320px;
    display: block;
  }
  .graph-legend {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem 1rem;
    margin-top: 0.7rem;
    padding-top: 0.6rem;
    border-top: 1px solid var(--border);
  }
  .legend-item {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    font-size: 0.8rem;
    color: var(--text);
  }
  .legend-swatch {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    flex-shrink: 0;
  }
</style>
</head>
<body>

<!-- ════════ SELECT SCREEN ════════ -->
<div id="select-screen">
  <h1>📡 airodump Live</h1>
  <p class="sub">Select a wireless interface to start scanning</p>
  <div id="iface-list"></div>
  <div class="manual-row">
    <input id="manual-iface" type="text" placeholder="or type interface name" autocomplete="off" autocapitalize="off">
    <button onclick="startManual()">Go</button>
  </div>
  <p id="status-msg" class="status-msg"></p>
</div>

<!-- ════════ DASHBOARD ════════ -->
<div id="dash-screen">
  <header>
    <h1>📡 Live Scan</h1>
    <span class="badge"><span id="dot" class="dot"></span><span id="status-text">Live</span></span>
    <span class="badge">APs: <strong id="ap-count">0</strong></span>
    <span class="badge">Clients: <strong id="sta-count">0</strong></span>
    <span class="badge" id="iface-badge">—</span>
    <span class="badge">Refresh: <strong id="refresh-badge">—</strong></span>
    <button class="btn-stop" onclick="stopScan()">Stop</button>
  </header>

  <div class="topn-bar">
    <label>Show top</label>
    <span class="topn-val" id="topn-val">10</span>
    <label>strongest</label>
    <input type="range" id="topn-slider" min="1" max="10" value="10" oninput="onTopNChange()">
    <button class="topn-all active" id="topn-all-btn" onclick="showAll()">All</button>
  </div>

  <div class="filter-toggles">
    <button class="toggle-btn on" id="btn-hide-hidden" onclick="toggleHideHidden()">Hide empty ESSID</button>
    <button class="toggle-btn on" id="btn-hide-pwr1" onclick="toggleHidePwr1()">Hide PWR -1</button>
  </div>

  <div class="view-bar">
    <div class="view-toggle">
      <button class="toggle-btn on" id="btn-view-table" onclick="setView('table')">Table</button>
      <button class="toggle-btn" id="btn-view-graph" onclick="setView('graph')">Channel Graph</button>
    </div>
    <div class="band-toggle" id="band-toggle">
      <button class="toggle-btn on" id="btn-band-24" onclick="setBand('2.4')">2.4 GHz</button>
      <button class="toggle-btn" id="btn-band-5" onclick="setBand('5')">5 GHz</button>
    </div>
  </div>

  <h2>Access Points · strongest first</h2>
  <div class="table-wrap" id="ap-table-wrap">
    <table>
      <thead>
        <tr>
          <th>PWR</th>
          <th>BSSID</th>
          <th>ESSID</th>
          <th>CH</th>
          <th>ENC</th>
          <th>CIPHER</th>
          <th>AUTH</th>
          <th>Beacons</th>
          <th>Data</th>
          <th>Last seen</th>
        </tr>
      </thead>
      <tbody id="ap-body">
        <tr><td colspan="10" class="empty">Waiting for data…</td></tr>
      </tbody>
    </table>
  </div>

  <div class="graph-wrap" id="ap-graph-wrap">
    <canvas id="channel-canvas"></canvas>
    <div id="graph-legend" class="graph-legend"></div>
  </div>

  <h2>Clients / Stations</h2>
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>PWR</th>
          <th>Station MAC</th>
          <th>Associated BSSID</th>
          <th>Packets</th>
          <th>Probes</th>
          <th>Last seen</th>
        </tr>
      </thead>
      <tbody id="sta-body">
        <tr><td colspan="6" class="empty">No clients yet</td></tr>
      </tbody>
    </table>
  </div>
</div>

<script>
const REFRESH = """ + str(REFRESH_MS) + r""";
let polling = false;
let topN = 10;          // 1-10, or 0 = show all
let lastData = null;    // cache last payload for instant filter
let hideHiddenEssid = true;   // hide empty ESSID by default
let hidePwrMinus1 = true;     // hide PWR -1 by default
let viewMode = 'table';       // 'table' or 'graph'
let bandMode = '2.4';         // '2.4' or '5'
let graphHitAreas = [];       // clickable regions on the channel graph canvas

function essidHue(essid, bssid) {
  const key = (essid && essid.trim()) ? essid : bssid;
  let hash = 0;
  for (let i = 0; i < key.length; i++) {
    hash = (hash * 31 + key.charCodeAt(i)) & 0xffffffff;
  }
  return Math.abs(hash) % 360;
}

function setView(mode) {
  viewMode = mode;
  document.getElementById('btn-view-table').classList.toggle('on', mode === 'table');
  document.getElementById('btn-view-graph').classList.toggle('on', mode === 'graph');
  document.getElementById('ap-table-wrap').style.display = (mode === 'table') ? 'block' : 'none';
  document.getElementById('ap-graph-wrap').classList.toggle('show', mode === 'graph');
  document.getElementById('band-toggle').classList.toggle('hidden', mode !== 'graph');
  if (lastData) render(lastData);
}

function setBand(band) {
  bandMode = band;
  document.getElementById('btn-band-24').classList.toggle('on', band === '2.4');
  document.getElementById('btn-band-5').classList.toggle('on', band === '5');
  if (lastData) render(lastData);
}

function onTopNChange() {
  const slider = document.getElementById('topn-slider');
  topN = parseInt(slider.value, 10);
  document.getElementById('topn-val').textContent = topN;
  document.getElementById('topn-all-btn').classList.remove('active');
  if (lastData) render(lastData);
}

function showAll() {
  topN = 0;
  document.getElementById('topn-val').textContent = '∞';
  document.getElementById('topn-all-btn').classList.add('active');
  if (lastData) render(lastData);
}

function toggleHideHidden() {
  hideHiddenEssid = !hideHiddenEssid;
  const btn = document.getElementById('btn-hide-hidden');
  btn.classList.toggle('on', hideHiddenEssid);
  btn.textContent = hideHiddenEssid ? 'Hide empty ESSID' : 'Show empty ESSID';
  if (lastData) render(lastData);
}

function toggleHidePwr1() {
  hidePwrMinus1 = !hidePwrMinus1;
  const btn = document.getElementById('btn-hide-pwr1');
  btn.classList.toggle('on', hidePwrMinus1);
  btn.textContent = hidePwrMinus1 ? 'Hide PWR -1' : 'Show PWR -1';
  if (lastData) render(lastData);
}

function setStatus(msg, cls) {
  const el = document.getElementById('status-msg');
  el.textContent = msg;
  el.className = 'status-msg' + (cls ? ' ' + cls : '');
}

async function loadInterfaces() {
  try {
    const r = await fetch('/api/interfaces');
    const data = await r.json();
    const list = document.getElementById('iface-list');
    if (!data.interfaces || data.interfaces.length === 0) {
      list.innerHTML = '<p class="status-msg">No free wireless interfaces found (connected ones are hidden to protect your internet). Type a name below if needed.</p>';
      return;
    }
    list.innerHTML = data.interfaces.map(name => {
      return `<button class="iface-btn" onclick="startScan('${name}')">${name}</button>`;
    }).join('');
  } catch (e) {
    setStatus('Failed to load interfaces', 'error');
  }
}

async function startScan(iface) {
  setStatus('Starting airodump-ng on ' + iface + '…');
  try {
    const r = await fetch('/api/start?iface=' + encodeURIComponent(iface), { method: 'POST' });
    const data = await r.json();
    if (data.ok) {
      document.getElementById('select-screen').style.display = 'none';
      document.getElementById('dash-screen').style.display = 'block';
      document.getElementById('iface-badge').textContent = iface;
      polling = true;
      poll();
    } else {
      setStatus(data.error || 'Failed to start', 'error');
    }
  } catch (e) {
    setStatus('Request failed: ' + e.message, 'error');
  }
}

function startManual() {
  const val = document.getElementById('manual-iface').value.trim();
  if (!val) {
    setStatus('Enter an interface name', 'error');
    return;
  }
  startScan(val);
}

async function stopScan() {
  polling = false;
  try {
    await fetch('/api/stop', { method: 'POST' });
  } catch (e) {}
  document.getElementById('dash-screen').style.display = 'none';
  document.getElementById('select-screen').style.display = 'block';
  setStatus('Stopped. Select an interface to scan again.');
  loadInterfaces();
}

function drawChannelGraph(apsInput) {
  graphHitAreas = [];
  const canvas = document.getElementById('channel-canvas');
  const wrap = canvas.parentElement;
  const dpr = window.devicePixelRatio || 1;
  const cssWidth = Math.max(280, wrap.clientWidth);
  const cssHeight = 320;
  canvas.width = cssWidth * dpr;
  canvas.height = cssHeight * dpr;
  canvas.style.width = cssWidth + 'px';
  canvas.style.height = cssHeight + 'px';
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssWidth, cssHeight);

  const channels = (bandMode === '2.4')
    ? Array.from({length: 14}, (_, i) => i + 1)
    : [36, 40, 44, 48, 52, 56, 60, 64, 100, 104, 108, 112, 116, 120, 124, 128, 132, 136, 140, 144, 149, 153, 157, 161, 165];

  const aps = (apsInput || []).filter(ap => {
    const ch = parseInt(ap.channel, 10);
    if (isNaN(ch)) return false;
    return (bandMode === '2.4') ? (ch >= 1 && ch <= 14) : (ch > 14);
  });

  const padL = 42, padR = 14, padT = 22, padB = 26;
  const plotW = cssWidth - padL - padR;
  const plotH = cssHeight - padT - padB;
  const yMin = -100, yMax = -20;

  function chanX(ch) {
    let idx = channels.indexOf(ch);
    if (idx < 0) idx = 0;
    return padL + (idx + 0.5) / channels.length * plotW;
  }
  function pwrY(pwr) {
    const clamped = Math.max(yMin, Math.min(yMax, pwr));
    return padT + (1 - (clamped - yMin) / (yMax - yMin)) * plotH;
  }

  ctx.strokeStyle = 'rgba(255,255,255,0.08)';
  ctx.fillStyle = '#8b949e';
  ctx.font = '10px sans-serif';
  ctx.lineWidth = 1;
  ctx.textAlign = 'left';
  for (let db = yMin; db <= yMax; db += 20) {
    const y = pwrY(db);
    ctx.beginPath();
    ctx.moveTo(padL, y);
    ctx.lineTo(padL + plotW, y);
    ctx.stroke();
    ctx.fillText(String(db), 2, y + 3);
  }

  ctx.textAlign = 'center';
  channels.forEach(ch => {
    ctx.fillText(String(ch), chanX(ch), cssHeight - padB + 15);
  });

  const legend = document.getElementById('graph-legend');

  if (aps.length === 0) {
    ctx.fillStyle = '#8b949e';
    ctx.fillText('No networks in this band', cssWidth / 2, cssHeight / 2);
    legend.innerHTML = '';
    return;
  }

  const sigmaChannels = (bandMode === '2.4') ? 2.2 : 1.6;
  const sigmaPx = (sigmaChannels / channels.length) * plotW;
  const legendItems = [];

  aps.forEach(ap => {
    const ch = parseInt(ap.channel, 10);
    const cx = chanX(ch);
    const baseY = padT + plotH;
    const peakY = pwrY(ap.power);
    const peakHeight = baseY - peakY;
    if (peakHeight <= 0) return;
    const hue = essidHue(ap.essid, ap.bssid);
    const stroke = `hsl(${hue}, 70%, 58%)`;
    const fill = `hsla(${hue}, 70%, 58%, 0.25)`;

    ctx.beginPath();
    ctx.moveTo(padL, baseY);
    const steps = 60;
    for (let i = 0; i <= steps; i++) {
      const x = padL + (i / steps) * plotW;
      const g = Math.exp(-Math.pow(x - cx, 2) / (2 * sigmaPx * sigmaPx));
      ctx.lineTo(x, baseY - peakHeight * g);
    }
    ctx.lineTo(padL + plotW, baseY);
    ctx.closePath();
    ctx.fillStyle = fill;
    ctx.fill();
    ctx.strokeStyle = stroke;
    ctx.lineWidth = 1.6;
    ctx.stroke();

    ctx.fillStyle = stroke;
    ctx.font = '11px sans-serif';
    const label = (ap.essid && ap.essid.trim()) ? ap.essid : ap.bssid;
    const labelY = Math.max(peakY - 6, padT + 10);
    ctx.fillText(label, cx, labelY);

    const textWidth = ctx.measureText(label).width;
    graphHitAreas.push({
      x1: cx - textWidth / 2 - 6, x2: cx + textWidth / 2 + 6,
      y1: labelY - 13, y2: labelY + 5,
      bssid: ap.bssid, essid: label
    });

    legendItems.push({ label, color: stroke, power: ap.power, channel: ch });
  });

  legendItems.sort((a, b) => b.power - a.power);
  legend.innerHTML = legendItems.map(it =>
    `<span class="legend-item"><span class="legend-swatch" style="background:${it.color}"></span>${it.label} · ch${it.channel} · ${it.power} dBm</span>`
  ).join('');
}

function pwrColor(p) {
  if (p >= -50) return '#3fb950';
  if (p >= -65) return '#d29922';
  return '#f85149';
}
function pwrBar(p) {
  const pct = Math.max(5, Math.min(100, (p + 100) * 1.4));
  return `<span class="pwr-bar" style="width:${pct}px;background:${pwrColor(p)}"></span>${p}`;
}
function encClass(priv) {
  const p = (priv || '').toUpperCase();
  if (p.includes('OPN') || p === '') return 'enc-open';
  if (p.includes('WEP')) return 'enc-wep';
  return 'enc-wpa';
}

function render(data) {
  lastData = data;
  let allAps = data.aps || [];

  // Apply hide filters first (before top-N)
  if (hideHiddenEssid) {
    allAps = allAps.filter(ap => ap.essid && ap.essid.trim() !== '');
  }
  if (hidePwrMinus1) {
    allAps = allAps.filter(ap => ap.power !== -1);
  }

  const totalAfterFilter = allAps.length;
  const shownAps = (topN > 0) ? allAps.slice(0, topN) : allAps;

  document.getElementById('ap-count').textContent = shownAps.length + (shownAps.length < totalAfterFilter ? '/' + totalAfterFilter : '');
  document.getElementById('sta-count').textContent = data.stations.length;
  const dot = document.getElementById('dot');
  const st = document.getElementById('status-text');
  if (data.scanning) {
    st.textContent = 'Live';
    dot.classList.remove('off');
  } else {
    st.textContent = 'Stopped';
    dot.classList.add('off');
  }

  const apBody = document.getElementById('ap-body');
  if (shownAps.length === 0) {
    apBody.innerHTML = '<tr><td colspan="10" class="empty">Waiting for networks…</td></tr>';
  } else {
    apBody.innerHTML = shownAps.map(ap => `
      <tr class="ap-row" data-bssid="${ap.bssid}">
        <td>${pwrBar(ap.power)}</td>
        <td class="bssid">${ap.bssid}</td>
        <td class="essid" title="${ap.essid}">${ap.essid || '<i style="color:#8b949e">hidden</i>'}</td>
        <td>${ap.channel}</td>
        <td class="${encClass(ap.privacy)}">${ap.privacy || '—'}</td>
        <td>${ap.cipher || '—'}</td>
        <td>${ap.auth || '—'}</td>
        <td>${ap.beacons}</td>
        <td>${ap.iv}</td>
        <td style="color:var(--muted)">${ap.last}</td>
      </tr>`).join('');
  }

  if (viewMode === 'graph') {
    drawChannelGraph(shownAps);
  }

  const staBody = document.getElementById('sta-body');
  if (data.stations.length === 0) {
    staBody.innerHTML = '<tr><td colspan="6" class="empty">No clients yet</td></tr>';
  } else {
    staBody.innerHTML = data.stations.map(s => `
      <tr>
        <td>${pwrBar(s.power)}</td>
        <td class="bssid">${s.mac}</td>
        <td class="bssid">${s.bssid}</td>
        <td>${s.packets}</td>
        <td style="max-width:160px;overflow:hidden;text-overflow:ellipsis">${s.probes || '—'}</td>
        <td style="color:var(--muted)">${s.last}</td>
      </tr>`).join('');
  }
}

async function poll() {
  if (!polling) return;
  try {
    const r = await fetch('/api/data?' + Date.now());
    if (r.ok) render(await r.json());
  } catch (e) {}
  if (polling) setTimeout(poll, REFRESH);
}

function showToast(msg) {
  let toast = document.getElementById('mac-toast');
  if (!toast) {
    toast = document.createElement('div');
    toast.id = 'mac-toast';
    document.body.appendChild(toast);
  }
  toast.textContent = msg;
  toast.classList.add('show');
  clearTimeout(toast._hideTimer);
  toast._hideTimer = setTimeout(() => toast.classList.remove('show'), 1800);
}

function fallbackCopy(text) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.left = '-9999px';
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  try { document.execCommand('copy'); } catch (e) {}
  document.body.removeChild(ta);
}

function copyMac(mac) {
  if (!mac) return;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(mac)
      .then(() => showToast('MAC address copied: ' + mac))
      .catch(() => { fallbackCopy(mac); showToast('MAC address copied: ' + mac); });
  } else {
    fallbackCopy(mac);
    showToast('MAC address copied: ' + mac);
  }
}

document.getElementById('ap-body').addEventListener('click', (e) => {
  const row = e.target.closest('tr.ap-row');
  if (row && row.dataset.bssid) copyMac(row.dataset.bssid);
});

document.getElementById('channel-canvas').addEventListener('click', (e) => {
  const canvas = e.currentTarget;
  const rect = canvas.getBoundingClientRect();
  const x = e.clientX - rect.left;
  const y = e.clientY - rect.top;
  const hit = graphHitAreas.find(a => x >= a.x1 && x <= a.x2 && y >= a.y1 && y <= a.y2);
  if (hit) copyMac(hit.bssid);
});

// Init
document.getElementById('refresh-badge').textContent = REFRESH + 'ms';
window.addEventListener('resize', () => {
  if (viewMode === 'graph' && lastData) render(lastData);
});
loadInterfaces();
// If already scanning (page refresh), go to dash
fetch('/api/status').then(r => r.json()).then(d => {
  if (d.scanning) {
    document.getElementById('select-screen').style.display = 'none';
    document.getElementById('dash-screen').style.display = 'block';
    document.getElementById('iface-badge').textContent = d.iface || '—';
    polling = true;
    poll();
  }
}).catch(() => {});
</script>
</body>
</html>
"""


# ── HTTP Handler ──────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[{time.strftime('%H:%M:%S')}] {args[0]}")

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path

        if path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(HTML.encode("utf-8"))
            return

        if path == "/api/interfaces":
            self._json({"interfaces": get_wlan_interfaces()})
            return

        if path == "/api/status":
            self._json({
                "scanning": is_scanning(),
                "iface": selected_iface,
            })
            return

        if path == "/api/data":
            csv_path = find_latest_csv()
            aps, stations = parse_airodump_csv(csv_path)
            self._json({
                "aps": aps,
                "stations": stations,
                "scanning": is_scanning(),
                "iface": selected_iface,
                "updated": time.strftime("%H:%M:%S"),
            })
            return

        self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/api/start":
            iface = (qs.get("iface") or [""])[0].strip()
            if not iface:
                self._json({"ok": False, "error": "No interface given"}, 400)
                return
            # Basic safety: only allow reasonable interface names
            if not all(c.isalnum() or c in "_-" for c in iface):
                self._json({"ok": False, "error": "Invalid interface name"}, 400)
                return
            ok = start_airodump(iface)
            if ok:
                self._json({"ok": True, "iface": iface})
            else:
                self._json({
                    "ok": False,
                    "error": f"airodump-ng failed on '{iface}'. Is it in monitor mode? Try: sudo airmon-ng start {iface}",
                })
            return

        if path == "/api/stop":
            stop_airodump()
            self._json({"ok": True})
            return

        self.send_error(404)


def main():
    if os.geteuid() != 0:
        print("[!] Warning: not running as root. airodump-ng will likely fail.")
        print("    Run with:  sudo python3 web_viewer.py")
        print()

    # List every interface with an IPv4 address, so the right one
    # (e.g. the hotspot dongle) is always visible, even with no internet.
    ips = []
    try:
        out = subprocess.check_output(
            ["ip", "-4", "-o", "addr", "show"], text=True, stderr=subprocess.DEVNULL
        )
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[1] != "lo":
                iface = parts[1]
                addr = parts[3].split("/")[0]
                ips.append((iface, addr))
    except Exception:
        pass

    print("=" * 50)
    print("  airodump-ng  ·  Live Browser Viewer")
    print("=" * 50)
    print(f"  Local:    http://127.0.0.1:{PORT}")
    if ips:
        for iface, addr in ips:
            print(f"  Network:  http://{addr}:{PORT}   ({iface})")
    else:
        print("  Network:  no interface has an IP yet")
        print("            set up a role first: sudo ./cfg_wifi_interface_roles.sh")
    print("=" * 50)
    print("  Open the URL on phone or computer,")
    print("  tap an interface button to start scanning.")
    print("  Press Ctrl+C to quit.")
    print()

    server = HTTPServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] Stopping…")
        stop_airodump()
        server.server_close()
        print("[*] Done.")


if __name__ == "__main__":
    main()
