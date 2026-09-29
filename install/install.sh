#!/usr/bin/env bash
set -euo pipefail

SYSTEM_BOOTSTRAP_VERSION=1
INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_DIR="/opt/robot-orchestrator"
STATE_DIR="/var/lib/robot-orchestrator"
LOG_DIR="/var/log/robot-orchestrator"
RUN_DIR="/run/robot-orchestrator"
ETC_DIR="/etc/robot-orchestrator"
STAMP_FILE="${STATE_DIR}/bootstrap/system.json"
SERVICE_USER="robot"

if [ "$(id -u)" -ne 0 ]; then
    echo "must be run as root (sudo bash install/install.sh)" >&2
    exit 1
fi

echo "==> checking platform"
if [ "$(uname -m)" != "aarch64" ]; then
    echo "warning: expected aarch64, found $(uname -m) -- continuing anyway" >&2
fi
if ! command -v apt-get >/dev/null 2>&1; then
    echo "apt-get not found -- this script targets Debian/Ubuntu-based images" >&2
    exit 1
fi

PYTHON_BIN="$(command -v python3)"
PYTHON_VERSION="$("${PYTHON_BIN}" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
echo "==> found python ${PYTHON_VERSION} at ${PYTHON_BIN}"
if awk -v v="${PYTHON_VERSION}" 'BEGIN { split(v, a, "."); exit !(a[1] > 3 || (a[1] == 3 && a[2] >= 12)) }'; then
    echo "note: python ${PYTHON_VERSION} detected -- numpy==1.23.5-style pins in izbushka-web-core/izbushka-voice-interface have no wheel for 3.12+; the orchestrator-integration branch already relaxes this to numpy>=1.26.4,<2, but verify with 'robot-orchestrator doctor' after first bootstrap"
fi

echo "==> installing apt packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update
xargs -a "${INSTALL_DIR}/install/apt-packages.txt" apt-get install -y --no-upgrade

if ! dpkg -s chromium >/dev/null 2>&1 && ! dpkg -s chromium-browser >/dev/null 2>&1; then
    if snap list chromium >/dev/null 2>&1; then
        echo "only the chromium snap is available -- the kiosk service needs a non-snap chromium binary; install one manually (e.g. from the board vendor's apt repo) and re-run" >&2
        exit 1
    fi
    apt-get install -y chromium || apt-get install -y chromium-browser
fi

echo "==> creating service user"
if ! getent group gpio >/dev/null 2>&1; then
    groupadd gpio
fi
if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
    useradd --create-home --shell /bin/bash "${SERVICE_USER}"
fi
usermod -aG video,audio,dialout,plugdev,gpio,render,input "${SERVICE_USER}"

echo "==> installing udev rules"
install -m 0644 "${INSTALL_DIR}/install/udev/99-platformio-udev.rules" /etc/udev/rules.d/99-platformio-udev.rules
install -m 0644 "${INSTALL_DIR}/install/udev/60-robot-gpio.rules" /etc/udev/rules.d/60-robot-gpio.rules
udevadm control --reload
udevadm trigger

echo "==> creating directories"
mkdir -p "${STATE_DIR}/bootstrap" "${STATE_DIR}/secrets" "${STATE_DIR}/repos" "${STATE_DIR}/venvs" \
    "${STATE_DIR}/data" "${STATE_DIR}/firmware" "${STATE_DIR}/pio" "${STATE_DIR}/wheelhouse" \
    "${LOG_DIR}/services" "${RUN_DIR}/overlays" "${ETC_DIR}"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${STATE_DIR}" "${LOG_DIR}" "${RUN_DIR}"
chmod 0700 "${STATE_DIR}/secrets"
chown "root:${SERVICE_USER}" "${ETC_DIR}"
chmod 0750 "${ETC_DIR}"
if [ ! -f "${ETC_DIR}/settings.local.yml" ]; then
    install -m 0640 -o root -g "${SERVICE_USER}" "${INSTALL_DIR}/install/settings.local.yml.template" "${ETC_DIR}/settings.local.yml"
fi

if [ "${INSTALL_DIR}" != "${TARGET_DIR}" ]; then
    echo "==> copying repository to ${TARGET_DIR}"
    mkdir -p "${TARGET_DIR}"
    rsync -a --delete --exclude ".venv" --exclude ".git" "${INSTALL_DIR}/" "${TARGET_DIR}/"
fi
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${TARGET_DIR}"

echo "==> building orchestrator venv"
if [ ! -d "${TARGET_DIR}/.venv" ]; then
    sudo -u "${SERVICE_USER}" "${PYTHON_BIN}" -m venv "${TARGET_DIR}/.venv"
fi
sudo -u "${SERVICE_USER}" "${TARGET_DIR}/.venv/bin/pip" install --upgrade pip
sudo -u "${SERVICE_USER}" "${TARGET_DIR}/.venv/bin/pip" install -r "${TARGET_DIR}/requirements.txt"
sudo -u "${SERVICE_USER}" "${TARGET_DIR}/.venv/bin/pip" install -e "${TARGET_DIR}"

echo "==> installing self-update launcher"
install -m 0755 "${INSTALL_DIR}/install/run.sh" "${TARGET_DIR}/run.sh"

echo "==> installing systemd units"
install -m 0644 "${INSTALL_DIR}/install/systemd/robot-orchestrator.service" /etc/systemd/system/robot-orchestrator.service
mkdir -p "/etc/systemd/system/getty@tty1.service.d"
install -m 0644 "${INSTALL_DIR}/install/systemd/getty@tty1.service.d/autologin.conf" \
    "/etc/systemd/system/getty@tty1.service.d/autologin.conf"
systemctl daemon-reload
systemctl enable robot-orchestrator.service
systemctl set-default multi-user.target

echo "==> installing X session files"
install -m 0755 -o "${SERVICE_USER}" -g "${SERVICE_USER}" "${INSTALL_DIR}/install/x11/xinitrc" \
    "/home/${SERVICE_USER}/.xinitrc"
if ! grep -qF "startx -- -nocursor" "/home/${SERVICE_USER}/.bash_profile" 2>/dev/null; then
    cat "${INSTALL_DIR}/install/x11/bash_profile.snippet" >> "/home/${SERVICE_USER}/.bash_profile"
fi
chown "${SERVICE_USER}:${SERVICE_USER}" "/home/${SERVICE_USER}/.bash_profile"

mkdir -p "/home/${SERVICE_USER}/.config/openbox"
OPENBOX_RC="/home/${SERVICE_USER}/.config/openbox/rc.xml"
if [ ! -f "${OPENBOX_RC}" ]; then
    cp /etc/xdg/openbox/rc.xml "${OPENBOX_RC}"
    python3 - "${OPENBOX_RC}" <<'PYEOF'
import sys
import xml.etree.ElementTree as ET

path = sys.argv[1]
ns = "http://openbox.org/3.4/rc"
ET.register_namespace("", ns)
tree = ET.parse(path)
root = tree.getroot()
keyboard = root.find(f"{{{ns}}}keyboard")
if keyboard is not None and keyboard.find(f"{{{ns}}}keybind[@key='C-A-t']") is None:
    keybind = ET.SubElement(keyboard, f"{{{ns}}}keybind", {"key": "C-A-t"})
    action = ET.SubElement(keybind, f"{{{ns}}}action", {"name": "Execute"})
    command = ET.SubElement(action, f"{{{ns}}}command")
    command.text = "xterm"
    tree.write(path, xml_declaration=True, encoding="UTF-8")
PYEOF
fi
chown -R "${SERVICE_USER}:${SERVICE_USER}" "/home/${SERVICE_USER}/.config"

echo "==> disabling screen blanking, power management and sleep"
mkdir -p /etc/X11/xorg.conf.d
install -m 0644 "${INSTALL_DIR}/install/xorg/10-robot-noblank.conf" /etc/X11/xorg.conf.d/10-robot-noblank.conf
mkdir -p /etc/systemd/logind.conf.d
install -m 0644 "${INSTALL_DIR}/install/systemd/logind-robot-no-idle.conf" /etc/systemd/logind.conf.d/robot-no-idle.conf
systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
mkdir -p "/home/${SERVICE_USER}/.config/autostart"
for entry in xfce4-power-manager xscreensaver xfce4-screensaver light-locker gnome-screensaver; do
    printf '[Desktop Entry]\nType=Application\nName=%s (disabled)\nHidden=true\n' "${entry}" \
        > "/home/${SERVICE_USER}/.config/autostart/${entry}.desktop"
done
chown -R "${SERVICE_USER}:${SERVICE_USER}" "/home/${SERVICE_USER}/.config/autostart"

echo "==> installing polkit rule for Wi-Fi"
if [ -d /etc/polkit-1/rules.d ]; then
    install -m 0644 "${INSTALL_DIR}/install/polkit/50-robot-nm.rules" /etc/polkit-1/rules.d/50-robot-nm.rules
elif [ -d /etc/polkit-1/localauthority/50-local.d ]; then
    install -m 0644 "${INSTALL_DIR}/install/polkit/50-robot-nm.pkla" /etc/polkit-1/localauthority/50-local.d/50-robot-nm.pkla
else
    echo "warning: neither /etc/polkit-1/rules.d nor /etc/polkit-1/localauthority/50-local.d exists -- skipping polkit rule, Wi-Fi control may prompt for auth" >&2
fi

echo "==> configuring ALSA"
USB_CARD="$(arecord -l 2>/dev/null | grep -i usb | head -1 | sed -n 's/^card \([0-9]*\).*/\1/p')"
if [ -n "${USB_CARD}" ]; then
    cat > /etc/asound.conf <<EOF
pcm.!default {
    type asym
    playback.pcm "dmixer"
    capture.pcm "dsnooper"
}
pcm.dmixer {
    type dmix
    ipc_key 1024
    slave { pcm "hw:${USB_CARD},0" }
}
pcm.dsnooper {
    type dsnoop
    ipc_key 1025
    slave { pcm "hw:${USB_CARD},0" }
}
EOF
else
    echo "no USB audio card detected yet -- skipping /etc/asound.conf, re-run install.sh once the microphone is connected"
fi

echo "==> writing bootstrap stamp"
python3 -c "
import json, time
json.dump({'version': ${SYSTEM_BOOTSTRAP_VERSION}, 'completed_at': time.time()}, open('${STAMP_FILE}', 'w'))
"
chown "${SERVICE_USER}:${SERVICE_USER}" "${STAMP_FILE}"

echo "==> done. Reboot, then check: systemctl status robot-orchestrator"
