import json
import unittest
from unittest.mock import Mock, patch

import server


class AgentPlanTests(unittest.TestCase):
    def test_normalize_agent_plan_limits_fields_and_accepts_truck(self):
        plan = server.normalize_agent_plan({
            "originText": " Port of Houston ",
            "destinationText": "Port Arthur",
            "includeTexts": ["Baytown", "Beaumont"],
            "avoidStorms": True,
            "profile": "TRUCK",
            "clarification": None,
            "ignored": "not returned",
        })

        self.assertEqual(plan["originText"], "Port of Houston")
        self.assertEqual(plan["includeTexts"], ["Baytown", "Beaumont"])
        self.assertEqual(plan["profile"], "truck")
        self.assertTrue(plan["avoidStorms"])
        self.assertNotIn("ignored", plan)

    def test_parse_model_json_accepts_fenced_json(self):
        parsed = server.parse_model_json(
            "```json\n"
            + json.dumps({
                "originText": "Austin",
                "destinationText": "Dallas",
                "includeTexts": [],
                "avoidStorms": False,
                "profile": "auto",
                "clarification": None,
            })
            + "\n```"
        )
        self.assertEqual(parsed["destinationText"], "Dallas")

    @patch("server.requests.post")
    def test_interpret_route_request_calls_local_ollama(self, post):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "message": {
                "content": json.dumps({
                    "originText": "New Orleans",
                    "destinationText": "Houston",
                    "includeTexts": ["Lafayette"],
                    "avoidStorms": True,
                    "profile": "truck",
                    "clarification": None,
                }),
            },
        }
        post.return_value = response

        plan = server.interpret_route_request("Take my semi from New Orleans to Houston via Lafayette")

        self.assertEqual(plan["profile"], "truck")
        self.assertEqual(plan["includeTexts"], ["Lafayette"])
        self.assertTrue(post.call_args.args[0].endswith("/api/chat"))
        self.assertEqual(post.call_args.kwargs["json"]["model"], server.OLLAMA_MODEL)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()

    def test_health_describes_open_source_stack(self):
        response = self.client.get("/api/health")
        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["service"], "turn-by-turn")
        self.assertEqual(payload["openSourceStack"]["routing"], "Valhalla")

    @patch("server.requests.get")
    def test_geocode_normalizes_nominatim_result(self, get):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = [{
            "display_name": "Houston, Harris County, Texas, USA",
            "lat": "29.7589",
            "lon": "-95.3677",
        }]
        get.return_value = response

        result = self.client.get("/api/geocode?q=Houston")
        payload = result.get_json()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(payload["source"], "Nominatim / OpenStreetMap (local)")
        self.assertAlmostEqual(payload["result"]["lat"], 29.7589)


if __name__ == "__main__":
    unittest.main()
