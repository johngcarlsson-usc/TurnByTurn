import unittest
from datetime import datetime

from nlroute.agent import run_turn


class ChatAgentTests(unittest.TestCase):
    def test_tool_round_preserves_model_metadata_and_followup_history(self):
        calls = []

        def chat(messages, schemas):
            calls.append(list(messages))
            if len(calls) == 1:
                return {"content": None, "reasoning_content": "provider metadata",
                        "tool_calls": [{"id": "call-1", "type": "function", "function": {
                            "name": "drive_time", "arguments": '{"destination":"school"}'}}]}
            self.assertEqual(messages[-2]["reasoning_content"], "provider metadata")
            self.assertEqual(messages[-1]["tool_call_id"], "call-1")
            return {"content": "You arrive at 15:20."}

        transcript = [{"role": "user", "content": "When do I reach school?"}]
        added, steps = run_turn(transcript, chat, {"drive_time": lambda destination: {"arrive": "15:20"}},
                                datetime(2026, 10, 30, 15), "here")
        self.assertEqual(added[-1]["content"], "You arrive at 15:20.")
        self.assertEqual(steps[0]["result"]["arrive"], "15:20")
        followup = [*transcript, *added, {"role": "user", "content": "And back?"}]

        def next_chat(messages, schemas):
            self.assertEqual(messages[1:], followup)
            return {"content": "Where are you returning to?"}

        run_turn(followup, next_chat, {}, datetime(2026, 10, 30, 15), "here")


if __name__ == "__main__":
    unittest.main()
