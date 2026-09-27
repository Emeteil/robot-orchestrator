import re
from datetime import datetime, timezone
from pathlib import Path

_MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}

_DATE_RE = re.compile(
    rb"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) [ 0-9]\d \d{4})(?=\x00)"
)
_TIME_RE = re.compile(rb"(\d\d:\d\d:\d\d)(?=\x00)")

_DATE_STR_RE = re.compile(r"^([A-Za-z]{3}) ([ 0-9]\d) (\d{4})$")
_TIME_STR_RE = re.compile(r"^(\d\d):(\d\d):(\d\d)$")


def extract_build_timestamp(binary_path: Path) -> tuple[str, str] | None:
    data = binary_path.read_bytes()

    date_matches = _DATE_RE.findall(data)
    if len(date_matches) != 1:
        return None

    time_matches = _TIME_RE.findall(data)
    if len(time_matches) != 1:
        return None

    return date_matches[0].decode("ascii"), time_matches[0].decode("ascii")


def parse_build_timestamp_to_utc(build_date: str, build_time: str, tz: str = "UTC") -> float:
    date_match = _DATE_STR_RE.match(build_date)
    if date_match is None:
        raise ValueError(f"unrecognized build_date format: {build_date!r}")
    time_match = _TIME_STR_RE.match(build_time)
    if time_match is None:
        raise ValueError(f"unrecognized build_time format: {build_time!r}")

    month = _MONTHS.get(date_match.group(1))
    if month is None:
        raise ValueError(f"unrecognized month in build_date: {build_date!r}")
    day = int(date_match.group(2))
    year = int(date_match.group(3))
    hour, minute, second = (int(g) for g in time_match.groups())

    dt = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    return dt.timestamp()
