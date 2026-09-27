from pathlib import Path

import yaml

from robot_orchestrator.supervisor.render import render
from robot_orchestrator.wal.atomic import atomic_write


def write_overlay(path: Path, overlay_template: dict, values: dict) -> None:
    rendered = render(overlay_template, values)
    atomic_write(path, yaml.safe_dump(rendered, sort_keys=False).encode("utf-8"))
