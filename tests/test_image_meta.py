from datetime import datetime, timezone
from pathlib import Path

import pytest

from robot_orchestrator.firmware import image_meta


def _bin_with(date_str: str, time_str: str) -> bytes:
    date_field = date_str.encode("ascii").ljust(12, b"\x00")
    time_field = time_str.encode("ascii").ljust(9, b"\x00")
    assert len(date_field) == 12
    assert len(time_field) == 9
    return b"\xde\xad\xbe\xef" * 8 + date_field + b"\x01\x02\x03" + time_field + b"\xff" * 16


def test_extract_build_timestamp_finds_double_digit_day(tmp_path: Path):
    binary = tmp_path / "firmware.bin"
    binary.write_bytes(_bin_with("Sep 27 2026", "18:45:03"))

    result = image_meta.extract_build_timestamp(binary)

    assert result == ("Sep 27 2026", "18:45:03")


def test_extract_build_timestamp_preserves_double_space_for_single_digit_day(tmp_path: Path):
    binary = tmp_path / "firmware.bin"
    binary.write_bytes(_bin_with("Sep  7 2026", "08:05:00"))

    result = image_meta.extract_build_timestamp(binary)

    assert result == ("Sep  7 2026", "08:05:00")


def test_extract_build_timestamp_returns_none_when_no_match(tmp_path: Path):
    binary = tmp_path / "firmware.bin"
    binary.write_bytes(b"\x00" * 64)

    assert image_meta.extract_build_timestamp(binary) is None


def test_extract_build_timestamp_returns_none_on_conflicting_matches(tmp_path: Path):
    binary = tmp_path / "firmware.bin"
    first = _bin_with("Sep 27 2026", "18:45:03")
    second = _bin_with("Jan  1 2020", "00:00:01")
    binary.write_bytes(first + second)

    assert image_meta.extract_build_timestamp(binary) is None


def test_parse_build_timestamp_to_utc_matches_independent_datetime_computation():
    expected = datetime(2026, 9, 27, 18, 45, 3, tzinfo=timezone.utc).timestamp()

    result = image_meta.parse_build_timestamp_to_utc("Sep 27 2026", "18:45:03")

    assert result == expected


def test_parse_build_timestamp_to_utc_handles_double_space_single_digit_day():
    expected = datetime(2026, 9, 7, 8, 5, 0, tzinfo=timezone.utc).timestamp()

    result = image_meta.parse_build_timestamp_to_utc("Sep  7 2026", "08:05:00")

    assert result == expected


def test_parse_build_timestamp_to_utc_raises_value_error_on_garbled_month():
    with pytest.raises(ValueError):
        image_meta.parse_build_timestamp_to_utc("Xxx 27 2026", "18:45:03")
