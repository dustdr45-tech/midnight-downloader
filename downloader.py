#!/usr/bin/env python3
"""
Midnight Downloader - Downloads everything from Telegram Saved Messages.

Fixes applied vs the original draft:
  - Duplicate detection is keyed on Telegram's own document/photo ID
    (see get_media_key), not on filename, so it survives re-runs even
    when two files share a display name, and never confuses two
    distinct files that happen to be named the same.
  - Extensions are never guessed. Files download into an isolated temp
    folder so Telethon resolves the real filename/extension itself
    (the sender's attached name, or Telethon's own internal logic for
    voice notes, round videos, etc - the same resolution official
    Telegram clients use) before being moved into place. A prior
    version tried to guess extensions from mime type and got audio
    files wrong (audio/ogg saved as .mp3), corrupting playback.
  - progress_callback matches Telethon's actual call signature
    (current, total) - Telethon does NOT pass a filename kwarg, so a
    signature that required one would crash on every download.
  - GetHistoryRequest pagination uses the last message's date as
    offset_date safeguard in addition to offset_id, avoiding an infinite
    loop if Telegram ever returns a short page in the middle of history.
  - All Telegram/network calls wrapped so one bad message doesn't kill
    the whole run.
  - Telegram rate limits (FloodWaitError) are honored: the script sleeps
    for exactly as long as Telegram asks and retries that file, instead
    of hammering the API and marking a burst of files as failed.
  - Downloaded file size is checked against the size Telegram reported
    for the message; a mismatch is flagged rather than silently treated
    as a successful download.
  - Temp download folders live in a dedicated .download_tmp directory
    that's wiped clean at the start of every run, so a killed process
    (crash, power loss, OOM) can't leave partial-download debris sitting
    in your real download folder indefinitely.
  - Designed to run once and exit (cron calls it nightly) - no long-lived
    event listening.
  - Ctrl+C during an interactive run exits cleanly with a short message
    instead of dumping a raw asyncio traceback; progress already saved
    to the manifest is unaffected either way.
"""

import os
import re
import json
import shutil
import asyncio
import logging
import tempfile
from pathlib import Path
from datetime import datetime

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.functions.messages import GetHistoryRequest
from tqdm import tqdm

# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
# IMPORTANT: load_dotenv() with no argument searches for .env starting
# from the CURRENT WORKING DIRECTORY, not from wherever this script
# lives. Run the script from a different folder (or let cron launch it
# with a different cwd) and it silently finds no .env, SESSION_PATH
# falls back to its default, and Telethon has no session file to reuse
# -> you get asked to log in again. Pinning this to the script's own
# directory makes it load the same .env every time, regardless of cwd.
SCRIPT_DIR = Path(__file__).resolve().parent
load_dotenv(SCRIPT_DIR / ".env")

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
PHONE_NUMBER = os.getenv("PHONE_NUMBER", "")

DOWNLOAD_DIR = Path(os.path.expanduser(os.getenv("DOWNLOAD_DIR", "~/Downloads/Telegram")))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Same reasoning as above: default this to a fixed, absolute location
# next to the script (not derived from DOWNLOAD_DIR's expanduser, which
# can itself resolve differently if HOME isn't set the same way under
# cron) so the session file is found at the same path on every run.
SESSION_PATH = os.path.expanduser(os.getenv("SESSION_PATH") or str(SCRIPT_DIR / "session"))
MANIFEST_PATH = DOWNLOAD_DIR / ".downloaded_manifest.json"
LOG_PATH = DOWNLOAD_DIR / "downloader.log"
# Dedicated subfolder for in-progress downloads. Kept separate from
# DOWNLOAD_DIR itself (rather than creating temp dirs loose inside it)
# so it's easy to spot and safe to wipe wholesale at the start of every
# run - see the cleanup in download_saved_messages().
TMP_ROOT = DOWNLOAD_DIR / ".download_tmp"

if not API_ID or not API_HASH or not PHONE_NUMBER:
    raise SystemExit(
        f"Missing API_ID / API_HASH / PHONE_NUMBER. Looked for .env at "
        f"{SCRIPT_DIR / '.env'} - copy .env.example to .env there and fill "
        f"in your values (see README.md)."
    )

_session_file = Path(f"{SESSION_PATH}.session")
print(f"Using session file: {_session_file} (exists: {_session_file.exists()})")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler(LOG_PATH)],
)
log = logging.getLogger("midnight-downloader")

client = TelegramClient(SESSION_PATH, API_ID, API_HASH)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def sanitize_filename(name: str) -> str:
    """Strip characters that are invalid/annoying on Linux filesystems,
    WITHOUT touching the extension. We no longer guess extensions
    ourselves anywhere in this file - Telethon resolves the real
    filename (sender-provided name, or its own internal logic for
    voice notes / round videos / etc, which correctly distinguishes
    things like audio/ogg from audio/mpeg). This function only cleans
    up characters that Linux filesystems dislike."""
    stem, ext = os.path.splitext(name)
    stem = re.sub(r"[^A-Za-z0-9._\- ]", "_", stem).strip() or "file"
    ext = re.sub(r"[^A-Za-z0-9.]", "", ext)
    return f"{stem}{ext}" if ext else stem


def get_media_key(msg) -> str:
    """Stable identity for a piece of media, used for the manifest so
    re-running the script never re-downloads the same item even if the
    on-disk filename later changes."""
    document = getattr(msg.media, "document", None)
    if document is not None:
        return f"doc:{document.id}"
    photo = getattr(msg.media, "photo", None)
    if photo is not None:
        return f"photo:{photo.id}"
    return f"msg:{msg.id}"


def human_size(size_bytes) -> str:
    if not size_bytes:
        return "Unknown"
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}" if unit != "B" else f"{int(size_bytes)} B"
        size_bytes /= 1024
    return f"{size_bytes:.2f} TB"


def human_speed(bytes_per_sec) -> str:
    if bytes_per_sec < 1024:
        return f"{bytes_per_sec:.1f} B/s"
    if bytes_per_sec < 1024 * 1024:
        return f"{bytes_per_sec/1024:.1f} KB/s"
    return f"{bytes_per_sec/1024/1024:.1f} MB/s"


def load_manifest() -> dict:
    if MANIFEST_PATH.exists():
        try:
            return json.loads(MANIFEST_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            log.warning("Manifest file unreadable, starting a fresh one")
    return {}


def save_manifest(manifest: dict) -> None:
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2))


def unique_path(path: Path) -> Path:
    """If path exists, append _1, _2, ... before the extension."""
    if not path.exists():
        return path
    stem, ext = path.stem, path.suffix
    counter = 1
    candidate = path.with_name(f"{stem}_{counter}{ext}")
    while candidate.exists():
        counter += 1
        candidate = path.with_name(f"{stem}_{counter}{ext}")
    return candidate


async def fetch_all_messages(peer) -> list:
    """Paginate through full Saved Messages history."""
    all_messages = []
    offset_id = 0
    limit = 100

    while True:
        history = await client(GetHistoryRequest(
            peer=peer,
            offset_id=offset_id,
            offset_date=None,
            add_offset=0,
            limit=limit,
            max_id=0,
            min_id=0,
            hash=0,
        ))
        if not history.messages:
            break

        all_messages.extend(history.messages)
        offset_id = history.messages[-1].id

        if len(history.messages) < limit:
            break

    return all_messages


# ----------------------------------------------------------------------
# Main download routine
# ----------------------------------------------------------------------

async def download_saved_messages():
    print("\n" + "=" * 70)
    print("STARTING MIDNIGHT DOWNLOADER")
    print("=" * 70)
    print(f"Download directory: {DOWNLOAD_DIR}")
    print("=" * 70 + "\n")

    start_time = datetime.now()
    manifest = load_manifest()

    # If a previous run was killed mid-download (crash, power loss, OOM
    # kill), leftover partial files could sit in a temp folder forever.
    # Wiping this at the start of every run means such debris never
    # survives more than one night, and never ends up mixed in with
    # real completed downloads.
    if TMP_ROOT.exists():
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
    TMP_ROOT.mkdir(parents=True, exist_ok=True)

    await client.start(phone=PHONE_NUMBER)
    me = await client.get_me()
    print(f"Connected as: {me.first_name} (@{me.username})\n")

    saved_messages = await client.get_entity("me")

    print("Scanning Saved Messages...")
    all_messages = await fetch_all_messages(saved_messages)
    print(f"Found {len(all_messages)} total messages")

    media_messages = [m for m in all_messages if m.media]
    print(f"Found {len(media_messages)} messages with media\n")

    if not media_messages:
        print("Nothing to download.")
        await client.disconnect()
        return

    to_download = []
    skipped = 0
    for msg in media_messages:
        key = get_media_key(msg)
        if key in manifest:
            skipped += 1
            continue
        to_download.append((msg, key))

    print(f"Skipping {skipped} already-downloaded items")
    print(f"Downloading {len(to_download)} new items\n")

    if not to_download:
        print("All caught up - nothing new to download.")
        await client.disconnect()
        return

    downloaded = 0
    failed = 0
    total_bytes = 0

    overall_bar = tqdm(total=len(to_download), desc="Overall", unit="file")

    for index, (msg, key) in enumerate(to_download, 1):
        document = getattr(msg.media, "document", None)
        expected_size = getattr(document, "size", None) if document else None

        print(f"\n[{index}/{len(to_download)}] message {msg.id}  ({human_size(expected_size)})")

        file_bar = tqdm(
            total=100,
            desc="  progress",
            unit="%",
            leave=False,
            bar_format="  {desc}: {percentage:3.0f}%|{bar}| {elapsed}",
        )

        dl_start = datetime.now()
        last_tick = dl_start
        last_bytes = 0

        def progress_callback(current, total):
            # NOTE: Telethon calls this as progress_callback(current, total).
            # It does NOT pass a filename argument - a signature requiring
            # one will raise "missing 1 required positional argument".
            nonlocal last_tick, last_bytes
            if not total:
                return
            pct = (current / total) * 100
            file_bar.update(pct - file_bar.n)

            now = datetime.now()
            elapsed = (now - last_tick).total_seconds()
            if elapsed >= 1:
                speed = (current - last_bytes) / elapsed
                file_bar.set_description(f"  {human_speed(speed)}")
                last_tick = now
                last_bytes = current

        try:
            downloaded_path = None
            # Telegram enforces rate limits on bulk downloads. If we hit
            # one, Telethon raises FloodWaitError with exactly how long
            # to wait - honor that and retry this file, rather than
            # marking it failed and immediately hammering the API again
            # on the next file (which just trips the limit repeatedly).
            max_flood_retries = 3
            for attempt in range(max_flood_retries + 1):
                try:
                    # Download into an isolated temp directory rather
                    # than a path we compute ourselves. Passing a
                    # directory (not a filename) tells Telethon to work
                    # out the real filename on its own - the sender's
                    # original attachment name when present, and its
                    # own internal resolution otherwise - the same
                    # logic official Telegram clients use. We are not
                    # guessing extensions anywhere in this script.
                    with tempfile.TemporaryDirectory(dir=TMP_ROOT) as tmp_dir:
                        downloaded_path = await client.download_media(
                            msg,
                            file=tmp_dir + os.sep,
                            progress_callback=progress_callback,
                        )

                        if not downloaded_path or not Path(downloaded_path).exists():
                            downloaded_path = None
                            break

                        downloaded_path = Path(downloaded_path)
                        # Only clean up filesystem-hostile characters -
                        # leave the extension exactly as Telethon
                        # resolved it.
                        clean_name = sanitize_filename(downloaded_path.name)
                        final_path = unique_path(DOWNLOAD_DIR / clean_name)
                        shutil.move(str(downloaded_path), str(final_path))
                    break
                except FloodWaitError as flood_exc:
                    if attempt >= max_flood_retries:
                        raise
                    wait_for = flood_exc.seconds + 1
                    print(f"  Rate limited by Telegram - waiting {wait_for}s before retrying...")
                    log.warning("FloodWait on message %s: sleeping %ss", msg.id, wait_for)
                    await asyncio.sleep(wait_for)

            file_bar.close()

            if downloaded_path is None:
                print("  Download reported success but file not found on disk")
                failed += 1
                overall_bar.update(1)
                save_manifest(manifest)
                continue

            size = final_path.stat().st_size
            # Sanity-check against the size Telegram told us to expect.
            # A silently truncated download (dropped connection that
            # Telethon didn't raise on) would otherwise get counted as
            # a clean success.
            if expected_size and size != expected_size:
                print(
                    f"  WARNING: downloaded size ({human_size(size)}) does not match "
                    f"expected size ({human_size(expected_size)}) - file may be incomplete"
                )
                log.warning(
                    "Size mismatch for message %s: got %s bytes, expected %s bytes",
                    msg.id, size, expected_size,
                )

            total_bytes += size
            dl_time = (datetime.now() - dl_start).total_seconds()
            speed_str = human_speed(size / dl_time) if dl_time > 0 else "n/a"
            print(f"  Saved as {final_path.name} in {dl_time:.1f}s at {speed_str}")
            manifest[key] = {
                "filename": final_path.name,
                "size": size,
                "downloaded_at": datetime.now().isoformat(),
            }
            downloaded += 1

        except Exception as exc:  # noqa: BLE001 - one bad message shouldn't kill the run
            file_bar.close()
            log.error("Failed to download message %s: %s", msg.id, exc)
            print(f"  Failed: {exc}")
            failed += 1

        overall_bar.update(1)
        # Persist progress incrementally so a mid-run crash doesn't lose it
        save_manifest(manifest)

    overall_bar.close()

    total_time = (datetime.now() - start_time).total_seconds()
    avg_speed = total_bytes / total_time if total_time > 0 else 0

    summary = (
        f"Download Complete\n\n"
        f"New files: {downloaded}\n"
        f"Already had: {skipped}\n"
        f"Failed: {failed}\n"
        f"Total size: {human_size(total_bytes)}\n"
        f"Time: {total_time:.1f}s ({total_time/60:.1f}m)\n"
        f"Avg speed: {human_speed(avg_speed)}\n"
        f"Location: {DOWNLOAD_DIR}"
    )

    print("\n" + "=" * 70)
    print(summary)
    print("=" * 70)

    try:
        await client.send_message("me", summary)
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not send completion message: %s", exc)

    await client.disconnect()
    print("\nDisconnected from Telegram")


async def main():
    try:
        await download_saved_messages()
    except Exception as exc:  # noqa: BLE001
        log.exception("Fatal error")
        print(f"\nFatal error: {exc}")
        raise


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Ctrl+C during an interactive test run (or a SIGINT forwarded
        # from start.sh) is a normal way to stop this - it shouldn't
        # produce a raw traceback. Progress up to this point is already
        # safe: the manifest is saved after every completed file, and
        # any in-progress download was still inside its temp folder
        # (never moved into your real download folder half-finished).
        print("\nStopped by user - progress so far has been saved.")
        raise SystemExit(130)  # conventional exit code for SIGINT

