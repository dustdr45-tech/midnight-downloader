#!/usr/bin/env python3
"""System tray icon for the Midnight Downloader: status, run now, open dashboard/folder. Optional."""
import os
import subprocess
import webbrowser
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

SCRIPT_DIR = Path(__file__).resolve().parent
START_SCRIPT = SCRIPT_DIR / "midnight-downloader.sh"
ENV_FILE = SCRIPT_DIR / ".env"
DASHBOARD_URL = f"http://127.0.0.1:{os.getenv('DASHBOARD_PORT', '8765')}"


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


def make_icon_image() -> Image.Image:
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([2, 2, size - 2, size - 2], fill=(52, 120, 246, 255))
    draw.polygon([(18, 32), (48, 18), (34, 46), (30, 36)], fill=(255, 255, 255, 255))
    return img


def get_next_run_text() -> str:
    try:
        out = subprocess.run(
            ["systemctl", "list-timers", "midnight-downloader.timer", "--no-pager"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        lines = [l for l in out.strip().splitlines() if l.strip()]
        if len(lines) >= 2:
            parts = lines[1].split()
            if len(parts) >= 2:
                return f"Next run: {parts[0]} {parts[1]}"
    except (subprocess.SubprocessError, OSError, FileNotFoundError):
        pass
    return "Next run: unknown"


def run_now(icon, item):
    subprocess.Popen(
        [str(START_SCRIPT), "run"],
        cwd=str(SCRIPT_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    icon.notify("Started a download run in the background.", "Midnight Downloader")


def open_dashboard(icon, item):
    webbrowser.open(DASHBOARD_URL)


def open_downloads(icon, item):
    subprocess.Popen(["xdg-open", str(get_download_dir())])


def quit_app(icon, item):
    icon.stop()


def build_menu_items():
    # Callable, not a static Menu, so get_next_run_text() re-runs each open.
    yield pystray.MenuItem(get_next_run_text(), None, enabled=False)
    yield pystray.Menu.SEPARATOR
    yield pystray.MenuItem("Run now", run_now)
    yield pystray.MenuItem("Open dashboard", open_dashboard)
    yield pystray.MenuItem("Open downloads folder", open_downloads)
    yield pystray.Menu.SEPARATOR
    yield pystray.MenuItem("Quit", quit_app)


def main():
    icon = pystray.Icon(
        "midnight-downloader",
        make_icon_image(),
        "Midnight Downloader",
        menu=pystray.Menu(build_menu_items),
    )
    icon.run()


if __name__ == "__main__":
    main()

