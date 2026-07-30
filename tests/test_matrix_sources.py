"""Offline tests for the haversine fallback source (osrm/here need network)."""

from app.matrix_sources import HAVERSINE_SPEED_KMH, haversine_km, haversine_matrix


def test_haversine_known_distance():
    # West Bromwich centre -> Birmingham centre is roughly 8 km great-circle
    d = haversine_km((52.5187, -1.9945), (52.4862, -1.8904))
    assert 7.0 < d < 9.0


def test_haversine_matrix_shape_and_symmetry():
    coords = [(52.5187, -1.9945), (52.5570, -2.0122), (52.5090, -1.9400)]
    m = haversine_matrix(coords)
    n = len(coords)
    assert len(m) == n and all(len(row) == n for row in m)
    for i in range(n):
        assert m[i][i] == 0
        for j in range(n):
            assert m[i][j] == m[j][i]
            assert m[i][j] >= 0


def test_haversine_seconds_scale():
    # 1 km at the assumed urban speed should be 3600/speed seconds
    coords = [(52.0, -2.0), (52.008993, -2.0)]  # ~1 km apart on a meridian
    m = haversine_matrix(coords)
    expected = 3600.0 / HAVERSINE_SPEED_KMH
    assert abs(m[0][1] - expected) < expected * 0.05
