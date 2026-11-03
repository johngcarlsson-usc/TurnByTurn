import unittest
from unittest.mock import patch

from nlroute import valhalla
from nlroute.constraints import normalize_ir
from nlroute.rules import road_name_matches
from nlroute.solver import route_seconds, solve_any_order, solve_ordered, solve_team


class NormalizeIrTests(unittest.TestCase):
    def test_malformed_fields_fall_back_to_neutral_defaults(self):
        ir = normalize_ir({
            "destination": " Stanford ",
            "stops": [{"kind": "bogus", "what": "Coffee"}, {"kind": "place"}, "junk"],
            "stopOrder": "sideways",
            "arriveBy": "25:00",
            "avoid": {"areas": [{"text": "downtown", "roads": "some"}], "roads": [{"name": ""}]},
            "profile": "spaceship",
        })
        self.assertEqual(ir["destination"], "Stanford")
        self.assertEqual(len(ir["stops"]), 1)
        self.assertEqual(ir["stops"][0]["kind"], "errand")
        self.assertEqual(ir["stops"][0]["what"], "coffee")
        self.assertEqual(ir["stopOrder"], "as_listed")
        self.assertIsNone(ir["arriveBy"])
        self.assertEqual(ir["avoid"]["areas"][0]["roads"], "all")
        self.assertEqual(ir["avoid"]["roads"], [])
        self.assertEqual(ir["profile"], "auto")


class TeamIrTests(unittest.TestCase):
    def test_team_tasks_are_cleaned(self):
        ir = normalize_ir({"team": {"partner": "wife", "tasks": [
            {"kind": "escort", "who": "daughter", "from": "school", "to": "pool", "arriveBy": "2:00"},
            {"kind": "escort", "who": "son", "from": "school"},  # no destination: dropped
            {"what": "bank"},
        ]}})
        tasks = ir["team"]["tasks"]
        self.assertEqual([t["kind"] for t in tasks], ["escort", "errand"])
        self.assertEqual(tasks[0]["arriveBy"], "02:00")
        self.assertEqual(ir["team"]["partner"], "wife")

    def test_no_tasks_means_no_team(self):
        ir = normalize_ir({"team": {"partner": "wife", "partnerAt": "Stonestown", "tasks": []}})
        self.assertEqual(ir["team"], {"partner": None, "partnerAt": None, "tasks": []})


class RoadNameTests(unittest.TestCase):
    def test_refs_match_exactly(self):
        self.assertTrue(road_name_matches("I-80", ["I 80 East"]))
        self.assertTrue(road_name_matches("Interstate 80", ["I 80 West"]))
        self.assertFalse(road_name_matches("I-80", ["I 880 South"]))

    def test_bridge_does_not_match_street(self):
        self.assertFalse(road_name_matches("Bay Bridge", ["Bay Street"]))
        self.assertTrue(road_name_matches("19th Ave", ["19th Avenue"]))


class SolverTests(unittest.TestCase):
    # 0 origin, 1-2 group A, 3-4 group B, 5 destination.
    D = [
        [0, 10, 50, 40, 10, 99],
        [10, 0, 40, 10, 30, 60],
        [50, 40, 0, 30, 30, 10],
        [40, 10, 30, 0, 30, 20],
        [10, 30, 30, 30, 0, 80],
        [99, 60, 10, 20, 80, 0],
    ]

    def test_ordered_layers_pick_exact_best(self):
        path, total = solve_ordered(self.D, [[0], [1, 2], [3, 4], [5]], {})
        self.assertEqual(path, [0, 1, 3, 5])
        self.assertEqual(total, 40)

    def test_any_order_visits_one_per_group(self):
        path, _ = solve_any_order(self.D, 0, 5, [], [[1, 2], [3, 4]], {}, time_limit_s=1)
        self.assertEqual(path[0], 0)
        self.assertEqual(path[-1], 5)
        self.assertEqual(len(set(path[1:-1]) & {1, 2}), 1)
        self.assertEqual(len(set(path[1:-1]) & {3, 4}), 1)
        self.assertEqual(route_seconds(self.D, path, {}), 40)

    def test_any_order_reports_infeasible_deadline(self):
        self.assertIsNone(solve_any_order(self.D, 0, 5, [], [[1, 2], [3, 4]], {}, deadline=30, time_limit_s=1))


class TeamSolverTests(unittest.TestCase):
    # Nodes: 0 = you, 1 = partner, 2 = pick up kid, 3 = drop kid, 4/5 = two banks.
    # The kid is near the partner, one bank is near you.
    D = [
        [0, 900, 1500, 1800, 60, 1500],
        [900, 0, 300, 600, 900, 600],
        [1500, 300, 0, 400, 1400, 700],
        [1800, 600, 400, 0, 1700, 800],
        [60, 900, 1400, 1700, 0, 1400],
        [1500, 600, 700, 800, 1400, 0],
    ]

    def test_splits_tasks_and_keeps_pickup_with_drop(self):
        paths, _ = solve_team(self.D, [0, 1], [[2], [3], [4, 5]], [(2, 3)], {})
        self.assertEqual(paths, [[0, 4], [1, 2, 3]])

    def test_deadline_at_drop_is_enforced(self):
        # Drop by 500 s is impossible for anyone (partner needs 300 + 400).
        self.assertIsNone(solve_team(self.D, [0, 1], [[2], [3], [4, 5]], [(2, 3)], {}, windows={3: 500}))

    def test_single_driver_may_skip_the_other_start(self):
        paths, _ = solve_team(self.D, [1], [[2], [3], [4, 5]], [(2, 3)], {}, optional=[0])
        self.assertEqual(paths[0][:3], [1, 2, 3])
        self.assertNotIn(0, paths[0])


class MatrixResnapTests(unittest.TestCase):
    def test_mostly_null_point_is_resnapped_not_route_backfilled(self):
        points = [{"lat": 37.0 + i / 100, "lng": -122.0} for i in range(4)]
        calls = []

        def fake_call(sources, targets, *opts, resnap_sources=(), resnap_targets=()):
            calls.append((len(sources), len(targets), set(resnap_sources), set(resnap_targets)))
            if not resnap_sources and not resnap_targets:
                # Point 2 fails in its whole row and column, like a parking-aisle snap.
                grid = [[None if 2 in (i, j) and i != j else 1.0 for j in range(4)] for i in range(4)]
                return grid, grid, False  # durations, distances, whether live traffic was used
            rows = [[5.0] * len(targets) for _ in sources]
            return rows, rows, False

        with patch.object(valhalla, "_matrix_call", side_effect=fake_call), \
                patch.object(valhalla, "route") as route:
            durations, _ = valhalla.matrix(points, points)
        route.assert_not_called()
        self.assertTrue(all(cell is not None for row in durations for cell in row))
        self.assertEqual(valhalla.matrix.last_stats["resnappedSources"], 1)
        self.assertEqual(valhalla.matrix.last_stats["resnappedTargets"], 1)


class AfterMinutesTests(unittest.TestCase):
    def test_after_minutes_is_kept_for_errands_and_clamped(self):
        ir = normalize_ir({"destination": "Sacramento", "stops": [
            {"kind": "errand", "what": "coffee", "afterMinutes": 60},
            {"kind": "errand", "what": "gas", "afterMinutes": -5},
            {"kind": "place", "text": "Davis", "afterMinutes": 30}]})
        self.assertEqual([s["afterMinutes"] for s in ir["stops"]], [60, 1, None])

    def test_stretch_follows_maneuver_times_not_distance(self):
        from nlroute.planner import Planner
        # 0.1 degree of latitude per vertex; the first maneuver is slow (60 min for 2 segments),
        # the second fast (10 min for 2 segments), so 60 minutes in is the third vertex, not the middle
        trip = {"latlons": [(37.0 + i / 10, -122.0) for i in range(5)], "durationSeconds": 4200,
                "steps": [{"beginShapeIndex": 0, "durationSeconds": 3600},
                          {"beginShapeIndex": 2, "durationSeconds": 600}]}
        stretch = Planner.stretch_at(trip, 60, 1)
        self.assertEqual(stretch, [(37.1, -122.0), (37.2, -122.0)])  # the segment driven at 60 min
        self.assertEqual(len(Planner.stretch_at(trip, 60, 31)), 4)  # 29..91 min: vertices 1-4
        self.assertIsNone(Planner.stretch_at(trip, 120, 5))  # the trip is only 70 minutes


class PreferKindTests(unittest.TestCase):
    def test_a_named_kind_beats_a_name_that_only_mentions_it(self):
        from nlroute.geocode import prefer_kind
        hits = [{"label": "Oakland Airport Executive Hotel", "category": "tourism/hotel"},
                {"label": "Oakland International Airport", "category": "aeroway/aerodrome"}]
        self.assertEqual(prefer_kind("Oakland airport", hits)[0]["label"], "Oakland International Airport")
        self.assertEqual(prefer_kind("Oakland", hits), hits)  # no kind named: order kept


if __name__ == "__main__":
    unittest.main()
