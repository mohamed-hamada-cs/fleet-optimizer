"""Stop-order optimization for split-shift school runs.

One vehicle, one trip, stops pre-assigned. The trip is an open TSP with a fixed
start, a fixed end, and a group-precedence rule:

  AM: depot -> all PAs (any order) -> all students (any order) -> school
  PM: school -> all students -> all PAs -> depot   (mirror)

Implementation: a single OR-Tools routing solve over ALL stops with arcs from the
second group into the first group forbidden (big-M cost). Forbidding
second->first arcs is exactly the precedence constraint: on a single path from
the fixed start, a first-group stop can only be entered from the start or from
its own group, so no second-group stop can ever precede a first-group stop.

This is deliberately NOT the two-stage decomposition (solve PA leg, then student
leg): a free-ended PA leg picks the "last PA" blind to the student leg, losing
the coupling (planning doc section 2). The single solve optimizes that
transition globally, and validate_order() still enforces the structural
guarantees after the fact.
"""

from __future__ import annotations

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

BIG_M = 10**9
SOLVE_TIME_LIMIT_SECONDS = 1


class SolverError(Exception):
    """Raised when no valid order satisfying the constraints was found."""


def solve_trip(
    matrix: list[list[int]],
    start: int,
    end: int,
    first_group: list[int],
    second_group: list[int],
) -> list[int]:
    """Return the optimal stop order (list of matrix indices, start..end inclusive).

    All of first_group is visited before any of second_group. Either group may
    be empty. `matrix` is an NxN travel-time matrix covering every index used.
    """
    nodes = [start, *first_group, *second_group, end]
    if len(set(nodes)) != len(nodes):
        raise SolverError("duplicate stop indices")

    n = len(nodes)
    first_local = {i for i, node in enumerate(nodes) if node in set(first_group)}
    second_local = {i for i, node in enumerate(nodes) if node in set(second_group)}

    def cost(i_local: int, j_local: int) -> int:
        if i_local in second_local and j_local in first_local:
            return BIG_M  # precedence: never enter the first group from the second
        return int(matrix[nodes[i_local]][nodes[j_local]])

    manager = pywrapcp.RoutingIndexManager(n, 1, [0], [n - 1])
    routing = pywrapcp.RoutingModel(manager)

    def transit(from_index: int, to_index: int) -> int:
        return cost(manager.IndexToNode(from_index), manager.IndexToNode(to_index))

    routing.SetArcCostEvaluatorOfAllVehicles(routing.RegisterTransitCallback(transit))

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    params.time_limit.FromSeconds(SOLVE_TIME_LIMIT_SECONDS)

    solution = routing.SolveWithParameters(params)
    if solution is None:
        raise SolverError("no solution found")

    order_local: list[int] = []
    index = routing.Start(0)
    while not routing.IsEnd(index):
        order_local.append(manager.IndexToNode(index))
        index = solution.Value(routing.NextVar(index))
    order_local.append(manager.IndexToNode(index))

    order = [nodes[i] for i in order_local]
    validate_order(order, start, end, first_group, second_group)
    return order


def validate_order(
    order: list[int],
    start: int,
    end: int,
    first_group: list[int],
    second_group: list[int],
) -> None:
    """Hard guarantee: raise if the order violates the trip structure."""
    if order[0] != start:
        raise SolverError("order does not start at the fixed start")
    if order[-1] != end:
        raise SolverError("order does not end at the fixed end")
    if sorted(order) != sorted([start, *first_group, *second_group, end]):
        raise SolverError("order does not visit every stop exactly once")
    if second_group:
        positions = {node: i for i, node in enumerate(order)}
        earliest_second = min(positions[node] for node in second_group)
        for node in first_group:
            if positions[node] > earliest_second:
                raise SolverError("precedence violated: second-group stop before first-group stop")


def trip_cost(matrix: list[list[int]], order: list[int]) -> int:
    """Total travel time of an ordered trip (for tests and reporting)."""
    return sum(matrix[a][b] for a, b in zip(order, order[1:]))


def check_order_structure(
    order: list[int],
    start: int,
    end: int,
    first_group: list[int],
    second_group: list[int],
    label_of: "dict[int, str] | None" = None,
) -> list[str]:
    """Describe every structural rule a proposed order breaks.

    Unlike validate_order (which raises on the solver's own output), this
    collects human-readable violations so a manager's hand-written order can be
    shown exactly what is wrong with it.
    """
    name = (lambda i: (label_of or {}).get(i, str(i)))
    problems: list[str] = []

    expected = sorted([start, *first_group, *second_group, end])
    if sorted(order) != expected:
        missing = [name(i) for i in expected if i not in order]
        extra = [name(i) for i in order if i not in expected]
        duplicated = [name(i) for i in set(order) if order.count(i) > 1]
        if missing:
            problems.append(f"missing stops: {', '.join(missing)}")
        if extra:
            problems.append(f"unknown stops: {', '.join(extra)}")
        if duplicated:
            problems.append(f"stops listed more than once: {', '.join(duplicated)}")
        return problems  # positional checks below would be meaningless

    if order[0] != start:
        problems.append(f"must start at {name(start)}, starts at {name(order[0])}")
    if order[-1] != end:
        problems.append(f"must end at {name(end)}, ends at {name(order[-1])}")

    if first_group and second_group:
        positions = {node: i for i, node in enumerate(order)}
        earliest_second = min(positions[n] for n in second_group)
        late = [name(n) for n in first_group if positions[n] > earliest_second]
        if late:
            first_second = name(min(second_group, key=lambda n: positions[n]))
            problems.append(
                f"{', '.join(late)} must be visited before {first_second}"
            )
    return problems


def pm_order_from_am(am_order: list[int]) -> list[int]:
    """Default PM order: the AM trip mirrored (school -> students reversed -> PAs reversed -> depot).

    Valid when travel times are roughly symmetric; callers wanting a
    directional-traffic-aware PM run solve_trip() with start=school, end=depot,
    first_group=students, second_group=PAs instead.
    """
    return list(reversed(am_order))
