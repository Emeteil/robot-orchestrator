import argparse
import base64
import sys
from datetime import datetime, timezone
from pathlib import Path

import qrcode
import yaml
from qrcode.constants import ERROR_CORRECT_H, ERROR_CORRECT_L, ERROR_CORRECT_M, ERROR_CORRECT_Q

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from robot_orchestrator.secrets.protocol import SecretsPayload, encode_payload  # noqa: E402

ECC_MAP = {
    "L": ERROR_CORRECT_L,
    "M": ERROR_CORRECT_M,
    "Q": ERROR_CORRECT_Q,
    "H": ERROR_CORRECT_H,
}


def _load_payload(path: Path) -> SecretsPayload:
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    raw["v"] = 1
    raw["issued_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return SecretsPayload.model_validate(raw)


def _png_data_uri(png_bytes: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png_bytes).decode()


def _build_sheet_html(images: list[tuple[int, int, Path]]) -> str:
    parts = [
        "<!doctype html>",
        '<html><head><meta charset="utf-8">',
        "<title>Secrets QR sheet</title></head><body>",
        '<h1 style="text-align: center;">Secrets QR sheet</h1>',
        '<p style="text-align: center;">Contains secrets — destroy after use.</p>',
    ]
    for k, n, png_path in images:
        data_uri = _png_data_uri(png_path.read_bytes())
        parts.append(
            f'<div style="margin-bottom:2em;text-align: center;">'
            f'<img src="{data_uri}" width="400" height="400">'
            f"<div>Part {k} of {n}</div></div>"
        )
    parts.append("</body></html>")
    return "\n".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--max-bytes", type=int, default=400)
    parser.add_argument("--ecc", choices=list(ECC_MAP.keys()), default="M")
    args = parser.parse_args()

    try:
        payload = _load_payload(args.input)
    except Exception as e:
        print(f"invalid secrets file: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        text_parts = encode_payload(payload, max_bytes=args.max_bytes)
    except ValueError as e:
        print(f"cannot encode payload: {e}", file=sys.stderr)
        sys.exit(1)

    args.out.mkdir(parents=True, exist_ok=True)

    n = len(text_parts)
    images: list[tuple[int, int, Path]] = []
    total_encoded_bytes = 0
    for k, text in enumerate(text_parts, start=1):
        total_encoded_bytes += len(text)
        img = qrcode.make(text, error_correction=ECC_MAP[args.ecc])
        png_path = args.out / f"part-{k}-of-{n}.png"
        img.save(png_path)
        images.append((k, n, png_path))

    sheet_html = _build_sheet_html(images)
    (args.out / "sheet.html").write_text(sheet_html, encoding="utf-8")

    print(f"parts: {n}")
    print(f"total encoded size: {total_encoded_bytes} bytes")
    print(f"output directory: {args.out}")


if __name__ == "__main__":
    main()
