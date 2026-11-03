# Two drivers, one to-do list

"My wife is at Stonestown. Between the two of us, someone has to pick up our daughter at
Alvarado Elementary and get her to swim at Rossi Pool by 2, and someone has to go to the bank
and a CVS. Who should do what?" (demo: **Split the errands with my wife**)

## What happens

1. The model fills `team` in the constraint IR: the partner, where they are (or null = their
   shared location), and the tasks: `escort` (pick up at one place, drop at another, optional
   deadline), `place`, or `errand` (any branch of a type/brand).
2. `nlroute/team.py` resolves the places and finds errand candidates near either person, builds
   one Valhalla matrix, and `solver.solve_team` (OR-Tools) assigns and orders everything:
   - pickup and drop in the same car, pickup first;
   - the drop deadline as a time window;
   - one branch per errand, by whoever is closer;
   - open-ended routes: each person is done after their last task;
   - objective: time both people spend (driving + stops) plus when the last one finishes.
3. Each person's part is routed for real and checked independently (stops reached, deadline met,
   pickup before drop in the same car). The plan where one person does everything is solved
   too, for the "Wife alone: 2:09pm" comparison.

## Sharing location and sending the plan (Glympse-style)

No accounts and no app needed on the partner's side, only a link:

- **Ask for location** mints an unguessable token (30 min) and opens the SMS app with the
  link `…/share.html?t=<token>`. The partner opens it in any phone browser and taps
  **Share my location**; fixes go to `/api/gps?t=<token>`. The planner uses the live position
  when the request doesn't say where the partner is ("use her shared location").
- **Send to Wife** stores the partner's part at `/api/trip/active?who=partner` and texts the
  same link. The page then shows the stops with **Start turn-by-turn** (Ferrostar in the
  browser on the phone's own GPS, mirrored back to the planner's map) and an **Open in Google
  Maps** fallback (loses the planner's exclusions).
- When the token expires, its location is deleted and the link stops working (410).

## Limits

- The partner's phone must reach the backend. On a tailnet that means the partner joins it
  (or the laptop is shared to them); for anyone else the backend needs a public HTTPS address
  (a small server with the Valhalla tiles, or Tailscale Funnel). Everything is in memory.
- A browser shares location only while the page is open (iOS stops in the background).
  During navigation that's fine; background sharing needs the native app.
- Google Maps location sharing can't be read: Google has no API for it.
