#!/usr/bin/env python3
"""Local web dashboard for the Midnight Downloader (stats, calendar, log, run-now button). Optional."""
import os
import re
import json
import calendar
import subprocess
from pathlib import Path
from datetime import date

from flask import Flask, jsonify, render_template_string

SCRIPT_DIR = Path(__file__).resolve().parent
ENV_FILE = SCRIPT_DIR / ".env"
CRON_LOG = SCRIPT_DIR / "cron.log"
START_SCRIPT = SCRIPT_DIR / "midnight-downloader.sh"


def read_env_var(key: str, default: str = "") -> str:
    if not ENV_FILE.exists():
        return default
    for line in ENV_FILE.read_text().splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip() or default
    return default


def get_download_dir() -> Path:
    raw = read_env_var("DOWNLOAD_DIR", "~/Downloads/Telegram")
    return Path(os.path.expanduser(raw))


def get_manifest_stats() -> dict:
    manifest_path = get_download_dir() / ".downloaded_manifest.json"
    if not manifest_path.exists():
        return {"total_files": 0, "total_size": 0}
    try:
        manifest = json.loads(manifest_path.read_text())
    except (json.JSONDecodeError, OSError):
        return {"total_files": 0, "total_size": 0}
    total_size = sum(entry.get("size", 0) for entry in manifest.values())
    return {"total_files": len(manifest), "total_size": total_size}


def get_run_history() -> dict:
    """Parse cron.log into a {YYYY-MM-DD: 'success'|'failed'} map."""
    history = {}
    if not CRON_LOG.exists():
        return history
    text = CRON_LOG.read_text(errors="ignore")
    for match in re.finditer(r"(\d{4}-\d{2}-\d{2}).*?Run finished with exit code (\d+)", text):
        day, code = match.group(1), int(match.group(2))
        history[day] = "success" if code == 0 else "failed"
    return history


def get_next_run() -> str | None:
    try:
        out = subprocess.run(
            ["systemctl", "list-timers", "midnight-downloader.timer", "--no-pager"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        lines = [l for l in out.strip().splitlines() if l.strip()]
        if len(lines) >= 2:
            parts = lines[1].split()
            if len(parts) >= 2:
                return f"{parts[0]} {parts[1]}"
    except (subprocess.SubprocessError, OSError, FileNotFoundError):
        pass
    return None


app = Flask(__name__)

PAGE = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Midnight Downloader</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root { color-scheme: dark; }
  body { font-family: -apple-system, "Segoe UI", Roboto, sans-serif; background:#12131a; color:#e6e6ef; margin:0; padding:2rem; max-width:640px; }
  h1 { font-size: 1.4rem; margin-bottom: 0.25rem; }
  .sub { color:#8b8fa3; margin-bottom:1.5rem; }
  .grid { display:grid; grid-template-columns: repeat(3, 1fr); gap:1rem; margin-bottom:1.5rem; }
  .card { background:#1b1d29; border-radius:12px; padding:1rem 1.1rem; }
  .card .label { color:#8b8fa3; font-size:0.75rem; text-transform:uppercase; letter-spacing:0.05em; }
  .card .value { font-size:1.4rem; font-weight:600; margin-top:0.25rem; }
  button { background:#5865f2; color:white; border:none; padding:0.6rem 1.2rem; border-radius:8px; font-size:0.95rem; cursor:pointer; }
  button:hover { background:#4752c4; }
  button:disabled { background:#3a3d4d; cursor:not-allowed; }
  table.cal { border-collapse: collapse; width:100%; max-width:380px; }
  table.cal th { color:#8b8fa3; font-weight:500; font-size:0.8rem; padding:0.4rem; }
  table.cal td { text-align:center; padding:0.5rem; border-radius:8px; font-size:0.9rem; }
  .day-success { background:#1f4d33; color:#7ee2a8; }
  .day-failed { background:#4d1f24; color:#ff8f96; }
  .day-empty { color:#4a4d5e; }
  .day-today { outline: 2px solid #5865f2; outline-offset: -2px; }
  pre#log { background:#1b1d29; padding:1rem; border-radius:12px; max-height:260px; overflow:auto; font-size:0.78rem; white-space:pre-wrap; }
  #status-msg { margin-top:0.6rem; color:#8b8fa3; font-size:0.85rem; min-height:1.2em; }
  h3 { margin-top:2rem; margin-bottom:0.6rem; font-size:1rem; color:#c7c9d9; }
</style>
</head>
<body>
  <h1>Midnight Downloader</h1>
  <div class="sub">Telegram Saved Messages, backed up automatically.</div>

  <div class="grid">
    <div class="card"><div class="label">Files downloaded</div><div class="value" id="total-files">-</div></div>
    <div class="card"><div class="label">Total size</div><div class="value" id="total-size">-</div></div>
    <div class="card"><div class="label">Next run</div><div class="value" id="next-run" style="font-size:1rem;">-</div></div>
  </div>

  <button id="run-now">Run now</button>
  <div id="status-msg"></div>

  <h3>This month</h3>
  <table class="cal" id="calendar"></table>

  <h3>Recent log</h3>
  <pre id="log">Loading...</pre>

<script>
function humanSize(bytes) {
  if (!bytes) return "0 B";
  const units = ["B","KB","MB","GB","TB"];
  let i = 0;
  while (bytes >= 1024 && i < units.length - 1) { bytes /= 1024; i++; }
  return bytes.toFixed(1) + " " + units[i];
}

async function refresh() {
  const res = await fetch("/api/status");
  const data = await res.json();

  document.getElementById("total-files").textContent = data.total_files;
  document.getElementById("total-size").textContent = humanSize(data.total_size);
  document.getElementById("next-run").textContent = data.next_run || "unknown";

  const cal = document.getElementById("calendar");
  cal.innerHTML = "";
  const headRow = document.createElement("tr");
  ["S","M","T","W","T","F","S"].forEach(d => {
    const th = document.createElement("th"); th.textContent = d; headRow.appendChild(th);
  });
  cal.appendChild(headRow);

  let row = document.createElement("tr");
  for (let i = 0; i < data.first_weekday; i++) row.appendChild(document.createElement("td"));
  for (let day = 1; day <= data.days_in_month; day++) {
    if (row.children.length === 7) { cal.appendChild(row); row = document.createElement("tr"); }
    const td = document.createElement("td");
    td.textContent = day;
    const key = data.month_prefix + String(day).padStart(2, "0");
    if (data.history[key] === "success") td.className = "day-success";
    else if (data.history[key] === "failed") td.className = "day-failed";
    else td.className = "day-empty";
    if (key === data.today) td.className += " day-today";
    row.appendChild(td);
  }
  cal.appendChild(row);

  document.getElementById("log").textContent = data.log_tail || "(no log yet)";
}

document.getElementById("run-now").addEventListener("click", async () => {
  const btn = document.getElementById("run-now");
  btn.disabled = true;
  document.getElementById("status-msg").textContent = "Starting a download run in the background...";
  await fetch("/api/run-now", { method: "POST" });
  setTimeout(() => {
    btn.disabled = false;
    document.getElementById("status-msg").textContent = "Started - watch the log below.";
  }, 1500);
});

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(PAGE)


@app.route("/api/status")
def status():
    stats = get_manifest_stats()
    history = get_run_history()
    today = date.today()
    _, days_in_month = calendar.monthrange(today.year, today.month)
    first_of_month = date(today.year, today.month, 1)
    # weekday() is Monday=0; convert to Sunday-first for the page's calendar.
    sunday_first_weekday = (first_of_month.weekday() + 1) % 7

    log_tail = ""
    if CRON_LOG.exists():
        lines = CRON_LOG.read_text(errors="ignore").splitlines()
        log_tail = "\n".join(lines[-40:])

    return jsonify({
        "total_files": stats["total_files"],
        "total_size": stats["total_size"],
        "next_run": get_next_run(),
        "history": history,
        "first_weekday": sunday_first_weekday,
        "days_in_month": days_in_month,
        "month_prefix": f"{today.year:04d}-{today.month:02d}-",
        "today": today.isoformat(),
        "log_tail": log_tail,
    })


@app.route("/api/run-now", methods=["POST"])
def run_now():
    subprocess.Popen(
        [str(START_SCRIPT), "run"],
        cwd=str(SCRIPT_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return jsonify({"started": True})


if __name__ == "__main__":
    port = int(os.getenv("DASHBOARD_PORT", "8765"))
    print(f"Dashboard running at http://127.0.0.1:{port}  (Ctrl+C to stop)")
    app.run(host="127.0.0.1", port=port, debug=False)
