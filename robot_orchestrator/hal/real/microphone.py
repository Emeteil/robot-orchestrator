import time

from robot_orchestrator.config import MicrophoneConfig
from robot_orchestrator.hal.base import ProbeResult

FALLBACK_SAMPLERATES = [48000, 44100, 32000, 22050, 16000, 8000]
RETRY_ATTEMPTS = 12
RETRY_DELAY_S = 2.0


class RealMicProbe:
    def __init__(self, config: MicrophoneConfig):
        self.config = config

    def probe(self) -> ProbeResult:
        result = self._probe_once()
        for _ in range(RETRY_ATTEMPTS - 1):
            if result.ok:
                return result
            time.sleep(RETRY_DELAY_S)
            result = self._probe_once()
        return result

    def _probe_once(self) -> ProbeResult:
        try:
            import numpy as np
            import sounddevice as sd

            match = self.config.alsa_card_match.lower()
            device_index = None
            reported_samplerate = None
            for index, device in enumerate(sd.query_devices()):
                if device.get("max_input_channels", 0) <= 0:
                    continue
                if match in device.get("name", "").lower():
                    device_index = index
                    reported_samplerate = int(device.get("default_samplerate") or 44100)
                    break

            if device_index is None:
                return ProbeResult(ok=False, detail="no matching input device")

            # some USB UAC1 codecs advertise a default_samplerate that ALSA's own
            # hw_params range accepts but that PortAudio's stream open then rejects;
            # the advertised rate is tried first, then a list of common rates.
            candidate_rates = [reported_samplerate] + [
                r for r in FALLBACK_SAMPLERATES if r != reported_samplerate
            ]

            recording = None
            samplerate = None
            last_error: Exception | None = None
            for rate in candidate_rates:
                frames = max(1, int(self.config.probe_seconds * rate))
                try:
                    recording = sd.rec(frames, samplerate=rate, channels=1, device=device_index, dtype="int16")
                    sd.wait()
                    samplerate = rate
                    break
                except Exception as e:
                    last_error = e
                    continue

            if recording is None:
                return ProbeResult(ok=False, detail=f"no working sample rate: {last_error}")

            rms = float(np.sqrt(np.mean(recording.astype(np.float64) ** 2)))
            if rms >= self.config.min_rms:
                return ProbeResult(ok=True, detail=f"microphone usable at {samplerate}Hz, rms={rms:.4f}")
            return ProbeResult(ok=False, detail=f"microphone rms={rms:.4f} below min_rms={self.config.min_rms}")
        except Exception as e:
            return ProbeResult(ok=False, detail=str(e))
