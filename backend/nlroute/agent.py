"""The chat agent: a conversation with the model, which calls the planner as tools.

Standard tool calling (OpenAI-style `tools` / `tool_calls`): each turn the
model either answers or asks for tools; their results go back to it until it
answers. Every fact about places, times and routes comes from a tool, so the
answer is as verified as the planner itself.
"""

import json

MAX_ROUNDS = 6

SYSTEM_PROMPT = """You are Exagion, a driving assistant. The driver talks to you on their phone.
Now: {now}. Driver's location: {location}.

Use the tools for every fact about places, travel times and routes; never estimate them yourself.
- To go somewhere or change the trip, call plan_trip with the whole request in the driver's words,
  folding in what was said earlier (e.g. "to John Sinnott Elementary, stop at a Jack in the Box").
  The route then appears on the driver's map.
- For "what if" and "can I make it" questions, call drive_time or plan_trip with depart_at, and
  answer from the times they return.
- Errands ("a pharmacy", "groceries", "gas", "coffee on the way") go to plan_trip in the driver's own
  words: it chooses which store and the order with an optimizer and checks the route. Don't search
  for stores and name them in the request yourself; that skips the optimizer.
- To find or compare places when the driver asks about places (not a trip), call search_places.
- When two people split a list of jobs ("between the two of us", "who should do what", "my wife and
  I"), call plan_trip with the whole request: it decides who does what and in which order (an
  optimizer, with both routes checked). Never work out the split yourself.
- "home" and "work" are the driver's saved places; use them as given, don't ask for an address.
Never add time at stops unless the driver said how long. If a tool returns a "question", ask the
driver exactly that. Answer in one to three short sentences: they may be about to drive."""

TOOLS = [
    {"type": "function", "function": {
        "name": "plan_trip",
        "description": "Plan and verify a route from the driver's request (destination, stops, things to "
                       "avoid, arrival times) and show it on the map, including splitting jobs between the "
                       "driver and someone else. Returns times, distance and checks.",
        "parameters": {"type": "object", "properties": {
            "request": {"type": "string", "description": "The full trip request in plain words."},
            "depart_at": {"type": ["string", "null"], "description": "Departure time HH:MM (24h) if not now."},
        }, "required": ["request"]}}},
    {"type": "function", "function": {
        "name": "drive_time",
        "description": "Driving time and distance between two places, for a departure time.",
        "parameters": {"type": "object", "properties": {
            "origin": {"type": "string", "description": "A place, or \"current location\"."},
            "destination": {"type": "string"},
            "depart_at": {"type": ["string", "null"], "description": "HH:MM (24h); omit for now."},
        }, "required": ["origin", "destination"]}}},
    {"type": "function", "function": {
        "name": "search_places",
        "description": "Find places by name or kind (\"Peet's\", \"gas station\"), nearest first.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"},
            "near": {"type": ["string", "null"], "description": "A place to search around; omit for the driver."},
        }, "required": ["query"]}}},
]


def run_turn(transcript, chat, tools, now, location_label):
    """Answer the driver's latest message.

    transcript: prior user/assistant/tool messages, ending with the driver's.
    chat(messages, tool_schemas) -> assistant message; tools: {name: function}.
    Returns (new messages to append to the transcript, steps for the UI).
    """
    system = {"role": "system",
              "content": SYSTEM_PROMPT.format(now=now.strftime("%A %H:%M"), location=location_label)}
    messages = [system, *transcript]
    added, steps = [], []
    for _ in range(MAX_ROUNDS):
        reply = chat(messages, TOOLS)
        reply = {"role": "assistant", "content": reply.get("content") or "",
                 **({"reasoning_content": reply["reasoning_content"]} if "reasoning_content" in reply else {}),
                 **({"tool_calls": reply["tool_calls"]} if reply.get("tool_calls") else {})}
        messages.append(reply)
        added.append(reply)
        if not reply.get("tool_calls"):
            return added, steps
        for call in reply["tool_calls"]:
            name = call["function"]["name"]
            args = {}
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
                result = tools[name](**args) if name in tools else {"error": f"no tool named {name}"}
            except Exception as error:  # the model sees the failure and can recover or ask
                args = args if isinstance(args, dict) else {}
                result = {"error": f"{type(error).__name__}: {error}"}
            steps.append({"tool": name, "args": args, "result": result})
            message = {"role": "tool", "tool_call_id": call.get("id"), "content": json.dumps(result)}
            messages.append(message)
            added.append(message)
    added.append({"role": "assistant", "content": "Sorry, I couldn't work that out. Could you rephrase?"})
    return added, steps
