from robot_orchestrator.config import MicrophoneConfig
from robot_orchestrator.hal.base import ProbeResult


class RealMicProbe:
    def __init__(self, config: MicrophoneConfig):
        self.config = config

    def probe(self) -> ProbeResult:
        try:
            import numpy as np
            import sounddevice as sd

            match = self.config.alsa_card_match.lower()
            device_index = None
            samplerate = None
            for index, device in enumerate(sd.query_devices()):
                if device.get("max_input_channels", 0) <= 0:
                    continue
                if match in device.get("name", "").lower():
                    device_index = index
                    samplerate = int(device.get("default_samplerate") or 44100)
                    break

            if device_index is None:
                return ProbeResult(ok=False, detail="no matching input device")

            frames = max(1, int(self.config.probe_seconds * samplerate))
            recording = sd.rec(frames, samplerate=samplerate, channels=1, device=device_index, dtype="int16")
            sd.wait()

            rms = float(np.sqrt(np.mean(recording.astype(np.float64) ** 2)))
            if rms >= self.config.min_rms:
                return ProbeResult(ok=True, detail=f"microphone usable, rms={rms:.4f}")
            return ProbeResult(ok=False, detail=f"microphone rms={rms:.4f} below min_rms={self.config.min_rms}")
        except Exception as e:
            return ProbeResult(ok=False, detail=str(e))
