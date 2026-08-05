"""HERE flexible polyline -> polyline5 normalisation.

HERE returns geometry in its own format; the dashboard only decodes polyline5, so a
HERE-sourced route came back undrawable. The service now normalises to polyline5.
"""

import pytest

from app.flexpolyline import decode_flexpolyline, encode_polyline5

HERE_EXAMPLE = "BFoz5xJ67i1B1B7PzIhaxL7Y"  # heremaps/flexible-polyline reference


def decode_polyline5(encoded: str):
    """Independent reference decoder, so the test does not trust our encoder."""
    pts, index, lat, lng = [], 0, 0, 0
    while index < len(encoded):
        for axis in range(2):
            result = shift = 0
            while True:
                byte = ord(encoded[index]) - 63
                index += 1
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if axis == 0:
                lat += delta
            else:
                lng += delta
        pts.append((lat / 1e5, lng / 1e5))
    return pts


def test_decodes_here_reference_example():
    pts = decode_flexpolyline(HERE_EXAMPLE)
    assert len(pts) == 4
    assert pts[0] == pytest.approx((50.10228, 8.69821), abs=1e-4)


def test_roundtrip_here_to_polyline5():
    pts = decode_flexpolyline(HERE_EXAMPLE)
    again = decode_polyline5(encode_polyline5(pts))
    assert len(again) == len(pts)
    for a, b in zip(pts, again):
        assert a[0] == pytest.approx(b[0], abs=1e-5)
        assert a[1] == pytest.approx(b[1], abs=1e-5)


def test_encoder_matches_known_polyline5():
    # Google's documented example
    assert encode_polyline5([(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)]) == "_p~iF~ps|U_ulLnnqC_mqNvxq`@"


def test_empty_is_safe():
    assert decode_flexpolyline("") == []
    assert encode_polyline5([]) == ""


def test_rejects_unknown_version():
    with pytest.raises(ValueError):
        decode_flexpolyline("CFoz5xJ67i1B1B7PzIhaxL7Y")
