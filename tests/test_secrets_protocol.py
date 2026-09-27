import random
import string

import pytest

from robot_orchestrator.secrets.protocol import (
    PartialAssembly,
    SecretsPayload,
    WifiCredential,
    encode_payload,
)


def _sample_payload(**overrides) -> SecretsPayload:
    data = {
        "v": 1,
        "issued_at": "2026-09-27T12:00:00Z",
        "secrets": {"GEMINI_API_KEY": "abc123", "GITHUB_PAT": "github_pat_xyz"},
    }
    data.update(overrides)
    return SecretsPayload.model_validate(data)


def test_single_part_roundtrip():
    payload = _sample_payload()

    parts = encode_payload(payload, max_bytes=4000)
    assert len(parts) == 1

    assembly = PartialAssembly()
    result = assembly.add_part(parts[0])

    assert result == payload


def test_multi_part_roundtrip_reverse_order():
    payload = _sample_payload(
        secrets={"GEMINI_API_KEY": "x" * 500, "GITHUB_PAT": "y" * 500},
        wifi=[WifiCredential(ssid="home", psk="hunter2")],
    )

    parts = encode_payload(payload, max_bytes=20)
    assert len(parts) >= 3

    assembly = PartialAssembly()
    result = None
    for text in reversed(parts):
        result = assembly.add_part(text)

    assert result == payload


def test_multi_part_roundtrip_shuffled_order():
    payload = _sample_payload(
        secrets={"GEMINI_API_KEY": "x" * 500, "GITHUB_PAT": "y" * 500},
        config={"gpio.chip": "/dev/gpiochip1", "gpio.line": 11},
    )

    parts = encode_payload(payload, max_bytes=20)
    assert len(parts) >= 3

    shuffled = list(parts)
    random.Random(42).shuffle(shuffled)

    assembly = PartialAssembly()
    result = None
    for text in shuffled:
        result = assembly.add_part(text)

    assert result == payload


def test_duplicate_parts_are_harmless():
    payload = _sample_payload(secrets={"GEMINI_API_KEY": "x" * 500, "GITHUB_PAT": "y" * 500})
    parts = encode_payload(payload, max_bytes=20)
    assert len(parts) >= 3

    assembly = PartialAssembly()
    assembly.add_part(parts[0])
    assembly.add_part(parts[0])
    result = None
    for text in parts[1:]:
        result = assembly.add_part(text)

    assert result == payload


def test_two_digests_interleaved_do_not_mix():
    payload_a = _sample_payload(secrets={"GEMINI_API_KEY": "a" * 500, "GITHUB_PAT": "a" * 500})
    payload_b = _sample_payload(secrets={"GEMINI_API_KEY": "b" * 500, "GITHUB_PAT": "b" * 500})

    parts_a = encode_payload(payload_a, max_bytes=20)
    parts_b = encode_payload(payload_b, max_bytes=20)
    assert len(parts_a) >= 3
    assert len(parts_b) >= 3

    assembly = PartialAssembly()
    result_a = None
    result_b = None
    max_len = max(len(parts_a), len(parts_b))
    for i in range(max_len):
        if i < len(parts_a):
            res = assembly.add_part(parts_a[i])
            if res is not None:
                result_a = res
        if i < len(parts_b):
            res = assembly.add_part(parts_b[i])
            if res is not None:
                result_b = res

    assert result_a == payload_a
    assert result_b == payload_b


@pytest.mark.parametrize(
    "garbage",
    [
        "not even close",
        "RO1:",
        "RO1:1",
        "RO1:1:2",
        "RO1:one:2:0123456789ABCDEF:XYZ",
        "RO1:1:two:0123456789ABCDEF:XYZ",
        "RO1:1:2:ZZ:XYZ",
    ],
)
def test_garbage_input_returns_none_and_does_not_corrupt_other_assembly(garbage):
    payload = _sample_payload(secrets={"GEMINI_API_KEY": "x" * 500, "GITHUB_PAT": "y" * 500})
    parts = encode_payload(payload, max_bytes=20)
    assert len(parts) >= 3

    assembly = PartialAssembly()
    assembly.add_part(parts[0])
    digest = _digest_of(parts[0])
    progress_before = assembly.progress(digest)

    result = assembly.add_part(garbage)
    assert result is None
    assert assembly.progress(digest) == progress_before

    final = None
    for text in parts[1:]:
        final = assembly.add_part(text)
    assert final == payload


def _digest_of(part_text: str) -> str:
    return part_text.split(":")[3]


def test_corrupted_chunk_fails_digest_check_and_discards():
    payload = _sample_payload(secrets={"GEMINI_API_KEY": "x" * 500, "GITHUB_PAT": "y" * 500})
    parts = encode_payload(payload, max_bytes=20)
    assert len(parts) >= 3

    prefix, k, n, digest, b45_chunk = parts[0].split(":", 4)
    chars = list(b45_chunk)
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ $%*+-./:"
    original_char = chars[0]
    replacement = next(c for c in alphabet if c != original_char)
    chars[0] = replacement
    corrupted = f"{prefix}:{k}:{n}:{digest}:{''.join(chars)}"

    assembly = PartialAssembly()
    result = None
    for text in [corrupted, *parts[1:]]:
        result = assembly.add_part(text)

    assert result is None
    assert assembly.digests() == []


def _random_string(seed: int, length: int) -> str:
    rng = random.Random(seed)
    alphabet = string.ascii_letters + string.digits
    return "".join(rng.choices(alphabet, k=length))


def test_encode_payload_raises_when_exceeding_max_parts():
    payload = _sample_payload(
        secrets={"GEMINI_API_KEY": _random_string(10, 6000), "GITHUB_PAT": _random_string(11, 6000)}
    )

    with pytest.raises(ValueError):
        encode_payload(payload, max_bytes=10, max_parts=16)


def test_add_part_does_not_crash_when_same_digest_reused_with_shrinking_total():
    assembly = PartialAssembly()

    assert assembly.add_part("RO1:1:5:AAAAAAAAAAAAAAAA:AB") is None
    assert assembly.add_part("RO1:5:5:AAAAAAAAAAAAAAAA:CD") is None
    result = assembly.add_part("RO1:1:2:AAAAAAAAAAAAAAAA:EF")

    assert result is None
