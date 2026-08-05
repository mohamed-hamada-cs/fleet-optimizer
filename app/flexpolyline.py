"""HERE Flexible Polyline decoding + polyline5 encoding.

HERE returns geometry in its own "flexible polyline" format, which is NOT the
polyline5 encoding Google/OSRM use — the dashboard's decoder only speaks
polyline5, so a HERE-sourced route came back undrawable (empty line on the map).

We normalise here: whatever the matrix source, the service returns polyline5, so
consumers have exactly one format to handle.

Reference: https://github.com/heremaps/flexible-polyline
"""

from __future__ import annotations

# Derived from the alphabet rather than a hand-typed lookup table — a mistyped table
# decodes silently wrong, which is far worse than failing loudly.
_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
_DECODING_TABLE = {char: index for index, char in enumerate(_ALPHABET)}


def _decode_unsigned_values(encoded: str):
    result = shift = 0
    for char in encoded:
        value = _DECODING_TABLE.get(char, -1)
        if value < 0:
            raise ValueError("invalid character in flexible polyline")
        result |= (value & 0x1F) << shift
        if (value & 0x20) == 0:
            yield result
            result = shift = 0
        else:
            shift += 5
    if shift > 0:
        raise ValueError("truncated flexible polyline")


def decode_flexpolyline(encoded: str) -> list[tuple[float, float]]:
    """Return [(lat, lng), ...]. Any third dimension (elevation) is discarded."""
    if not encoded:
        return []
    values = list(_decode_unsigned_values(encoded))
    if len(values) < 2:
        return []

    version, header = values[0], values[1]
    if version != 1:
        raise ValueError(f"unsupported flexible polyline version {version}")

    precision = header & 15
    third_dim = (header >> 4) & 7
    factor = 10**precision
    stride = 3 if third_dim else 2

    points: list[tuple[float, float]] = []
    lat = lng = 0
    body = values[2:]
    for i in range(0, len(body) - (len(body) % stride), stride):
        lat += _unzigzag(body[i])
        lng += _unzigzag(body[i + 1])
        points.append((lat / factor, lng / factor))
    return points


def _unzigzag(value: int) -> int:
    return ~(value >> 1) if value & 1 else value >> 1


def encode_polyline5(points: list[tuple[float, float]]) -> str:
    """Encode [(lat, lng), ...] as a precision-5 polyline (Google/OSRM format)."""
    out: list[str] = []
    prev_lat = prev_lng = 0
    for lat, lng in points:
        ilat = int(round(lat * 1e5))
        ilng = int(round(lng * 1e5))
        _encode_value(ilat - prev_lat, out)
        _encode_value(ilng - prev_lng, out)
        prev_lat, prev_lng = ilat, ilng
    return "".join(out)


def _encode_value(delta: int, out: list[str]) -> None:
    value = ~(delta << 1) if delta < 0 else delta << 1
    while value >= 0x20:
        out.append(chr((0x20 | (value & 0x1F)) + 63))
        value >>= 5
    out.append(chr(value + 63))
