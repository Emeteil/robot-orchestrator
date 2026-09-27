from dataclasses import dataclass

from robot_orchestrator.boot.context import BootFacts

VOICE_INTERFACE_KILL_REASONS = ("mic_unavailable", "internet_down", "secrets_missing:GEMINI_API_KEY")


@dataclass
class ModeDecision:
    production: bool
    reasons: list[str]
    capabilities: dict[str, bool]


def decide_mode(facts: BootFacts) -> ModeDecision:
    reasons = facts.reasons()
    production = len(reasons) == 0
    capabilities = {
        "kiosk_browser": production,
        "wake_word": production,
        "voice_interface": not any(r in reasons for r in VOICE_INTERFACE_KILL_REASONS),
    }
    return ModeDecision(production=production, reasons=reasons, capabilities=capabilities)


def capability(decision: ModeDecision, name: str) -> bool:
    return decision.capabilities.get(name, False)
