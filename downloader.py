#!/usr/bin/env python3
"""Downloads all media from Telegram Saved Messages via Telethon (user account, no size limit)."""

import os
import re
import json
import shutil
import asyncio
import logging
import tempfile
import subprocess
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
# .env and session are pinned to the script's own directory, not cwd,
# so behavior is identical whether run by hand, cron, or systemd.
SCRIPT_DIR = Path(__file__).resolve().parent
load_dotenv(SCRIPT_DIR / ".env")

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
PHONE_NUMBER = os.getenv("PHONE_NUMBER", "")

DOWNLOAD_DIR = Path(os.path.expanduser(os.getenv("DOWNLOAD_DIR", "~/Downloads/Telegram")))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

SESSION_PATH = os.path.expanduser(os.getenv("SESSION_PATH") or str(SCRIPT_DIR / "session"))
MANIFEST_PATH = DOWNLOAD_DIR / ".downloaded_manifest.json"
LOG_PATH = DOWNLOAD_DIR / "downloader.log"
TMP_ROOT = DOWNLOAD_DIR / ".download_tmp"  # wiped clean at the start of every run

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
    """Clean filesystem-hostile characters without touching the extension."""
    stem, ext = os.path.splitext(name)
    stem = re.sub(r"[^A-Za-z0-9._\- ]", "_", stem).strip() or "file"
    ext = re.sub(r"[^A-Za-z0-9.]", "", ext)
    return f"{stem}{ext}" if ext else stem


def get_media_key(msg) -> str:
    """Stable dedup key from Telegram's own document/photo ID, not filename."""
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


def send_desktop_notification(title: str, message: str) -> None:
    """Best-effort popup via notify-send. Never fatal - skipped silently if unavailable."""
    try:
        env = os.environ.copy()
        uid = os.getuid()
        env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{uid}/bus")
        env.setdefault("DISPLAY", ":0")
        subprocess.run(
            ["notify-send", title, message],
            env=env,
            timeout=5,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.SubprocessError, OSError, FileNotFoundError):
        pass


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

async def connect_with_retries(max_attempts: int = 6, base_delay: int = 10) -> None:
    """Retry the initial connection with backoff. A scheduled wake from
    suspend can fire before Wi-Fi/network has reconnected - Telethon's
    own built-in retry only spans a few seconds, not long enough for that."""
    for attempt in range(1, max_attempts + 1):
        try:
            await client.start(phone=PHONE_NUMBER)
            return
        except (ConnectionError, OSError) as exc:
            if attempt == max_attempts:
                raise
            delay = base_delay * attempt
            print(f"Connection attempt {attempt} failed ({exc}) - retrying in {delay}s...")
            log.warning("Connection attempt %s failed: %s - retrying in %ss", attempt, exc, delay)
            await asyncio.sleep(delay)


async def download_saved_messages():
    print("\n" + "=" * 70)
    print("STARTING MIDNIGHT DOWNLOADER")
    print("=" * 70)
    print(f"Download directory: {DOWNLOAD_DIR}")
    print("=" * 70 + "\n")

    start_time = datetime.now()
    manifest = load_manifest()

    if TMP_ROOT.exists():
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
    TMP_ROOT.mkdir(parents=True, exist_ok=True)

    await connect_with_retries()
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

    send_desktop_notification(
        "Midnight Downloader",
        f"Starting download of {len(to_download)} new file(s)...",
    )

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
            # Telethon calls this as (current, total) - no filename kwarg.
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
            max_flood_retries = 3
            for attempt in range(max_flood_retries + 1):
                try:
                    # Directory target (not a filename) lets Telethon
                    # resolve the real filename/extension itself.
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

        except Exception as exc:  # noqa: BLE001
            file_bar.close()
            log.error("Failed to download message %s: %s", msg.id, exc)
            print(f"  Failed: {exc}")
            failed += 1

        overall_bar.update(1)
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

    send_desktop_notification(
        "Midnight Downloader - done",
        f"{downloaded} new file(s), {human_size(total_bytes)}"
        + (f", {failed} failed" if failed else ""),
    )

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
        print("\nStopped by user - progress so far has been saved.")
        raise SystemExit(130)
