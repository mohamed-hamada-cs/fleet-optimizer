"""Solver correctness tests on fixed synthetic matrices — no network, no HERE.

Geometry: stops on a number line, travel time = |xi - xj|. Brute force compares
against every valid permutation, so these tests prove optimality, not just
feasibility, for small instances.
"""

from itertools import permutations

import pytest

from app.solver import (
    SolverError,
    pm_order_from_am,
    solve_trip,
    trip_cost,
    validate_order,
)


def line_matrix(xs: list[float]) -> list[list[int]]:
    return [[int(abs(a - b)) for b in xs] for a in xs]


def brute_force_best(matrix, start, end, first_group, second_group) -> int:
    best = None
    for firsts in permutations(first_group):
        for seconds in permutations(second_group):
            order = [start, *firsts, *seconds, end]
            cost = trip_cost(matrix, order)
            if best is None or cost < best:
                best = cost
    return best


def assert_valid_and_optimal(matrix, start, end, first_group, second_group):
    order = solve_trip(matrix, start, end, first_group, second_group)
    validate_order(order, start, end, first_group, second_group)
    assert trip_cost(matrix, order) == brute_force_best(
        matrix, start, end, first_group, second_group
    )
    return order


def test_am_line_geometry_natural_order():
    # depot=0, PA1=1, PA2=2, students at 3,4,5, school=10
    xs = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 10.0]
    order = assert_valid_and_optimal(line_matrix(xs), 0, 6, [1, 2], [3, 4, 5])
    assert order == [0, 1, 2, 3, 4, 5, 6]


def test_school_geographically_in_the_middle_still_last():
    # school (idx 5) sits at x=3, between students at 1, 5 and 6 — a naive TSP
    # would visit it mid-route; ours must keep it last.
    xs = [0.0, 2.0, 1.0, 5.0, 6.0, 3.0]  # depot, pa, s1, s2, s3, school
    order = assert_valid_and_optimal(line_matrix(xs), 0, 5, [1], [2, 3, 4])
    assert order[-1] == 5
    assert order[1] == 1  # the PA still comes first


def test_no_pa_starts_at_depot():
    xs = [0.0, 4.0, 2.0, 6.0, 8.0]  # depot, s1, s2, s3, school
    order = assert_valid_and_optimal(line_matrix(xs), 0, 4, [], [1, 2, 3])
    assert order == [0, 2, 1, 3, 4]  # nearest student first


def test_pa_choice_considers_student_leg_coupling():
    # Two PAs equidistant-ish from depot but only one flows well into the
    # students: the coupled solve must pick the order ending nearest the
    # first student. Two-stage with a free-ended PA leg can get this wrong.
    xs = [0.0, 1.0, 3.0, 10.0, 11.0, 12.0, 20.0]  # depot, paA, paB, s1, s2, s3, school
    order = assert_valid_and_optimal(line_matrix(xs), 0, 6, [1, 2], [3, 4, 5])
    assert order == [0, 1, 2, 3, 4, 5, 6]  # paB (x=3) last among PAs, flowing into s1 (x=10)


def test_pm_mirror_direction():
    # PM: school start, depot end, students before PAs.
    xs = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 10.0]
    order = assert_valid_and_optimal(line_matrix(xs), 6, 0, [3, 4, 5], [1, 2])
    assert order == [6, 5, 4, 3, 2, 1, 0]


def test_pm_default_is_reversed_am():
    assert pm_order_from_am([0, 1, 2, 3, 6]) == [6, 3, 2, 1, 0]


def test_sixteen_students_solves_and_validates():
    xs = [0.0, 1.0] + [float(3 + i) for i in range(16)] + [30.0]
    matrix = line_matrix(xs)
    order = solve_trip(matrix, 0, 17 + 1, [1], list(range(2, 18)))
    validate_order(order, 0, 18, [1], list(range(2, 18)))


def test_duplicate_indices_rejected():
    xs = [0.0, 1.0, 2.0, 3.0]
    with pytest.raises(SolverError):
        solve_trip(line_matrix(xs), 0, 3, [1], [1, 2])
