import hashlib
import json
import re
import zlib
from dataclasses import dataclass, field

import base45
from pydantic import BaseModel, Field

MAX_DECOMPRESSED_BYTES = 64 * 1024
DEFAULT_MAX_PARTS = 16

_PART_RE = re.compile(r"^RO1:(\d+):(\d+):([0-9A-Fa-f]{16}):(.*)$")


class WifiCredential(BaseModel):
    ssid: str
    psk: str


class SecretsPayload(BaseModel):
    v: int
    issued_at: str
    secrets: dict[str, str]
    wifi: list[WifiCredential] = Field(default_factory=list)
    config: dict[str, str | int] = Field(default_factory=dict)


def _canonical_json_bytes(payload: SecretsPayload) -> bytes:
    data = payload.model_dump(mode="json")
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")


def encode_payload(payload: SecretsPayload, max_bytes: int = 400, max_parts: int = DEFAULT_MAX_PARTS) -> list[str]:
    data = _canonical_json_bytes(payload)
    blob = zlib.compress(data, 9)
    digest = hashlib.sha256(blob).hexdigest()[:16].upper()
    chunks = [blob[i:i + max_bytes] for i in range(0, len(blob), max_bytes)] or [b""]
    if len(chunks) > max_parts:
        raise ValueError(f"payload requires {len(chunks)} parts, exceeds max_parts={max_parts}")
    n = len(chunks)
    parts = []
    for k, chunk in enumerate(chunks, start=1):
        b45_chunk = base45.b45encode(chunk).decode()
        parts.append(f"RO1:{k}:{n}:{digest}:{b45_chunk}")
    return parts


def _safe_decompress(blob: bytes, limit: int) -> bytes | None:
    decompressor = zlib.decompressobj()
    try:
        output = decompressor.decompress(blob, limit + 1)
        output += decompressor.flush()
    except zlib.error:
        return None
    if len(output) > limit:
        return None
    return output


@dataclass
class _Assembly:
    total: int
    parts: dict[int, bytes] = field(default_factory=dict)


class PartialAssembly:
    def __init__(self) -> None:
        self._assemblies: dict[str, _Assembly] = {}

    def digests(self) -> list[str]:
        return list(self._assemblies.keys())

    def progress(self, digest: str) -> tuple[int, int]:
        assembly = self._assemblies.get(digest)
        if assembly is None:
            return (0, 0)
        return (len(assembly.parts), assembly.total)

    def add_part(self, text: str) -> SecretsPayload | None:
        match = _PART_RE.match(text)
        if not match:
            return None
        k_str, n_str, digest, b45_chunk = match.groups()
        k = int(k_str)
        n = int(n_str)
        if k < 1 or n < 1 or k > n:
            return None
        digest = digest.upper()
        try:
            chunk = base45.b45decode(b45_chunk)
        except ValueError:
            return None

        assembly = self._assemblies.setdefault(digest, _Assembly(total=n))
        assembly.total = n
        assembly.parts[k] = chunk

        if len(assembly.parts) < assembly.total:
            return None
        if any(i not in assembly.parts for i in range(1, assembly.total + 1)):
            return None

        ordered = b"".join(assembly.parts[i] for i in range(1, assembly.total + 1))
        computed = hashlib.sha256(ordered).hexdigest()[:16].upper()
        if computed != digest:
            del self._assemblies[digest]
            return None

        decompressed = _safe_decompress(ordered, MAX_DECOMPRESSED_BYTES)
        if decompressed is None:
            del self._assemblies[digest]
            return None

        try:
            data = json.loads(decompressed.decode("utf-8"))
            validated = SecretsPayload.model_validate(data)
        except Exception:
            del self._assemblies[digest]
            return None

        del self._assemblies[digest]
        return validated
