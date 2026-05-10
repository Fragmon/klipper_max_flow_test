#!/bin/bash
# Crydteam TMC Flow Test — installer
# Style adapted from Klippain Shake&Tune installer.
#
# What this script does:
#   - validates the host is set up properly (no root, python3, Klipper)
#   - validates the plugin file's Python syntax
#   - symlinks the plugin into Klipper's extras directory
#     (symlink, not file copy, so Klipper's repo stays clean)
#   - clears stale Python cache
#   - installs the Mainsail/Fluidd macros file (copy, not symlink)
#     and adds the [include] directive to printer.cfg
#   - registers the plugin with Moonraker's update manager
#   - restarts Klipper (and Moonraker, if installed)

USER_CONFIG_PATH="${HOME}/printer_data/config"
MOONRAKER_CONFIG="${USER_CONFIG_PATH}/moonraker.conf"
KLIPPER_PATH="${KLIPPER_PATH:-${HOME}/klipper}"
EXTRAS_DIR="${KLIPPER_PATH}/klippy/extras"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_FILE="tmc_flow_test.py"
PLUGIN_SRC="${REPO_DIR}/${PLUGIN_FILE}"
PLUGIN_LINK="${EXTRAS_DIR}/${PLUGIN_FILE}"

UPDATER_NAME="Crydteam-Tuning-Plugins"
GIT_ORIGIN="https://github.com/Fragmon/klipper_max_flow_test.git"

set -eu
export LC_ALL=C


# ─── Self-bootstrap — make sure all .sh scripts are executable ────────
# When the repo is cloned via GitHub Web-UI uploads or transferred
# through some non-Git tools, the executable bit on shell scripts can
# get stripped. Re-set it here so future runs work via `./install.sh`.
chmod +x "${BASH_SOURCE[0]}" 2>/dev/null || true
find "${REPO_DIR}" -maxdepth 1 -name "*.sh" -exec chmod +x {} \; 2>/dev/null || true


function preflight_checks {
    if [ "$EUID" -eq 0 ]; then
        echo "[PRE-CHECK] This script must not be run as root!"
        exit 1
    fi

    if ! command -v python3 &> /dev/null; then
        echo "[ERROR] Python 3 is not installed. Please install Python 3 first!"
        exit 1
    fi

    if [ "$(sudo systemctl list-units --full -all -t service --no-legend | grep -F 'klipper.service')" ]; then
        printf "[PRE-CHECK] Klipper service found! Continuing...\n\n"
    else
        echo "[ERROR] Klipper service not found, please install Klipper first!"
        exit 1
    fi

    if [ ! -d "${EXTRAS_DIR}" ]; then
        echo "[ERROR] Klipper extras directory not found at ${EXTRAS_DIR}"
        echo "[ERROR] Set KLIPPER_PATH=/path/to/klipper if it lives elsewhere."
        exit 1
    fi

    if [ ! -f "${PLUGIN_SRC}" ]; then
        echo "[ERROR] Plugin source ${PLUGIN_SRC} missing — repo seems incomplete."
        exit 1
    fi
}

function validate_plugin_syntax {
    echo "[CHECK] Validating plugin Python syntax..."
    if ! python3 -c "import ast; ast.parse(open('${PLUGIN_SRC}').read())" 2>/dev/null; then
        echo "[ERROR] Plugin file has a Python syntax error — refusing to install."
        echo "[ERROR] Run: python3 -c \"import ast; ast.parse(open('${PLUGIN_SRC}').read())\""
        exit 1
    fi
    printf "[CHECK] Plugin syntax OK!\n\n"
}

function link_module {
    if [ -L "${PLUGIN_LINK}" ]; then
        echo "[INSTALL] Existing symlink found, refreshing..."
        rm -f "${PLUGIN_LINK}"
    elif [ -f "${PLUGIN_LINK}" ]; then
        local backup="${PLUGIN_LINK}.bak.$(date +%Y%m%d-%H%M%S)"
        echo "[INSTALL] Real file (not symlink) at ${PLUGIN_LINK}"
        echo "[INSTALL] Backing up to ${backup}"
        mv "${PLUGIN_LINK}" "${backup}"
    fi

    ln -frsn "${PLUGIN_SRC}" "${PLUGIN_LINK}"
    printf "[INSTALL] Symlinked %s\n          -> %s\n\n" \
        "${PLUGIN_LINK}" "${PLUGIN_SRC}"
}

function clear_pycache {
    local pycache="${EXTRAS_DIR}/__pycache__"
    if [ -d "${pycache}" ]; then
        rm -f "${pycache}/${PLUGIN_FILE%.py}".*.pyc 2>/dev/null || true
        printf "[INSTALL] Cleared stale Python cache\n\n"
    fi
}

function add_updater {
    if [ ! -f "${MOONRAKER_CONFIG}" ]; then
        echo "[INFO] moonraker.conf not found at ${MOONRAKER_CONFIG}"
        echo "[INFO] Skipping update manager registration — Moonraker is not"
        echo "[INFO] installed in the standard location, or this is a"
        echo "[INFO] Klipper-only setup."
        printf "\n"
        return 0
    fi

    local update_section
    update_section=$(grep -c "\[update_manager[a-z ]* ${UPDATER_NAME}\]" \
                     "${MOONRAKER_CONFIG}" || true)
    if [ "${update_section}" -ne 0 ]; then
        printf "[INSTALL] Update manager block already present in moonraker.conf\n\n"
        return 0
    fi

    echo "[INSTALL] Adding update manager block to moonraker.conf..."
    cat <<EOF >> "${MOONRAKER_CONFIG}"

## Crydteam TMC Flow Test automatic update management
[update_manager ${UPDATER_NAME}]
type: git_repo
primary_branch: main
path: ${REPO_DIR}
origin: ${GIT_ORIGIN}
managed_services: klipper
EOF
    printf "[INSTALL] Update manager block added!\n\n"
}

function install_macros {
    local macros_src="${REPO_DIR}/tmc_flow_test_macros.cfg"
    local macros_dst="${USER_CONFIG_PATH}/tmc_flow_test_macros.cfg"

    if [ ! -f "${macros_src}" ]; then
        echo "[INFO] No macros file found in repo (tmc_flow_test_macros.cfg)"
        echo "[INFO] Skipping macros install."
        printf "\n"
        return 0
    fi

    if [ ! -d "${USER_CONFIG_PATH}" ]; then
        echo "[INFO] Klipper config directory ${USER_CONFIG_PATH} not found."
        echo "[INFO] Skipping macros install."
        printf "\n"
        return 0
    fi

    if [ -f "${macros_dst}" ]; then
        # Don't overwrite — user may have customized.
        # Compare hashes to tell user if there's a newer version.
        local src_hash dst_hash
        src_hash=$(md5sum "${macros_src}" | awk '{print $1}')
        dst_hash=$(md5sum "${macros_dst}" | awk '{print $1}')
        if [ "${src_hash}" = "${dst_hash}" ]; then
            printf "[INSTALL] Macros file already up-to-date.\n\n"
        else
            echo "[INSTALL] Macros file already exists at"
            echo "          ${macros_dst}"
            echo "          but differs from the repo version."
            echo "          NOT overwriting (you may have customized it)."
            echo "          To force-update, delete the file and re-run install.sh."
            printf "\n"
        fi
        return 0
    fi

    echo "[INSTALL] Copying macros file to printer config directory..."
    cp "${macros_src}" "${macros_dst}"
    printf "[INSTALL] Macros installed at ${macros_dst}\n\n"
}

function add_include_to_printer_cfg {
    local printer_cfg="${USER_CONFIG_PATH}/printer.cfg"
    local include_line="[include tmc_flow_test_macros.cfg]"

    if [ ! -f "${printer_cfg}" ]; then
        echo "[INFO] printer.cfg not found at ${printer_cfg}"
        echo "[INFO] Skipping printer.cfg edit."
        printf "\n"
        return 0
    fi

    # Already present? skip.
    if grep -qF "${include_line}" "${printer_cfg}"; then
        printf "[INSTALL] [include tmc_flow_test_macros.cfg] already in printer.cfg\n\n"
        return 0
    fi

    # Backup before edit
    local backup="${printer_cfg}.bak.$(date +%Y%m%d-%H%M%S)"
    cp "${printer_cfg}" "${backup}"
    echo "[INSTALL] Backing up printer.cfg to ${backup}"

    # Prepend the include directive at the top of the file.
    # We insert as the first line so the macros are included before
    # any other section. Klipper accepts include anywhere but "at top"
    # is the convention.
    local tmp="${printer_cfg}.tmp.$$"
    {
        echo "${include_line}"
        cat "${printer_cfg}"
    } > "${tmp}"
    mv "${tmp}" "${printer_cfg}"
    printf "[INSTALL] Added [include tmc_flow_test_macros.cfg] to top of printer.cfg\n\n"
}

function restart_klipper {
    echo "[POST-INSTALL] Restarting Klipper..."
    sudo systemctl restart klipper
}

function restart_moonraker {
    if [ ! -f "${MOONRAKER_CONFIG}" ]; then
        return 0
    fi
    echo "[POST-INSTALL] Restarting Moonraker..."
    sudo systemctl restart moonraker
}


printf "\n=============================================\n"
echo "- Crydteam TMC Flow Test install script     -"
printf "=============================================\n\n"


# Run steps
preflight_checks
validate_plugin_syntax
link_module
clear_pycache
install_macros
add_include_to_printer_cfg
add_updater
restart_klipper
restart_moonraker

printf "\n[DONE] Installation complete.\n"
echo "  Verify in your printer console:  TMC_FLOW_STATUS"
printf "\n"
