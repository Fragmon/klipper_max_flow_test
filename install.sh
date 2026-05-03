#!/bin/bash
# TMC Flow Test — installer
# Creates a symlink from the repo into Klipper's extras/ directory and
# (optionally) prints the moonraker.conf snippet for update-manager
# integration.

set -e

# ─── Self-bootstrap: ensure execute bits are set ──────────────────────
# When this repo is cloned via GitHub Web-UI uploads or transferred
# through some non-Git tools, the executable bit on shell scripts can
# get stripped. Re-set it here so future runs work as `./install.sh`
# (no need for `bash install.sh`).
SCRIPT_PATH="${BASH_SOURCE[0]}"
if [ ! -x "${SCRIPT_PATH}" ]; then
    chmod +x "${SCRIPT_PATH}" 2>/dev/null || true
fi
# Also fix any other .sh files in the repo root, just in case.
SCRIPT_DIR="$(cd "$(dirname "${SCRIPT_PATH}")" && pwd)"
find "${SCRIPT_DIR}" -maxdepth 1 -name "*.sh" -exec chmod +x {} \; 2>/dev/null || true

# ─── Defaults ─────────────────────────────────────────────────────────
KLIPPER_DIR="${KLIPPER_DIR:-${HOME}/klipper}"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
PLUGIN_FILE="tmc_flow_test.py"
EXTRAS_DIR="${KLIPPER_DIR}/klippy/extras"
LINK_PATH="${EXTRAS_DIR}/${PLUGIN_FILE}"
SOURCE_PATH="${REPO_DIR}/${PLUGIN_FILE}"

# ─── Helpers ──────────────────────────────────────────────────────────
info()  { printf '\e[34m[info]\e[0m %s\n' "$1"; }
ok()    { printf '\e[32m[ ok ]\e[0m %s\n' "$1"; }
warn()  { printf '\e[33m[warn]\e[0m %s\n' "$1"; }
err()   { printf '\e[31m[fail]\e[0m %s\n' "$1" >&2; }

# ─── Pre-flight checks ────────────────────────────────────────────────
info "Repo directory:    ${REPO_DIR}"
info "Klipper directory: ${KLIPPER_DIR}"

if [ ! -d "${KLIPPER_DIR}" ]; then
    err "Klipper directory not found at ${KLIPPER_DIR}."
    err "Set KLIPPER_DIR=/path/to/klipper if it lives elsewhere."
    exit 1
fi
if [ ! -d "${EXTRAS_DIR}" ]; then
    err "Klipper extras directory not found at ${EXTRAS_DIR}."
    exit 1
fi
if [ ! -f "${SOURCE_PATH}" ]; then
    err "Plugin source ${SOURCE_PATH} missing — repo seems incomplete."
    exit 1
fi

# ─── Validate plugin syntax before linking ────────────────────────────
info "Validating plugin syntax…"
if ! python3 -c "import ast; ast.parse(open('${SOURCE_PATH}').read())" 2>/dev/null; then
    err "Plugin file has a Python syntax error — refusing to install."
    err "Run: python3 -c \"import ast; ast.parse(open('${SOURCE_PATH}').read())\""
    exit 1
fi
ok "Plugin syntax OK"

# ─── Create symlink ───────────────────────────────────────────────────
if [ -L "${LINK_PATH}" ]; then
    info "Existing symlink found, removing…"
    rm -f "${LINK_PATH}"
elif [ -f "${LINK_PATH}" ]; then
    warn "Real file (not symlink) exists at ${LINK_PATH}"
    warn "Backing up to ${LINK_PATH}.bak before replacing"
    mv "${LINK_PATH}" "${LINK_PATH}.bak"
fi

ln -s "${SOURCE_PATH}" "${LINK_PATH}"
ok "Symlinked ${LINK_PATH} → ${SOURCE_PATH}"

# ─── Clear stale Python cache ─────────────────────────────────────────
PYCACHE="${EXTRAS_DIR}/__pycache__"
if [ -d "${PYCACHE}" ]; then
    rm -f "${PYCACHE}/${PLUGIN_FILE%.py}".*.pyc 2>/dev/null || true
    ok "Cleared stale Python cache"
fi

# ─── Done ─────────────────────────────────────────────────────────────
echo
ok "Installation complete."
echo
echo "Next steps:"
echo "  1. Restart Klipper:    sudo systemctl restart klipper"
echo "  2. Verify in console:  TMC_FLOW_STATUS"
echo
echo "To enable Moonraker update-manager integration, add this block"
echo "to your moonraker.conf (typically ~/printer_data/config/moonraker.conf):"
echo
echo "  [update_manager Crydteam-Tuning-Plugins]"
echo "  type: git_repo"
echo "  primary_branch: main"
echo "  path: ${REPO_DIR}"
echo "  origin: https://github.com/Fragmon/klipper_max_flow_test.git"
echo "  managed_services: klipper"
echo
echo "Then restart Moonraker:  sudo systemctl restart moonraker"
echo
