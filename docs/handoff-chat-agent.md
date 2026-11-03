# Handoff: chat-style planner for Exagion

Repo: `/home/johngunnarcarlsson/TurnByTurn`, branch `exagion-team-custom-code`.

## Goal
Replace the one-shot request box with a chat the driver can go back and forth with,
including questions such as "If I leave at 3:00, can I be back by 4:30?".

## Already done (uncommitted)
- `backend/nlroute/agent.py`: `run_turn(transcript, chat, tools, now, location_label)`.
  It uses standard OpenAI-style tool calling with three tools: `plan_trip`, `drive_time`
  and `search_places`. It returns `(new_messages, steps)`.
- `backend/server.py`:
  - `chat_with_tools(messages, tools)`: one model call with `tools`.
  - `agent_tools(current, picked, holder)`: the tool functions. `plan_trip` stores the full plan in `holder["plan"]`.
  - Route `POST /api/agent/chat`:
    - request body: `{transcript: [...], currentLocation, picked}`;
    - response: `{messages, reply, steps, plan}`.
- The backend imports cleanly. The model (Beam, OpenAI-compatible, configured in
  `backend/.env.local`) supports tool calling; this was verified.

## Steps, one at a time, verifying after each
1. Restart the backend (`npm start` from the repo root, with `FLASK_USE_RELOADER=0 ROUTING_FRONTEND_PORT=5175`).
   Make sure only one copy is listening on port 5055.
2. Send one test request:
   `curl -s -X POST localhost:5055/api/agent/chat -H 'Content-Type: application/json' -d '{"transcript":[{"role":"user","content":"If I leave at 15:00, when do I reach John Sinnott Elementary?"}],"currentLocation":{"lat":37.4191,"lng":-121.9048}}'`
   Expect a short `reply` and a `drive_time` or `plan_trip` entry in `steps`.
3. Frontend, in `src/PlannerApp.jsx`:
   - Replace the describe `Textarea` with a chat thread. Keep `transcript` in state and render user and assistant messages; skip `tool` messages.
   - On send, POST to `/api/agent/chat` via `apiUrl()` with the whole transcript, `currentLocation()` and `picked: pickedRef.current`.
     Append the returned `messages` to `transcript`.
   - If the response has a `plan`, pass it to the existing result handling so the route draws on the map.
   - Keep the "Where to?" box and its autocomplete as they are.
4. Run `npx vite build`, then `npx cap sync android`, then `cd android && ./gradlew assembleDebug`.
   Use the JDK at `~/.local/opt/jdk-21` as `JAVA_HOME`.
   The phone installs from `https://<your-backend-host>/api/download/turn-by-turn.apk`.

## Rules from John
- Use only times and durations the driver states. Never invent stop times or other defaults.
- Use established open-source tools; no homemade stand-ins.
- Never print or commit `backend/.env.local` (it holds the API key).
- Run tests with `cd backend && venv/bin/python -m unittest test_nlroute test_server` (17 should pass).
