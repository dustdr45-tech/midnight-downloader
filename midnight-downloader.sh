#!/usr/bin/env bash
# Midnight Telegram Downloader - single combined script.
# Usage: ./midnight-downloader.sh setup   (run once, by hand)
#        ./midnight-downloader.sh run     (called by the scheduler)

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
  $(basename "$0") setup           One-time setup (venv, deps, login, schedule)
  $(basename "$0") run             Run one download pass now
  $(basename "$0") install-gui     Install optional GUI dependencies (dashboard + tray)
  $(basename "$0") install-tray    Make the tray icon start automatically on login
  $(basename "$0") dashboard       Launch the local web dashboard
  $(basename "$0") tray            Launch the tray icon (foreground)
USAGE
}

read_env_var() {
    # $1 = key, $2 = .env path. `|| true`: a missing key must not trip
    # set -e + pipefail and kill the whole script.
    grep -E "^$1=" "$2" 2>/dev/null | tail -n1 | cut -d= -f2- | tr -d '[:space:]' || true
}

expand_tilde() {
    # Bash doesn't expand ~ inside a variable's value; do it by hand.
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

cmd_run() {
    local python_bin
    python_bin="$(pick_python)"

    exec 200>"$LOCK_FILE"
    if ! flock -n 200; then
        echo "$(date): Previous run still in progress, skipping." >> "${SCRIPT_DIR}/cron.log"
        exit 0
    fi

    echo "$(date): Starting download run" >> "${SCRIPT_DIR}/cron.log"

    set +e
    "$python_bin" "${SCRIPT_DIR}/downloader.py" >> "${SCRIPT_DIR}/cron.log" 2>&1
    local exit_code=$?
    set -e

    echo "$(date): Run finished with exit code ${exit_code}" >> "${SCRIPT_DIR}/cron.log"
    exit "$exit_code"
}

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
# network-online.target ordering above mainly helps at boot - it isn't
# reliably re-checked after a resume-from-suspend wake, so this buffer
# gives Wi-Fi a moment to reconnect regardless of that ordering.
ExecStartPre=/bin/sleep 15
ExecStart=${project_dir}/$(basename "$0") run
EOF

    cat > "/etc/systemd/system/${SERVICE_NAME}.timer" <<EOF
[Unit]
Description=Nightly schedule for ${SERVICE_NAME}

[Timer]
OnCalendar=*-*-* ${hh}:${mm}:00
WakeSystem=true
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

cmd_install_gui() {
    if [ ! -x "${VENV_DIR}/bin/pip" ]; then
        echo "No venv found - run './$(basename "$0") setup' first." >&2
        exit 1
    fi
    "${VENV_DIR}/bin/pip" install --quiet -r "${SCRIPT_DIR}/requirements-gui.txt"
    echo "GUI dependencies installed."
    echo "Try:  ./$(basename "$0") dashboard"
    echo "  or: ./$(basename "$0") tray"
}

cmd_dashboard() {
    "$(pick_python)" "${SCRIPT_DIR}/dashboard.py"
}

cmd_tray() {
    "$(pick_python)" "${SCRIPT_DIR}/tray.py"
}

cmd_install_tray() {
    local autostart_dir="${HOME}/.config/autostart"
    mkdir -p "$autostart_dir"
    cat > "${autostart_dir}/midnight-downloader-tray.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Midnight Downloader Tray
Comment=Tray icon for the Midnight Telegram Downloader
Exec=$(pick_python) ${SCRIPT_DIR}/tray.py
Icon=applications-internet
Terminal=false
X-GNOME-Autostart-enabled=true
EOF
    echo "Installed autostart entry at ${autostart_dir}/midnight-downloader-tray.desktop"
    echo "The tray icon will start next time you log in, or run now with:"
    echo "  ./$(basename "$0") tray"
}

case "${1:-}" in
    setup) cmd_setup ;;
    run) cmd_run ;;
    install-timer) shift; cmd_install_timer "$@" ;;
    install-gui) cmd_install_gui ;;
    install-tray) cmd_install_tray ;;
    dashboard) cmd_dashboard ;;
    tray) cmd_tray ;;
    *) usage; exit 1 ;;
esac
