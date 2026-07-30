"""Validation of a manager's hand-written stop order (the 'is my order better?' feature)."""

from app.solver import check_order_structure

# indices: 0 depot, 1 pa, 2-4 students, 5 school
START, END = 0, 5
PAS = [1]
STUDENTS = [2, 3, 4]
LABELS = {0: "depot", 1: "pa", 2: "s1", 3: "s2", 4: "s3", 5: "school"}


def check(order):
    return check_order_structure(order, START, END, PAS, STUDENTS, LABELS)


def test_valid_order_has_no_violations():
    assert check([0, 1, 3, 2, 4, 5]) == []


def test_school_not_last_is_caught():
    problems = check([0, 1, 2, 5, 3, 4])
    assert any("must end at school" in p for p in problems)


def test_depot_not_first_is_caught():
    problems = check([1, 0, 2, 3, 4, 5])
    assert any("must start at depot" in p for p in problems)


def test_pa_after_student_is_caught():
    problems = check([0, 2, 1, 3, 4, 5])
    assert any("pa must be visited before s1" in p for p in problems)


def test_missing_stop_is_caught():
    problems = check([0, 1, 2, 3, 5])
    assert any("missing stops: s3" in p for p in problems)


def test_duplicate_stop_is_caught():
    problems = check([0, 1, 2, 2, 3, 5])
    assert any("more than once" in p for p in problems)


def test_multiple_violations_reported_together():
    # depot last, school first, PA after students
    problems = check([5, 2, 3, 4, 1, 0])
    assert len(problems) >= 2
