# Midnight Downloader

Downloads every file from your Telegram **Saved Messages** on a
schedule, using your personal account (via Telethon) rather than a
bot — no 50MB size limit. Wakes the machine from suspend if needed,
downloads, and can go back to sleep afterward.

## Files

| File | Purpose |
|---|---|
| `midnight-downloader.sh` | Setup + the command the scheduler calls nightly |
| `downloader.py` | Download logic |
| `dashboard.py` | Optional local web dashboard |
| `tray.py` | Optional system tray icon |
| `.env.example` | Config template — copy to `.env` |
| `requirements.txt` | Core Python dependencies |
| `requirements-gui.txt` | Extra dependencies for dashboard/tray |

## 1. Get Telegram API credentials

1. Go to <https://my.telegram.org>, log in.
2. **API Development Tools** → create an app (any name/URL works).
3. Copy the **api_id** and **api_hash** shown.

## 2. Install system packages

Requires Python 3.10+ and `systemd` (for scheduling).

| Distro | Command |
|---|---|
| Arch | `sudo pacman -S python` |
| Debian / Ubuntu | `sudo apt install python3 python3-venv python3-pip` |
| Fedora | `sudo dnf install python3 python3-pip` |
| openSUSE | `sudo zypper install python3 python3-pip python3-virtualenv` |

Optional, for GUI add-ons (notifications need `notify-send`, the tray
icon's "open folder" needs `xdg-open` — both are already installed on
most desktop setups):

| Distro | Package |
|---|---|
| Arch | `libnotify`, `xdg-utils` |
| Debian / Ubuntu | `libnotify-bin`, `xdg-utils` |
| Fedora / openSUSE | `libnotify`, `xdg-utils` |

GNOME removed native tray icon support — if you're on GNOME, install
the **AppIndicator and KStatusNotifierItem Support** extension from
extensions.gnome.org for `tray.py` to show up. KDE, XFCE, and most
other desktops work without it.

## 3. Run setup

```bash
cd ~/telegram-downloader
chmod +x midnight-downloader.sh
./midnight-downloader.sh setup
```

Does everything in order: creates a venv and installs dependencies,
creates `.env` from the template (stops here the first time so you
can fill in `API_ID` / `API_HASH` / `PHONE_NUMBER`, then re-run
`setup`), logs into Telegram interactively if there's no saved
session yet, and installs the nightly schedule (asks for `sudo` —
needed to set a hardware wake alarm and install a system-level
timer; the download itself still runs as your normal user).

## 4. What happens every night

Nothing needs to be left open beforehand — the scheduled run is a
separate process started by `systemd`, unrelated to your terminal.

At the time set in `.env` (`DOWNLOAD_HOUR` / `DOWNLOAD_MINUTE`):
if suspended, the machine wakes via RTC alarm (`WakeSystem=true`),
`midnight-downloader.sh run` executes, everything downloads
(skipping anything already pulled down), and the machine is free to
sleep again afterward.

**Caveat:** RTC wake depends on hardware/firmware support. Most
desktops and many laptops handle it; some laptops using `s2idle`
suspend don't wake reliably, and some BIOS/UEFI settings disable it
by default (look for "Wake on RTC" or similar). Test once — suspend
a few minutes before the scheduled time and confirm it wakes.

## Checking on it

```bash
systemctl list-timers midnight-downloader.timer   # next run time
journalctl -u midnight-downloader.service          # past run logs
tail -f ~/telegram-downloader/cron.log              # live log
```

## Changing the schedule

Edit `DOWNLOAD_HOUR` / `DOWNLOAD_MINUTE` in `.env`, then:

```bash
sudo ./midnight-downloader.sh install-timer ~/telegram-downloader "$(whoami)"
```

## Testing manually

```bash
./midnight-downloader.sh run      # same path the scheduler uses, logs to cron.log
```

For live progress bars in your terminal instead:

```bash
source venv/bin/activate
python downloader.py
```

## Turning it off

```bash
sudo systemctl disable --now midnight-downloader.timer
```

## GUI add-ons (optional)

```bash
./midnight-downloader.sh install-gui
```

- **Desktop notifications** — automatic once installed. Popup on
  start and completion; silently skipped if no graphical session.
- **Web dashboard** — stats, a calendar of past runs, log tail, and a
  Run now button: `./midnight-downloader.sh dashboard`, then open
  <http://127.0.0.1:8765>.
- **Tray icon** — `./midnight-downloader.sh tray`, or
  `./midnight-downloader.sh install-tray` to start it on every login.

## Troubleshooting

- **Asked to log in every run** — the script prints the exact session
  path at startup (`Using session file: ... (exists: True/False)`);
  confirm it's the same path every run.
- **"Missing API_ID / API_HASH / PHONE_NUMBER"** — re-run
  `./midnight-downloader.sh setup` after filling in `.env`.
- **Nightly run doesn't happen** — check
  `systemctl list-timers midnight-downloader.timer` and
  `journalctl -u midnight-downloader.service`. If asleep and it
  didn't wake, see the RTC caveat above.
- **"Previous run still in progress, skipping"** — expected; a prior
  run is still downloading.
- **FloodWaitError** — handled automatically (waits Telegram's
  requested delay, retries up to 3 times).
