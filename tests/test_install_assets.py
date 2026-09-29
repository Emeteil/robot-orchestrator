from pathlib import Path

INSTALL_DIR = Path(__file__).resolve().parent.parent / "install"


def _read(relative: str) -> str:
    return (INSTALL_DIR / relative).read_text(encoding="utf-8")


def test_xinitrc_keeps_the_display_awake_for_as_long_as_x_lives():
    xinitrc = _read("x11/xinitrc")

    assert "xset -dpms" in xinitrc
    assert "xset s off" in xinitrc
    assert "xset s noblank" in xinitrc
    assert "while xset q" in xinitrc, "the keep-awake loop must stop once the X server is gone"
    assert xinitrc.index("keep_awake &") < xinitrc.index("exec openbox-session")


def test_xorg_server_defaults_never_blank_or_power_down():
    conf = _read("xorg/10-robot-noblank.conf")

    for option in ("BlankTime", "StandbyTime", "SuspendTime", "OffTime"):
        assert f'"{option}"' in conf
    assert conf.count('"0"') == 4


def test_logind_never_acts_on_idle():
    assert "IdleAction=ignore" in _read("systemd/logind-robot-no-idle.conf")


def test_install_script_trusts_the_checkout_so_self_update_can_read_its_head():
    script = _read("install.sh")

    assert 'git config --system --add safe.directory "${TARGET_DIR}"' in script


def test_install_script_wires_up_every_no_sleep_measure():
    script = _read("install.sh")

    assert "10-robot-noblank.conf" in script
    assert "logind-robot-no-idle.conf" in script
    assert "systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target" in script
    assert "xfce4-power-manager" in script and "Hidden=true" in script
