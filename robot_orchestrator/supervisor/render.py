import re
from typing import Any

_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z0-9_.]+)\}")


def render_string(template: str, values: dict[str, Any]) -> Any:
    full_match = _PLACEHOLDER_RE.fullmatch(template)
    if full_match:
        key = full_match.group(1)
        if key in values:
            return values[key]
        return template

    def _sub(m: re.Match) -> str:
        key = m.group(1)
        return str(values.get(key, m.group(0)))

    return _PLACEHOLDER_RE.sub(_sub, template)


def render(value: Any, values: dict[str, Any]) -> Any:
    if isinstance(value, str):
        return render_string(value, values)
    if isinstance(value, dict):
        return {k: render(v, values) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, values) for v in value]
    return value
