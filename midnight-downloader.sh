#!/usr/bin/env bash
# Midnight Telegram Downloader - single combined script.
#
# Usage:
#   ./midnight-downloader.sh setup    One-time setup. Run this yourself,
#                                      once, in your terminal.
#   ./midnight-downloader.sh run      Runs one download pass. This is
#                                      what the scheduled timer calls -
#                                      you never need to run this by hand.
#
# After `setup` finishes, you're done: close your terminal, go to sleep,
# and the machine will wake itself at the scheduled time, run the
# download, and can go back to sleep afterward. Nothing needs to be left
# open or activated beforehand - the scheduled run is a separate process
# started by systemd, not a continuation of your terminal session.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="${SCRIPT_DIR}/venv"
ENV_FILE="${SCRIPT_DIR}/.env"
LOCK_FILE="/tmp/midnight-downloader.lock"
SERVICE_NAME="midnight-downloader"

usage() {
    cat <<USAGE
Usage:
  $(basename "$0") setup    One-time setup (venv, deps, login, schedule)
  $(basename "$0") run      Run one download pass now
USAGE
}

read_env_var() {
    # $1 = variable name, $2 = path to .env file
    # A missing key is a normal, expected case (we fall back to a
    # default wherever this is called) - NOT an error. Without the
    # `|| true`, grep finding no match exits 1, and because the script
    # runs with `set -eo pipefail`, that failure would silently kill
    # the entire script the instant a key happens to be absent from
    # .env, with no error message at all.
    grep -E "^$1=" "$2" 2>/dev/null | tail -n1 | cut -d= -f2- | tr -d '[:space:]' || true
}

expand_tilde() {
    # Bash does NOT expand ~ inside a variable's value the way it does
    # when you type ~ directly at a prompt - a value read out of .env
    # like "~/Downloads/Telegram" stays a literal string starting with
    # the character ~, which then gets treated as a real (relative)
    # folder name instead of your home directory. Expand it by hand.
    case "$1" in
        "~") printf '%s' "$HOME" ;;
        "~/"*) printf '%s' "${HOME}${1:1}" ;;
        *) printf '%s' "$1" ;;
    esac
}

pick_python() {
    if [ -x "${VENV_DIR}/bin/python3" ]; then
        echo "${VENV_DIR}/bin/python3"
    else
        command -v python3
    fi
}

# ----------------------------------------------------------------------
# run: what the scheduled timer actually calls every night
# ----------------------------------------------------------------------
cmd_run() {
    local python_bin
    python_bin="$(pick_python)"

    # Prevent overlapping runs if a previous invocation is still going.
    exec 200>"$LOCK_FILE"
    if ! flock -n 200; then
        echo "$(date): Previous run still in progress, skipping." >> "${SCRIPT_DIR}/cron.log"
        exit 0
    fi

    echo "$(date): Starting download run" >> "${SCRIPT_DIR}/cron.log"

    # Run outside `set -e` so a non-zero exit doesn't abort this script
    # before we get to log it.
    set +e
    "$python_bin" "${SCRIPT_DIR}/downloader.py" >> "${SCRIPT_DIR}/cron.log" 2>&1
    local exit_code=$?
    set -e

    echo "$(date): Run finished with exit code ${exit_code}" >> "${SCRIPT_DIR}/cron.log"
    exit "$exit_code"
}

# ----------------------------------------------------------------------
# setup: one-time interactive setup, run by hand
# ----------------------------------------------------------------------
cmd_setup() {
    echo "== Step 1/4: Python environment =="
    if [ ! -d "$VENV_DIR" ]; then
        python3 -m venv "$VENV_DIR"
        echo "Created venv at $VENV_DIR"
    else
        echo "venv already exists"
    fi
    "${VENV_DIR}/bin/pip" install --quiet --upgrade pip
    "${VENV_DIR}/bin/pip" install --quiet -r "${SCRIPT_DIR}/requirements.txt"
    echo "Dependencies installed"
    echo

    echo "== Step 2/4: configuration =="
    if [ ! -f "$ENV_FILE" ]; then
        cp "${SCRIPT_DIR}/.env.example" "$ENV_FILE"
        echo "Created .env from the template."
        echo "Edit it now with your API_ID / API_HASH / PHONE_NUMBER"
        echo "(get these from https://my.telegram.org), then re-run:"
        echo "  ./$(basename "$0") setup"
        exit 0
    fi
    if ! grep -qE '^API_ID=.+' "$ENV_FILE" || ! grep -qE '^API_HASH=.+' "$ENV_FILE" \
        || ! grep -qE '^PHONE_NUMBER=.+' "$ENV_FILE"; then
        echo "API_ID / API_HASH / PHONE_NUMBER in .env look incomplete." >&2
        echo "Fill them in, then re-run: ./$(basename "$0") setup" >&2
        exit 1
    fi
    echo ".env looks configured"
    echo

    echo "== Step 3/4: Telegram login =="
    local session_path session_file
    session_path="$(expand_tilde "$(read_env_var SESSION_PATH "$ENV_FILE")")"
    session_file="${session_path:-${SCRIPT_DIR}/session}.session"
    if [ -f "$session_file" ]; then
        echo "Existing Telegram session found - skipping interactive login"
    else
        echo "No saved session yet. Telegram will send you a login code now."
        # This also runs a full download pass as part of logging in
        # (Telethon requires a real connection to authenticate) - that's
        # expected and fine, it's your first sync.
        "${VENV_DIR}/bin/python3" "${SCRIPT_DIR}/downloader.py" || true
        if [ ! -f "$session_file" ]; then
            echo "Login didn't complete (no session file at $session_file)." >&2
            echo "Re-run ./$(basename "$0") setup to try again." >&2
            exit 1
        fi
        echo "Logged in - session saved for future runs."
    fi
    echo

    echo "== Step 4/4: schedule (wakes the machine from suspend) =="
    echo "This needs sudo - it sets a hardware wake alarm and installs a"
    echo "system-level schedule."
    sudo "$0" install-timer "$SCRIPT_DIR" "$(whoami)"
    echo
    echo "All set. From now on: just let the machine sleep. It will wake"
    echo "itself at the scheduled time, run the download, and can go back"
    echo "to sleep afterward - nothing needs to be left open beforehand."
}

# ----------------------------------------------------------------------
# install-timer: internal, called via sudo from `setup` (or run directly)
# ----------------------------------------------------------------------
cmd_install_timer() {
    local project_dir="${1:-$SCRIPT_DIR}"
    local target_user="${2:-${SUDO_USER:-}}"

    if [ "$(id -u)" -ne 0 ]; then
        echo "install-timer must be run as root - use: sudo $0 install-timer" >&2
        exit 1
    fi
    if [ -z "$target_user" ] || [ "$target_user" = "root" ]; then
        echo "Could not determine which user to run the download as." >&2
        echo "Run this via 'sudo ./$(basename "$0") setup', not as a direct root login." >&2
        exit 1
    fi

    local env_file="${project_dir}/.env"
    local hour minute
    hour="$(read_env_var DOWNLOAD_HOUR "$env_file")"
    minute="$(read_env_var DOWNLOAD_MINUTE "$env_file")"
    hour="${hour:-0}"
    minute="${minute:-0}"

    if ! [[ "$hour" =~ ^([0-9]|1[0-9]|2[0-3])$ ]]; then
        echo "DOWNLOAD_HOUR in .env must be 0-23 (got '$hour')" >&2
        exit 1
    fi
    if ! [[ "$minute" =~ ^([0-9]|[1-5][0-9])$ ]]; then
        echo "DOWNLOAD_MINUTE in .env must be 0-59 (got '$minute')" >&2
        exit 1
    fi

    local hh mm
    hh="$(printf '%02d' "$hour")"
    mm="$(printf '%02d' "$minute")"

    cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=Midnight Telegram Downloader
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=${target_user}
WorkingDirectory=${project_dir}
ExecStart=${project_dir}/$(basename "$0") run
EOF

    cat > "/etc/systemd/system/${SERVICE_NAME}.timer" <<EOF
[Unit]
Description=Nightly schedule for ${SERVICE_NAME} (wakes the machine from suspend if needed)

[Timer]
OnCalendar=*-*-* ${hh}:${mm}:00
# The whole point of this file: ask systemd to set an RTC wake alarm
# and resume the machine from suspend for this timer, rather than
# silently missing it the way plain cron would.
WakeSystem=true
# If the machine was fully off (not just suspended) at the scheduled
# time, run the missed job next time it's up instead of skipping it
# until the next scheduled slot.
Persistent=true

[Install]
WantedBy=timers.target
EOF

    systemctl daemon-reload
    systemctl enable --now "${SERVICE_NAME}.timer"

    echo "Scheduled ${SERVICE_NAME}.timer for ${target_user}, daily at ${hh}:${mm}."
    echo "Check next run:  systemctl list-timers ${SERVICE_NAME}.timer"
    echo "Check past logs: journalctl -u ${SERVICE_NAME}.service"
}

case "${1:-}" in
    setup) cmd_setup ;;
    run) cmd_run ;;
    install-timer) shift; cmd_install_timer "$@" ;;
    *) usage; exit 1 ;;
esac
