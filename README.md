# Midnight Downloader

Downloads every file from your Telegram **Saved Messages** on a
schedule, using your personal account (via Telethon) rather than a
bot — so there's no 50MB size limit. Wakes your machine from suspend
if needed, runs the download, and can go back to sleep afterward.

## Files

| File | Purpose |
|---|---|
| `midnight-downloader.sh` | Everything: one-time setup, and the run the scheduler calls nightly |
| `downloader.py` | The actual download logic |
| `.env.example` | Config template — copy to `.env` |
| `requirements.txt` | Python dependencies |
| `README.md` | This file |

## 1. Get Telegram API credentials

1. Go to <https://my.telegram.org> and log in with your phone number.
2. Click **API Development Tools**.
3. Create an app (any name/URL is fine — this is just for API access).
4. Copy the **api_id** and **api_hash** shown.

## 2. Install system packages (Arch Linux)

```bash
sudo pacman -S python
```

That's it — scheduling uses `systemd`, which Arch already has.

## 3. Run setup

```bash
cd ~/telegram-downloader
chmod +x midnight-downloader.sh
./midnight-downloader.sh setup
```

This one command does everything, in order:

1. **Creates a Python virtual environment** and installs dependencies
   (`telethon`, `python-dotenv`, `tqdm`) into it.
2. **Checks your `.env`.** First time through, it creates `.env` from
   `.env.example` and stops so you can fill in `API_ID`, `API_HASH`,
   and `PHONE_NUMBER`. Edit `.env`, then run `./midnight-downloader.sh
   setup` again to continue.
3. **Logs into Telegram interactively** if there's no saved session
   yet — Telegram will text/message you a login code, enter it (and
   your 2FA password if you have one) when prompted. This also runs
   your first full download as part of connecting, which is expected.
4. **Installs the nightly schedule** — this step asks for your `sudo`
   password, because setting a hardware wake alarm and installing a
   system-level schedule both require root. The download itself still
   runs as your normal user afterward, not root.

When it finishes, **you're done** — nothing else to run, nothing to
leave open.

## 4. What happens every night

You don't need to `cd` into this folder or activate anything before
going to sleep. The scheduled run is a completely separate process
started by `systemd`, unrelated to whatever your terminal is doing —
closing your terminal, or never opening one again, changes nothing.

At the time set in `.env` (`DOWNLOAD_HOUR` / `DOWNLOAD_MINUTE`,
24-hour format):

1. If the machine is suspended, `systemd` uses the hardware RTC
   (real-time clock) to wake it — this is what `WakeSystem=true` in
   the installed timer does. If the machine is already awake, this
   step is a no-op.
2. `systemd` runs `midnight-downloader.sh run`, which activates the
   venv internally, locks against overlapping runs, and calls
   `downloader.py`.
3. Everything downloads, skipping anything already pulled down in a
   previous run.
4. The machine is free to go back to sleep on its own afterward —
   nothing here keeps it awake once the run finishes.

**Caveat worth knowing:** RTC wake depends on your hardware/firmware
actually supporting it. Most desktops and many laptops do, but some
laptops using modern low-power suspend (`s2idle`) don't wake reliably
from an RTC alarm, and some BIOS/UEFI settings disable it by default
(look for something like "Wake on RTC" or "Power On by RTC Alarm").
**Test it once** before trusting it every night: suspend the machine
a few minutes before the scheduled time and confirm it wakes and
downloads.

## Checking on it

```bash
systemctl list-timers midnight-downloader.timer   # confirm next run time
journalctl -u midnight-downloader.service          # see logs from past runs
tail -f ~/telegram-downloader/cron.log              # live log during/after a run
```

## Changing the schedule

Edit `DOWNLOAD_HOUR` / `DOWNLOAD_MINUTE` in `.env`, then re-install
just the schedule (skips the venv/login steps since those are already
done):

```bash
sudo ./midnight-downloader.sh install-timer ~/telegram-downloader "$(whoami)"
```

## Testing a run manually

```bash
./midnight-downloader.sh run
```

This is the exact same code path the scheduler uses, so it's a
reliable way to confirm things work — output goes to `cron.log`, not
your terminal, so `tail -f cron.log` in a second window if you want to
watch it live. For a version with live progress bars printed directly
to your terminal instead, run the Python script directly:

```bash
source venv/bin/activate
python downloader.py
```

## Turning it off

```bash
sudo systemctl disable --now midnight-downloader.timer
```

## What each problem in the original draft was and how this fixes it

- **`Document has no attribute 'name'`** / **wrong or corrupted-sounding
  files** — the original approach guessed file extensions from mime
  type, and got audio wrong (Opus/OGG voice notes saved as `.mp3`,
  which made players decode them with the wrong codec — that's what
  produced warped, "groggy" playback, not actual corruption). This
  script no longer guesses extensions at all: each file downloads into
  an isolated temp folder where Telethon resolves the real filename
  itself (the sender's attached name, or Telethon's own internal
  resolution for voice notes/round videos/etc — the same logic
  official Telegram clients use), then gets moved into place.
- **Duplicates not skipped** — tracked by Telegram's own document/photo
  ID in a manifest file, not by filename, so it survives re-runs and
  never confuses two different files that happen to share a name.
- **No download speed shown** — a per-file progress bar reports live
  KB/s or MB/s, plus an overall bar tracking files completed.
- **`progress_callback() missing 'filename'`** — Telethon actually
  calls this callback as `progress_callback(current, total)`, with no
  filename argument. Fixed to match the real signature.
- **Compilation issues with the Bot API server** — avoided entirely by
  using Telethon with your user account instead of the Telegram Bot
  API, so there's nothing to compile and no 50MB bot upload/download cap.
- **Pagination through large histories** — handled via a loop that
  advances `offset_id` until Telegram returns a page shorter than the
  requested limit.
- **Crash losing all progress** — the manifest saves to disk after
  *every* file, not just at the end.
- **Re-logging in every run** — caused by `.env`/session paths being
  resolved relative to the current working directory instead of the
  script's own location. Both are now pinned to fixed, absolute paths.
- **Telegram rate limits (`FloodWaitError`)** during large batches are
  caught specifically, and the script sleeps for exactly as long as
  Telegram asks before retrying that file (up to 3 times), instead of
  hammering the API and failing a burst of files.
- **Silently truncated downloads** — final file size is compared
  against what Telegram reported for the message; a mismatch is
  flagged instead of counted as a clean success.
- **Orphaned partial downloads if the process is killed** — temp files
  live in a dedicated `.download_tmp` folder that's wiped clean at the
  start of every run, so a crash can't leave debris behind
  indefinitely.
- **Ctrl+C during a manual run dumping a raw traceback** — now exits
  cleanly with a short message; nothing is lost either way since the
  manifest is saved incrementally.
- **Machine asleep at the scheduled time** — plain `cron` only fires
  while the OS is already running and silently misses the trigger if
  the machine is suspended. This is why scheduling uses a `systemd`
  timer with `WakeSystem=true` instead, which asks the hardware to
  wake the machine for the scheduled run.

## Troubleshooting

- **Asked to log in every run** — check `.env` and the session file
  are in the same fixed location every time; the script prints the
  exact session path it's using at startup (`Using session file: ...
  (exists: True/False)`) so you can confirm it's stable across runs.
- **"Missing API_ID / API_HASH / PHONE_NUMBER"** — `.env` isn't filled
  in yet; re-run `./midnight-downloader.sh setup`.
- **Nightly run doesn't seem to happen** — check
  `systemctl list-timers midnight-downloader.timer` for the next
  scheduled time, and `journalctl -u midnight-downloader.service` for
  errors. If the machine was asleep and didn't wake, see the RTC wake
  caveat above.
- **"Previous run still in progress, skipping"** — a prior run is
  still downloading (large batches can take a while); this is the
  lock file working as intended, not a bug.
- **Rate limiting / FloodWaitError** — the script waits out Telegram's
  requested delay and retries automatically. If a file still fails
  after 3 attempts, it'll be picked up again on the next scheduled run.
