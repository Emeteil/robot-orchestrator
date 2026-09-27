import sys
import types

from robot_orchestrator.config import MicrophoneConfig
from robot_orchestrator.hal.real.microphone import RealMicProbe


class _FakeSamples(list):
    def astype(self, dtype):
        return self

    def __pow__(self, power):
        return [value**power for value in self]


def _fake_numpy():
    return types.SimpleNamespace(
        mean=lambda values: sum(values) / len(values),
        sqrt=lambda value: value**0.5,
        float64=float,
    )


def _install_fake_audio_stack(monkeypatch, devices: list[dict], recorded_samples: list, rec_calls: list):
    fake_numpy = _fake_numpy()

    def query_devices():
        return devices

    def rec(frames, samplerate, channels, device, dtype):
        rec_calls.append((frames, samplerate, channels, device, dtype))
        return _FakeSamples(recorded_samples)

    fake_sounddevice = types.SimpleNamespace(query_devices=query_devices, rec=rec, wait=lambda: None)

    monkeypatch.setitem(sys.modules, "numpy", fake_numpy)
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sounddevice)


def test_probe_returns_not_ok_when_no_matching_device(monkeypatch):
    rec_calls: list = []
    _install_fake_audio_stack(
        monkeypatch,
        devices=[{"name": "Built-in Mic", "max_input_channels": 2, "default_samplerate": 44100}],
        recorded_samples=[],
        rec_calls=rec_calls,
    )

    probe = RealMicProbe(MicrophoneConfig(alsa_card_match="USB"))
    result = probe.probe()

    assert result.ok is False
    assert "no matching" in result.detail
    assert rec_calls == []


def test_probe_ignores_output_only_devices_with_matching_name(monkeypatch):
    rec_calls: list = []
    _install_fake_audio_stack(
        monkeypatch,
        devices=[{"name": "USB Speakers", "max_input_channels": 0, "default_samplerate": 44100}],
        recorded_samples=[],
        rec_calls=rec_calls,
    )

    probe = RealMicProbe(MicrophoneConfig(alsa_card_match="USB"))
    result = probe.probe()

    assert result.ok is False
    assert rec_calls == []


def test_probe_ok_when_measured_rms_meets_threshold(monkeypatch):
    rec_calls: list = []
    _install_fake_audio_stack(
        monkeypatch,
        devices=[{"name": "USB Microphone", "max_input_channels": 1, "default_samplerate": 48000}],
        recorded_samples=[4.0, 4.0, 4.0, 4.0],
        rec_calls=rec_calls,
    )

    probe = RealMicProbe(MicrophoneConfig(alsa_card_match="usb", min_rms=2.0, probe_seconds=1.0))
    result = probe.probe()

    assert result.ok is True
    assert rec_calls[0][1] == 48000


def test_probe_not_ok_when_measured_rms_below_threshold(monkeypatch):
    rec_calls: list = []
    _install_fake_audio_stack(
        monkeypatch,
        devices=[{"name": "USB Microphone", "max_input_channels": 1, "default_samplerate": 48000}],
        recorded_samples=[0.1, 0.1, 0.1, 0.1],
        rec_calls=rec_calls,
    )

    probe = RealMicProbe(MicrophoneConfig(alsa_card_match="usb", min_rms=2.0))
    result = probe.probe()

    assert result.ok is False


def test_probe_never_raises_on_unexpected_backend_error(monkeypatch):
    def raising_query_devices():
        raise OSError("no audio backend")

    fake_sounddevice = types.SimpleNamespace(query_devices=raising_query_devices, rec=None, wait=None)
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sounddevice)
    monkeypatch.setitem(sys.modules, "numpy", _fake_numpy())

    probe = RealMicProbe(MicrophoneConfig())
    result = probe.probe()

    assert result.ok is False
    assert "no audio backend" in result.detail
