import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
QR_ENCODE = REPO_ROOT / "tools" / "qr_encode.py"


def test_qr_encode_cli_produces_pngs_and_sheet(tmp_path: Path):
    input_yaml = tmp_path / "secrets.yml"
    input_yaml.write_text(
        yaml.safe_dump(
            {
                "secrets": {"GEMINI_API_KEY": "abc123", "GITHUB_PAT": "github_pat_xyz"},
                "wifi": [{"ssid": "home", "psk": "hunter2"}],
                "config": {"gpio.chip": "/dev/gpiochip1", "gpio.line": 11},
            }
        ),
        encoding="utf-8",
    )
    out_dir = tmp_path / "sheet"

    result = subprocess.run(
        [sys.executable, str(QR_ENCODE), str(input_yaml), "--out", str(out_dir)],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr

    png_files = sorted(out_dir.glob("part-*-of-*.png"))
    assert len(png_files) >= 1
    for png_file in png_files:
        assert png_file.stat().st_size > 100

    sheet_html = out_dir / "sheet.html"
    assert sheet_html.exists()
    html_text = sheet_html.read_text(encoding="utf-8")
    assert "destroy after use" in html_text
    for k in range(1, len(png_files) + 1):
        assert f"Part {k} of {len(png_files)}" in html_text


def test_qr_encode_cli_rejects_invalid_payload(tmp_path: Path):
    input_yaml = tmp_path / "secrets.yml"
    input_yaml.write_text(yaml.safe_dump({"secrets": "not-a-dict"}), encoding="utf-8")
    out_dir = tmp_path / "sheet"

    result = subprocess.run(
        [sys.executable, str(QR_ENCODE), str(input_yaml), "--out", str(out_dir)],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert not out_dir.exists() or list(out_dir.iterdir()) == []
