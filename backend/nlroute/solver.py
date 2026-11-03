"""Choosing WHICH errand stop and in WHAT order.

Two exact methods, picked by what the driver said:

- Ordered stops ("coffee, then pick up Sam, then gas") form a layered graph:
  one layer per stop, each layer holding that stop's candidates. A shortest
  path through the layers is exact and instant, no solver needed.
- Unordered errands ("pharmacy, groceries and gas, any order, home by 6") are a
  generalized TSP with time windows: pick ONE candidate per errand group and
  an order. OR-Tools' routing solver handles it with one disjunction per group.
"""

from ortools.constraint_solver import pywrapcp, routing_enums_pb2


UNREACHABLE = 10 ** 7


def _cost(durations, i, j):
    value = durations[i][j]
    return UNREACHABLE if value is None else int(round(value))


def solve_ordered(durations, layers, dwell):
    """Exact shortest path through ordered layers of node indices.

    layers: [[origin], [cands of stop 1], ..., [destination]].
    dwell: seconds spent at each node. Returns (node sequence, seconds).
    """
    best = {node: (0, [node]) for node in layers[0]}
    for layer in layers[1:]:
        nxt = {}
        for node in layer:
            options = [
                (cost + _cost(durations, prev, node) + dwell.get(node, 0), path + [node])
                for prev, (cost, path) in best.items()
            ]
            nxt[node] = min(options, key=lambda option: option[0])
        best = nxt
    total, path = min(best.values(), key=lambda option: option[0])
    return path, total


def solve_any_order(durations, origin, destination, required, groups, dwell,
                    windows=None, deadline=None, time_limit_s=1):
    """OR-Tools generalized TSP with optional time windows.

    required: node indices that must be visited (named places).
    groups: lists of node indices; exactly one per group is visited.
    windows: {node: latest arrival seconds}; deadline: latest arrival at destination.
    Returns (node sequence, seconds) or None if infeasible.
    """
    n = len(durations)
    manager = pywrapcp.RoutingIndexManager(n, 1, [origin], [destination])
    routing = pywrapcp.RoutingModel(manager)

    def transit(from_index, to_index):
        i = manager.IndexToNode(from_index)
        j = manager.IndexToNode(to_index)
        return _cost(durations, i, j) + int(dwell.get(i, 0))

    transit_index = routing.RegisterTransitCallback(transit)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_index)
    horizon = 24 * 3600
    routing.AddDimension(transit_index, 0, horizon, True, "Time")
    time_dim = routing.GetDimensionOrDie("Time")

    in_play = {origin, destination, *required, *(node for group in groups for node in group)}
    for node in range(n):
        if node not in in_play:
            # Not part of this problem: allow skipping at no cost.
            routing.AddDisjunction([manager.NodeToIndex(node)], 0)
    for group in groups:
        # Exactly one member: max cardinality 1, and a huge penalty for none.
        routing.AddDisjunction([manager.NodeToIndex(node) for node in group], UNREACHABLE, 1)
    for node, latest in (windows or {}).items():
        time_dim.CumulVar(manager.NodeToIndex(node)).SetMax(int(latest))
    if deadline is not None:
        time_dim.CumulVar(routing.End(0)).SetMax(int(deadline))

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    params.time_limit.FromSeconds(time_limit_s)
    solution = routing.SolveWithParameters(params)
    if solution is None:
        return None

    index = routing.Start(0)
    path = []
    while not routing.IsEnd(index):
        path.append(manager.IndexToNode(index))
        index = solution.Value(routing.NextVar(index))
    path.append(manager.IndexToNode(index))
    visited = set(path)
    # The penalty makes skipping a group legal but expensive; a skipped group
    # means no candidate fits the windows, which is infeasible for the driver.
    if any(not visited.intersection(group) for group in groups):
        return None
    return path, solution.Value(time_dim.CumulVar(routing.End(0)))


def route_seconds(durations, path, dwell):
    """Recost a node path from the matrix: never trust the solver's objective."""
    total = 0
    for a, b in zip(path, path[1:]):
        if durations[a][b] is None:
            return None
        total += durations[a][b] + dwell.get(a, 0)
    return total


def solve_team(durations, starts, groups, pairs, dwell, windows=None, optional=(), time_limit_s=2):
    """Several drivers sharing one list of tasks (a VRP with pickups and drops).

    Each driver starts where they are and is done after their last task, so
    the routes are open-ended: node `len(durations)` is a virtual end that
    every node reaches once its dwell is done. Minimizes the time everyone
    spends (driving plus stops) plus when the last person finishes, so a
    split that gets the family done sooner wins over one that merely
    trades minutes from one car to the other.

    starts: one start node per driver.
    groups: lists of candidate nodes; exactly one per group is visited, by anyone.
    pairs: (pickup, dropoff) nodes, same driver, pickup first ("bring her to swim").
    windows: {node: latest arrival, seconds from now}.
    optional: nodes that may be left out at no cost (another driver's start
        when solving for one driver alone).
    Returns ([node path per driver], {node: arrival seconds}) or None.
    """
    n = len(durations)
    end = n
    manager = pywrapcp.RoutingIndexManager(n + 1, len(starts), list(starts), [end] * len(starts))
    routing = pywrapcp.RoutingModel(manager)

    def transit(from_index, to_index):
        i = manager.IndexToNode(from_index)
        j = manager.IndexToNode(to_index)
        if i == end:
            return 0
        return (0 if j == end else _cost(durations, i, j)) + int(dwell.get(i, 0))

    transit_index = routing.RegisterTransitCallback(transit)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_index)
    routing.AddDimension(transit_index, 0, 24 * 3600, True, "Time")
    time_dim = routing.GetDimensionOrDie("Time")
    time_dim.SetGlobalSpanCostCoefficient(1)

    for node in optional:
        routing.AddDisjunction([manager.NodeToIndex(node)], 0)
    for group in groups:
        routing.AddDisjunction([manager.NodeToIndex(node) for node in group], UNREACHABLE, 1)
    solver = routing.solver()
    for pickup, dropoff in pairs:
        p, d = manager.NodeToIndex(pickup), manager.NodeToIndex(dropoff)
        routing.AddPickupAndDelivery(p, d)
        solver.Add(routing.VehicleVar(p) == routing.VehicleVar(d))
        solver.Add(time_dim.CumulVar(p) <= time_dim.CumulVar(d))
    for node, latest in (windows or {}).items():
        time_dim.CumulVar(manager.NodeToIndex(node)).SetMax(int(latest))

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION
    params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    params.time_limit.FromSeconds(time_limit_s)
    solution = routing.SolveWithParameters(params)
    if solution is None:
        return None

    paths, arrivals = [], {}
    for vehicle in range(len(starts)):
        index = routing.Start(vehicle)
        path = []
        while not routing.IsEnd(index):
            node = manager.IndexToNode(index)
            path.append(node)
            arrivals[node] = solution.Value(time_dim.CumulVar(index))
            index = solution.Value(routing.NextVar(index))
        paths.append(path)
    visited = {node for path in paths for node in path}
    if any(not visited.intersection(group) for group in groups):
        return None
    return paths, arrivals
