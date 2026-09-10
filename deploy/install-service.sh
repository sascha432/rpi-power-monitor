#!/usr/bin/env bash
# =============================================================================
# rpi-power-monitor — install / manage the systemd service (run ON the Pi)
#
# Usage:
#   sudo ./deploy/install-service.sh              # install + enable + start server
#   sudo ./deploy/install-service.sh --client     # install + enable + start client
#   sudo ./deploy/install-service.sh --no-start   # install + enable only
#   sudo ./deploy/install-service.sh --no-enable  # install + start only
#   sudo ./deploy/install-service.sh --uninstall  # stop, disable, remove server
#   sudo ./deploy/install-service.sh --client --uninstall  # remove client service
#
# Options:
#   --server          install the server service (default)
#   --client          install the client service
#   --user NAME       run the service as NAME     (default: repo owner)
#   --group NAME      run the service as GROUP    (default: repo owner group)
#   --python PATH     venv python for ExecStart   (default: auto-detected)
#   -h, --help        show this help
# =============================================================================
set -euo pipefail

TARGET="server"
SERVICE_NAME="rpi-power-monitor.service"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
SRC_UNIT=""
DEST_UNIT=""

DO_ENABLE=1
DO_START=1
DO_UNINSTALL=0
SERVICE_USER=""
SERVICE_GROUP=""
SERVICE_PYTHON=""

usage() {
    sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --server)     TARGET="server" ;;
        --client)     TARGET="client" ;;
        --no-start)   DO_START=0 ;;
        --no-enable)  DO_ENABLE=0 ;;
        --uninstall)  DO_UNINSTALL=1 ;;
        --user)       SERVICE_USER="$2"; shift ;;
        --group)      SERVICE_GROUP="$2"; shift ;;
        --python)     SERVICE_PYTHON="$2"; shift ;;
        -h|--help)    usage; exit 0 ;;
        *) echo "Unknown option: $1"; usage; exit 2 ;;
    esac
    shift
done

case "${TARGET}" in
    server)
        SERVICE_NAME="rpi-power-monitor.service"
        ;;
    client)
        SERVICE_NAME="rpi-power-monitor-client.service"
        ;;
    *)
        echo "ERROR: unknown target '${TARGET}'." >&2
        exit 2
        ;;
esac

SRC_UNIT="${SCRIPT_DIR}/${SERVICE_NAME}"
DEST_UNIT="/etc/systemd/system/${SERVICE_NAME}"

# -- auto-detect defaults ---------------------------------------------------

# Prefer the project virtualenv; fall back to a plain python3.
if [ -z "${SERVICE_PYTHON}" ]; then
    for candidate in \
        "${REPO_DIR}/.venv/bin/python" \
        "${REPO_DIR}/.venv_acidpi4/bin/python"
    do
        if [ -x "${candidate}" ]; then
            SERVICE_PYTHON="${candidate}"
            break
        fi
    done
fi
if [ -z "${SERVICE_PYTHON}" ]; then
    SERVICE_PYTHON="$(command -v python3 || true)"
fi
if [ -z "${SERVICE_PYTHON}" ] || [ ! -x "${SERVICE_PYTHON}" ]; then
    echo "ERROR: no usable python found. Pass one with --python <path>." >&2
    exit 1
fi

# Run as the repo owner so state/energy.json stays writable by the service.
if [ -z "${SERVICE_USER}" ]; then
    SERVICE_USER="$(stat -c '%U' "${REPO_DIR}" 2>/dev/null || echo root)"
fi
if [ -z "${SERVICE_GROUP}" ]; then
    SERVICE_GROUP="$(stat -c '%G' "${REPO_DIR}" 2>/dev/null || echo root)"
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: install needs root. Re-run with: sudo $0" >&2
    exit 1
fi

# -- uninstall --------------------------------------------------------------
if [ "${DO_UNINSTALL}" -eq 1 ]; then
    systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
    systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
    rm -f "${DEST_UNIT}"
    systemctl daemon-reload
    echo "Removed ${DEST_UNIT} (stopped + disabled)."
    exit 0
fi

# -- install ----------------------------------------------------------------
if [ ! -f "${SRC_UNIT}" ]; then
    echo "ERROR: ${SRC_UNIT} not found." >&2
    exit 1
fi

echo "Service : ${SERVICE_NAME}"
echo "Repo    : ${REPO_DIR}"
echo "User    : ${SERVICE_USER}  Group: ${SERVICE_GROUP}"
echo "Python  : ${SERVICE_PYTHON}"
echo

# Fill the __TOKEN__ placeholders (paths contain '/', so use '|' as sed delim).
sed \
    -e "s|__USER__|${SERVICE_USER}|g" \
    -e "s|__GROUP__|${SERVICE_GROUP}|g" \
    -e "s|__REPO_DIR__|${REPO_DIR}|g" \
    -e "s|__PYTHON__|${SERVICE_PYTHON}|g" \
    "${SRC_UNIT}" > "${DEST_UNIT}"

systemctl daemon-reload

if [ "${DO_ENABLE}" -eq 1 ]; then
    systemctl enable "${SERVICE_NAME}"
fi
if [ "${DO_START}" -eq 1 ]; then
    systemctl start "${SERVICE_NAME}"
fi

echo
echo "Installed to ${DEST_UNIT}."
echo "Next steps:"
echo "  systemctl status ${SERVICE_NAME}"
echo "  journalctl -u ${SERVICE_NAME} -f"
echo

if [ "${TARGET}" = "server" ]; then
    echo "Note: the service user needs to be a member of the 'i2c' group to open"
    echo "the INA3221 bus. If startup fails with a sensor/I2C error, run:"
    echo "  sudo usermod -aG i2c ${SERVICE_USER}   # then reboot"
fi
