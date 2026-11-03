"""Two drivers, one to-do list: who does what, in which order.

"My wife's at Stonestown; between us we need to get our daughter from school
to swim by 2, and someone has to hit the bank and a CVS." Each person starts
where they are (the partner's position can come from their phone's shared
location), and each is done after their last task. The tasks become a small
vehicle-routing problem: pickup-and-drop pairs that must share a car, a
deadline at the drop, and errand groups where any one branch near either
person will do. OR-Tools assigns and orders them to minimize the time both
people spend; the plain "I'll do it all" plan is solved too, for comparison.
"""

import time
from datetime import timedelta

from . import places, solver, valhalla
from .verify import verify

PARTNER_FIX_MAX_AGE_S = 15 * 60
CANDIDATES_PER_ERRAND = 8


def _task_dwell(task):
    """Only time the driver stated; no invented pickup or errand durations."""
    return (task.get("dwellMinutes") or 0) * 60


def plan_team(planner, origin, started):
    from .planner import PlanningError, _point, public_trip

    team = planner.ir["team"]
    partner_name = (team.get("partner") or "partner").strip()
    title = partner_name[:1].upper() + partner_name[1:]

    # Where the partner is: what the driver said, else their phone's shared location.
    if team.get("partnerAt"):
        partner = planner.resolve(team["partnerAt"], f"{title} is at")
        partner["label"] = f"{title} ({partner['label']})"
    else:
        fix = (planner.shared_locations or {}).get("partner")
        if not fix or fix.get("ageSeconds", 1e9) > PARTNER_FIX_MAX_AGE_S:
            raise PlanningError(
                f"Where is your {partner_name} right now? Open the share link (/beacon.html?who=partner) "
                f"on your {partner_name}'s phone, or tell me where your {partner_name} is.")
        partner = _point(fix["lat"], fix["lng"], f"{title} (shared location)", source="phone")
        planner.step("resolve", f"{title}: shared location from their phone",
                     match={"label": partner["label"], "category": f"{round(fix['ageSeconds'] / 60)} min old",
                            "locality": None}, alternatives=[], ambiguous=False)
    me = dict(origin, label="You" if origin["label"].startswith("Current location") else f"You ({origin['label']})")
    people = [{"name": "You", "key": "me", "start": me}, {"name": title, "key": "partner", "start": partner}]

    # Tasks -> nodes. Fixed places resolve once; errands get candidate branches.
    now_clock = planner.depart_clock()
    nodes = [me, partner]
    pairs, groups, windows, dwell, tasks_out = [], [], {}, {}, []
    anchors = [me, partner]
    errands = []
    for task in team["tasks"]:
        if task["kind"] == "escort":
            who = task.get("who") or "them"
            pickup = planner.resolve(task["from"], f"Pick up {who}")
            dropoff = planner.resolve(task["to"], f"Bring {who} to")
            p, d = len(nodes), len(nodes) + 1
            nodes += [dict(pickup, task=f"Pick up {who}"), dict(dropoff, task=f"Drop {who} off")]
            dwell[p] = 0
            dwell[d] = _task_dwell(task)
            pairs.append((p, d))
            groups += [[p], [d]]
            deadline = planner.clock_seconds(task.get("arriveBy"))
            if deadline is not None:
                if deadline - now_clock <= 0:
                    raise PlanningError(f"It's already past {task['arriveBy']} for {who}.")
                windows[d] = deadline - now_clock
                nodes[d]["arriveByClock"] = deadline
            anchors += [pickup, dropoff]
            tasks_out.append({"kind": "escort", "label": f"{who.title()}: {pickup['label']} → {dropoff['label']}"
                              + (f" by {task['arriveBy']}" if task.get("arriveBy") else "")})
        elif task["kind"] == "place":
            place = planner.resolve(task["text"], "Task")
            node = len(nodes)
            nodes.append(dict(place, task=f"Go to {place['label']}"))
            dwell[node] = _task_dwell(task)
            groups.append([node])
            anchors.append(place)
            tasks_out.append({"kind": "place", "label": place["label"]})
        else:
            errands.append(task)

    # Errand branches: along the straight lines between everyone's places,
    # plus the nearest few to each person's start.
    line = [(p["lat"], p["lng"]) for p in anchors]
    for task in errands:
        label = task["what"] + (f" ({' or '.join(task['brands'])})" if task["brands"] else "")
        found, seen = [], set()
        for cand in (places.search_along_route(line, task["what"], task["brands"], max_offset_m=2500,
                                               limit=CANDIDATES_PER_ERRAND)
                     + [c for start in (me, partner) for c in places.search_along_route(
                         [(start["lat"], start["lng"]), (start["lat"] + 1e-4, start["lng"])],
                         task["what"], task["brands"], max_offset_m=4000, limit=3)]):
            if cand["id"] not in seen:
                seen.add(cand["id"])
                found.append(cand)
        if not found:
            raise PlanningError(f"No {label} near either of you in the open places data.")
        group = []
        for cand in found:
            group.append(len(nodes))
            dwell[len(nodes)] = _task_dwell(task)
            nodes.append(_point(cand["lat"], cand["lng"], cand["label"], address=cand.get("address"),
                                category=cand["category"], locality=cand.get("locality"), source="Overture",
                                id=cand["id"], task=label[:1].upper() + label[1:]))
        groups.append(group)
        planner.step("search", f"Search: {label} near either of you", found=len(found), position="anywhere",
                     points=[[c["lng"], c["lat"]] for c in found])
        tasks_out.append({"kind": "errand", "label": label})
    if len(nodes) == 2:
        raise PlanningError("What needs doing?")

    # One matrix between everything, then the assignment.
    t0 = time.perf_counter()
    opts = (planner.costing, planner.costing_options or None, planner.exclude_locations, planner.exclude_polygons)
    durations, _ = valhalla.matrix(nodes, nodes, *opts)
    matrix_ms = round((time.perf_counter() - t0) * 1000)
    t0 = time.perf_counter()
    solved = solver.solve_team(durations, [0, 1], groups, pairs, dwell, windows)
    solve_ms = round((time.perf_counter() - t0) * 1000)
    if solved is None:
        raise PlanningError("There's no way to fit all of that in, even splitting it between you: "
                            "the deadline can't be met from where you both are.")
    paths, _ = solved

    def busy_seconds(path):
        # Driving plus time at every stop, including the last.
        return 0 if len(path) < 2 else solver.route_seconds(durations, path, dwell) + dwell.get(path[-1], 0)

    together = sum(busy_seconds(path) for path in paths)
    alone = {}
    for k, person in enumerate(people):
        one = solver.solve_team(durations, [k], groups, pairs, dwell, windows, optional=[1 - k], time_limit_s=1)
        alone[person["key"]] = None if one is None else busy_seconds(one[0][0])

    def clock(seconds):
        return (planner.now.replace(hour=0, minute=0, second=0, microsecond=0)
                + timedelta(seconds=now_clock + seconds)).strftime("%-I:%M%p").lower()

    # Route each person's part for real, then check it independently.
    out_people, checks = [], []
    for person, path in zip(people, paths):
        stops = [dict(nodes[i], dwellSeconds=dwell.get(i, 0)) for i in path[1:]]
        entry = {"name": person["name"], "key": person["key"], "start": person["start"], "stops": stops,
                 "route": None, "navRequest": None, "doneAt": None, "seconds": 0}
        if stops:
            trip = planner.route_with_exclusions([person["start"], *stops], f"{person['name']}'s route")
            edges = valhalla.edges_for_route(trip, planner.costing)
            # Clock times along the final route itself, not the solver's numbers.
            t = 0.0
            for i, stop in enumerate(stops):
                t += trip["legDurations"][i] if i < len(trip["legDurations"]) else 0
                stop["arriveAt"] = clock(t)
                stop["arriveSeconds"] = t
                if stop.get("arriveByClock") is not None:
                    stop["closesAtClock"] = stop["arriveByClock"]
                t += stop["dwellSeconds"]
            for check in verify(trip, edges, dict(planner.ir, stopOrder="any"), stops, planner.rules, planner.storm_rings,
                                now_clock, None):
                check["name"] = f"{person['name']}: {check['name'][:1].lower()}{check['name'][1:]}"
                checks.append(check)
            entry.update(route=public_trip(trip), navRequest=planner.nav_request([person["start"], *stops]),
                         doneAt=clock(t), seconds=t)
        out_people.append(entry)
    for pickup, dropoff in pairs:
        holder = next(p for p in out_people if any(s is not None and s.get("task") == nodes[pickup]["task"]
                                                   for s in p["stops"]))
        order = [s.get("task") for s in holder["stops"]]
        checks.append({"name": f"{nodes[pickup]['task']} before {nodes[dropoff]['task'].lower()}, same car",
                       "passed": nodes[dropoff]["task"] in order
                       and order.index(nodes[pickup]["task"]) < order.index(nodes[dropoff]["task"]),
                       "detail": holder["name"]})

    best_alone_key = min((k for k in alone if alone[k] is not None), key=lambda k: alone[k], default=None)
    comparison = {
        "togetherSeconds": together,
        "doneAt": max(out_people, key=lambda p: p["seconds"])["doneAt"],
        "alone": [{"name": person["name"], "seconds": alone[person["key"]],
                   "doneAt": clock(alone[person["key"]]) if alone[person["key"]] is not None else None}
                  for person in people],
        "bestAlone": best_alone_key and next(p["name"] for p in people if p["key"] == best_alone_key),
    }
    planner.step("solve", "Who does what: OR-Tools, pickups paired with drops, deadlines as time windows",
                 nodes=len(nodes), matrixMs=matrix_ms, solveMs=solve_ms, totalMinutes=round(together / 60, 1))
    planner.step("verify", "Independent check of both routes", checks=checks)

    # The top-level route is the driver's own (what "Car" sends); the
    # partner's is in team.people. If the driver has nothing to do, show theirs.
    lead = out_people[0] if out_people[0]["route"] else out_people[1]
    return {
        "origin": lead["start"],
        "destination": lead["stops"][-1],
        "stops": lead["stops"][:-1],
        "route": lead["route"],
        "baseline": None,
        "dwellSeconds": sum(s["dwellSeconds"] for s in lead["stops"]),
        "areas": [],
        "excludedPoints": planner.exclude_locations,
        "stormRings": planner.storm_rings,
        "checks": checks,
        "allChecksPassed": all(check["passed"] for check in checks),
        "notes": planner.notes,
        "team": {"people": out_people, "tasks": tasks_out, "comparison": comparison, "lead": lead["key"]},
        "navRequest": lead["navRequest"],
        "trace": planner.trace,
        "plannerMs": round((time.perf_counter() - started) * 1000),
    }
