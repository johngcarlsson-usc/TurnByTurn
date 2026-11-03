# Natural-Language Routing on an Open Stack

FOSS4G 2026 lightning talk · Wed Nov 4, 15:00–15:25 · Compagno

The deck is `beamer/talk.pdf`, built from `beamer/talk.tex` (default Beamer theme) with
`tectonic beamer/talk.tex`; images come from `screenshots/`. An earlier version is a Claude Slides artifact,
whose source is `deck.json` and `slides/<id>.html`. The script below is that version's speaker notes, slide
by slide; the Beamer frames follow the same order, except that "One pattern" and "The model's only job"
are one frame and the "Where it breaks" divider is a Beamer section instead of a slide.

Every demo slide has a matching scenario in the app (ids in `src/plannerScenarios.js`): open
`/?scenario=<id>&autorun=1` for the checked-in constraints, `&mode=live` to send the spoken request to
the model, or `&drive=1` to go straight into turn-by-turn. The screenshots come from the live path:
`backend/venv/bin/python scripts/capture_talk.py` posts a phone GPS fix at the Great Mall, Milpitas,
runs each demo and saves `screenshots/<id>.png`. See `../driving-demo.md` for the phone-GPS setup.

## Script

### Why toggles aren’t enough, and the pattern: language becomes constraints, constraints become geometry.

**1. Natural-language routing on an open stack**

Hi, I’m [name]. Mainstream maps give you a few toggles and a search box that only understands “near me.” In the next 25 minutes I’ll show how to turn the requests you actually say out loud into real routes, using only open tools and open data: Valhalla, Overture, OpenStreetMap, OR-Tools, and a language model whose only job is filling in a form.

**2. Maps give us toggles. We think in constraints.**

Every mainstream map app exposes routing through a handful of global switches, and a search box that ranks by distance from where you are. But the constraints in our heads are local and specific: one freeway, in one neighborhood. One storm cell. A coffee stop that is on the way, which is not the same as near me. All of these are expressible in plain language, and all of them are computable with open tools. They just fall outside what the products will do.

**3. One pattern, every request**

This is the whole talk in one slide. The language model has exactly one job: turn the sentence into a typed constraint object. Everything after that is deterministic code calling open engines. That split is what makes every decision inspectable on screen, and, as we’ll see, checkable after the fact.

**4. The toolbox: open data, open engines**

Here’s the toolbox, and which demo uses which piece. Valhalla does the routing on a whole-US graph built from OpenStreetMap; we use four of its features: route, matrix, map-matching with trace_attributes, and per-request exclusions. Overture’s places are a GeoParquet file that DuckDB queries in place. Photon and Nominatim turn names into coordinates, locally, from the same OSM data. OR-Tools does the optimization. The language model sits behind a JSON schema; the screenshots were taken with a hosted model, Beam-1.0, and the same five requests also run on a local qwen3.6 35B-A3B through Ollama on this laptop: same stops, every check passing, 18 to 61 seconds each instead of 3 to 14. Getting that to work took three fixes this week, all in the repo. Speeds come from Caltrans PeMS loop detectors, built into Valhalla’s predicted traffic, so a route at 5 pm is not a route at 3 am. Valhalla over OSRM, deliberately: OSRM is faster at matrices, but every request here carries its own rules, and that’s Valhalla’s strength.

**5. The model’s only job**

Here is the actual schema output for: “I need a pharmacy before it closes at six, groceries, and gas, in any order, and I have to be home by 6:15.” Notice what’s not in it. No store names, no coordinates: “a pharmacy” is a category, and choosing which pharmacy is an optimization problem, not a language problem. And no start: origin null means the phone’s GPS. Nobody tells their navigation app “I’m leaving work now”; it knows. Keeping the model’s output this small is what lets a three-billion-active-parameter local model do it reliably.

### How it works, end to end (deck frames 4–10, after “Approach”)

**How one request runs**

Here is the whole pipeline on one slide. The shaded boxes are the only places a language model runs. There are three: the chat agent that decides which tool to call, the extraction step that fills in the form, and, only for requests the form can't express, a step that writes a little route code. Everything in white is ordinary deterministic code. The key box is Dispatch. It doesn't ask the model which algorithm to use. It looks at which fields of the form are filled in and picks one of five methods, and only two of those use OR-Tools. Whatever method runs, the result is one final route, and that route is checked against the form on a fresh map-match before it reaches the phone.

**One request, end to end**

Let's follow one real request through every box: pharmacy before six, groceries, gas, home by 6:15, at 5:15 from the Great Mall. These are the numbers from a live run, not a mock-up. The model calls take about 3 seconds. The whole planner takes 1.4 seconds, and one of those seconds is OR-Tools, because I give it a one-second budget. Search finds 7 pharmacies, 27 grocery stores and 15 gas stations along the way, and we keep the best 6 of each. The full matrix is 20 by 20 and takes a third of a second. Six point nine seconds from sentence to verified route.

**Steps 1–2: the two model calls**

The first call is a chat agent with three tools. It decides what kind of request this is, a trip, a question about places, or a "can I make it" question, and here it calls plan_trip. Notice that its prompt tells it not to choose stores: if it searched for a Walgreens and put that in the request, the optimizer would never get to choose. The second call fills the form. The API enforces the JSON schema, so the output always parses, and then a sanitizer clamps numbers and checks enums. What comes out is on the right: three errands, a closing time, a deadline, any order. No coordinates and no store names.

**Step 3: dispatch is ordinary code**

This is the answer to "how does it decide when to use OR-Tools." It doesn't decide; this code does. The model answered small questions about the sentence: is there a friend to meet, are two people splitting tasks, did they give an order. Each answer is a field, and this if-chain turns fields into a method. That matters for debugging: if the wrong solver ran, exactly one field was wrong, and you can see it, because the app shows the form.

**What each field compiles to**

And this is how the form becomes mathematics. An errand becomes a set of candidate nodes and one disjunction: visit exactly one of these. A closing time is an upper bound on a time variable. "Stay off 880" removes edges from the graph. Picking up our daughter becomes a pickup-and-delivery pair forced into the same car. The point of the bottom line: new combinations of these cost nothing, because they're all the same underlying model. A genuinely new kind of constraint, opening hours for example, means a new field and its translation, and that's code a person writes and reviews.

**Steps 4–8: from words to a 20-node instance**

Now the geography. DuckDB queries Overture's GeoParquet in place for pharmacies within 2 km of the base route. Two thin matrices, start to every candidate and every candidate to home, give each candidate its detour. We keep six per errand, so the instance is start, home and 18 candidates: 20 nodes. One full matrix from Valhalla. There are 1296 possible tours here: six choices per errand, cubed, times six orders. And look at the groceries: the closest one on its own isn't the one we end up using, because the solver sees how the stops interact.

**Steps 9–10: solve, then check independently**

The OR-Tools model is small: one vehicle, three groups of six with "exactly one" disjunctions, a 45-minute bound on the pharmacy nodes and 60 minutes on the end. It starts from the cheapest-arc construction and runs guided local search for one second. The answer: Nilgiris, Walgreens, 76, home, 10.4 minutes against 7.2 direct. Two habits worth stealing. We recompute the cost from the matrix instead of trusting the solver's objective. And the checks run on a fresh map-match of the final line, not on the solver's state: pharmacy at 5:22, home at 5:25.

### Optimization background (deck frames 12–16, after “Tools”)

**Operations research in one slide**

For those of you who haven't met operations research: it's the business of writing down a decision as a model. Variables for what you choose, constraints for what's allowed, an objective for what you want. The modelling is the hard part; the solvers behind it are largely interchangeable. Linear programs, integer programs, constraint programming and local search are the main families. Airlines schedule crews this way, and logistics companies route trucks this way. Most routing problems are NP-hard, so in practice you run a good heuristic with a time limit and accept that you don't get a proof of optimality.

**Vehicle routing: a family of problems**

Vehicle routing isn't one problem, it's a family. The traveling salesman tour; several vehicles; time windows at each stop; pickups that must precede their deliveries in the same vehicle. Two members matter for us. The generalized TSP, on the right: the nodes come in clusters, here pharmacies, groceries, gas stations, and you must visit exactly one per cluster, choosing which one and the order at the same time. And prize-collecting: nodes are optional, but skipping one costs a penalty. Our errands are exactly a generalized TSP with time windows.

**Google OR-Tools**

OR-Tools is Google's open-source optimization suite, Apache-licensed, a C++ core with Python, Java and C# bindings. It's broader than routing. CP-SAT is its constraint-programming solver and has won many golds in the MiniZinc Challenge. There's one API over linear and integer solvers, Google's own GLOP and PDLP, SCIP, HiGHS, and commercial solvers if you have them. There are classic graph algorithms. We only use the routing library, but the same pip install would schedule shifts, pack bins or assign tasks.

**The routing library's vocabulary**

Here's the whole vocabulary, in code you could run. An arc cost is a callback, and for us that's the Valhalla matrix. A dimension is a quantity that accumulates along a route, like time or load, with a variable at every node, so time windows and capacities are just bounds on those variables. A disjunction says "at most k of these nodes, and here's the penalty for visiting none," which gives you both optional stops and pick-one-of-a-cluster. Then pickup and delivery, start and end per vehicle, and a span cost on the longest route. The search builds a first solution and improves it with a metaheuristic until the clock runs out.

**Our five requests, in this vocabulary**

So where do our five demos sit? The first three never touch OR-Tools. Gas on the right is the best of a filtered candidate set. Coffee an hour in is the same with a time filter. Avoiding 880 is a shortest path on a graph with edges removed, repeated until it holds. Only demos four and five need a real optimizer, because only there do the choice of store and the order interact. When the driver gives the order, even with candidates, it's a layered shortest path, exact and instant. And demo four has 1296 tours: local search gets a good one in a second, but notice what the verifier checks. It checks the deadlines, not optimality.

### Five live requests from a phone at the Great Mall, Milpitas, each exercising a different open tool.

**6. Five requests from the Great Mall, Milpitas**

Five requests, all from one spot: the Great Mall in Milpitas, about an hour from this room. The phone sends its GPS fix the way any phone would, and that fix is the start of every trip, so none of these sentences says where I am. Each one exercises a different open tool. And none of this is canned: every screenshot in this section came from the live app, with the sentence going to the model, and the script that took them is in the repo.

**7. “Gas on the right, on the way”**
<sub>Screenshot: `screenshots/gm-gas-right.png`</sub>

“Take me to Levi’s Stadium, and get gas on the way at a station on the right so I don’t have to turn across traffic.” The model sets side: right on a gas errand; nothing else. DuckDB queries the Overture GeoParquet directly, no import, for stations within 80 meters of the road on the right of travel. Which side of the plain route a station sits on is only a hint, because the final route can reach it another way, so the check is geometric: the final route’s heading over its last 15 meters into the stop. If you’d arrive with it on your left, it’s rejected and the planner picks again. Here: a 76 on West Calaveras, half a minute out of the way. Same request to San Jose airport gives an honest no: the route is freeway the whole way, and nothing fronts a freeway.

**8. “Coffee about an hour from now”**
<sub>Screenshot: `screenshots/gm-coffee-hour.png`</sub>

“I’m driving to the Sheraton in downtown Sacramento, and I’m going to want coffee about an hour from now.” That’s this conference’s hotel. “An hour from now” is a time, not a place, so the model writes afterMinutes: 60, and code turns it into geometry: Valhalla’s maneuver times say which stretch of road you’ll be on between 54 and 66 minutes in. That stretch is the Benicia bridge and the freeway around it, with no café within two kilometers, so the search widens to plus or minus twelve minutes and says so on screen instead of quietly loosening. Then a Valhalla matrix gives every candidate’s real detour, and the smallest wins: a Peet’s in Pleasant Hill, a third of a minute off the route. The verifier checks the stop is actually reached inside the window it searched. An earlier run picked “Gano Excel” in Fairfield, tagged as a coffee shop; more on that later.

**9. Distance prefilters, minutes decide**

Under the hood of the first two demos. The search is two stages. First a cheap prefilter: DuckDB reads Overture’s GeoParquet file directly, no database, no import, with a bounding box around the route and a category filter; “coffee” maps to two Overture categories, and that table lives in code, not in the model. Shapely then gives each hit its distance to the route line and its position along it, which is what the “about an hour in” window and the right-side check use. Straight-line distance is only a prefilter, because a café 200 meters from a freeway can be ten minutes away. The decision is in minutes: detour equals time from start to the place, plus place to destination, minus the direct trip. Instead of one full n-by-n matrix, two thin ones, start to every candidate and every candidate to the destination, are enough to rank them. The best six per errand go to the next stage. If asked “why not PostGIS?”: nothing here needs a server; a 273k-row parquet file and DuckDB answer in milliseconds on a laptop.

**10. “Oakland airport, but stay off 880”**
<sub>Screenshot: `screenshots/gm-no-880.png`</sub>

“Get me to Oakland airport, but stay off 880.” Avoiding a road isn’t a toggle; it’s a predicate over graph edges. The loop: route, map-match the line back onto edges with trace_attributes, find every edge that violates the rule, add those to exclude_locations, route again, until a pass comes back clean. Here it goes up 680 and over 580: 63 minutes, twenty more than the plain route along 880, shown dotted in red so you can see the cost of the request. A confession: the first time I ran this, “Oakland airport” resolved to the Oakland Airport Executive Hotel, because Photon ranks a name match above the airport. Now, when a request names a kind of place, airport, school, hospital, a hit whose OpenStreetMap tag actually is that kind comes first. The same bug was sending “San Jose airport” to the San Jose Airport Inn.

**11. Avoidance is a loop, not a toggle**

Under the hood of the 880 demo. Mathematically it’s a fixpoint iteration on the set of excluded points. Valhalla can’t take a rule like “no edge named I-880”; it takes points to avoid, and it avoids the graph edges those points snap to. So: route; map-match the resulting line back onto edges with trace_attributes, which gives every edge’s names, road class and shape; test each edge against the rule; add the midpoint of every violating edge to the excluded set; route again. The set only grows, so it can’t cycle, and it’s capped at eight rounds. One pass is rarely enough in general: block one freeway and the router finds the next one, which is why “skip the downtown freeway” took three passes in San Francisco. Here two were enough: the first route was 43.5 minutes with 122 edges on I-880, also named the Nimitz Freeway; the second, 63.1 minutes, clean. Both routing passes together took 121 milliseconds. If the loop gets stuck, it stops, and the independent verifier reports the violation instead of hiding it. The rule itself is a small class: a name match on the edge’s names, optionally limited to an area polygon.

**12. “Three errands, any order, home by 6:15”**
<sub>Screenshot: `screenshots/gm-errands.png`</sub>

“I need a pharmacy before it closes at six, groceries, and gas, in any order, and I have to be home by 6:15.” No “leaving work now”: the phone knows where I am. “A pharmacy” has hundreds of answers, so choosing one is optimization, not language. The planner screens candidates by real detour with two thin matrices, keeps six per errand, builds one Valhalla matrix over all of them, and hands OR-Tools a generalized traveling-salesman problem. Each errand is a group with one disjunction, maximum cardinality one, so the solver visits exactly one pharmacy, one grocery, one station, in whatever order is fastest. The 6 pm close and the 6:15 deadline are limits on the time dimension’s cumulative variables. The verifier then reads the clock off the final route: pharmacy at 5:23, home at 5:25. Ordered requests, “first coffee, then…”, need no solver at all: a shortest path through layers is exact.

**13. Errands: a generalized TSP**

Under the hood of the errands demo. On the left is the model as a mathematician would write it: minimize total travel time over arcs, visit exactly one node from each group, the pharmacy group, the grocery group and the gas group, and keep arrival times consistent: arriving at j is at least arriving at i plus the travel time plus any stop time. The pharmacy’s closing time and the deadline at home are upper bounds on those arrival times. On the right is the same thing in OR-Tools’ routing library. The time dimension is the T variables; CumulVar is T at a node, and SetMax is the window. “Exactly one per group” is a disjunction: OR-Tools may visit at most one node of the group, and skipping the whole group costs ten million, so it only happens when nothing fits; the code then reports it as infeasible rather than returning a route that quietly skips the pharmacy. Twenty nodes here: eighteen shortlisted stores plus start and home. The matrix took 0.4 seconds; the solver is an anytime local search, so it uses its whole one-second budget. Afterwards the chosen path is re-costed from the matrix; we never trust the solver’s objective. If someone asks why not an exact MIP: at this size you could, and an ordered request does get an exact layered shortest path; local search keeps latency flat as requests grow.

**14. “Between the two of us… who does what?”**
<sub>Screenshot: `screenshots/gm-team.png`</sub>

“My wife is at the Fremont BART station. Between the two of us, someone has to pick up our daughter at Stratford School and get her home by 3:30, someone has to do the grocery shopping, which takes about half an hour, and someone has to stop at a CVS. Who should do what?” This is a small vehicle-routing problem. Two vehicles, each starting where that person is, each done after their last stop. The school pickup and the drop at home are a pickup-and-delivery pair: same vehicle, pickup first, and 3:30 is a window on the drop. Groceries and CVS are disjunctions over candidates near either of us. Stop times are only what was said: thirty minutes for groceries because it was in the sentence, zero otherwise. Result: I do CVS and the school run and we’re home at 3:15; she does Whole Foods near BART. Both done by 3:34. Either of us alone finishes 3:51 or 4:11 and misses 3:30. One fix this needed: the chat agent first answered “who should do what” itself, in prose. It was plausible and unverified. Now that question always goes to the solver.

**15. Two drivers, one list**

Under the hood of the split. It’s the same model with two vehicles. Each starts where that person is: me at the Great Mall, my wife at Fremont BART. Neither has to go anywhere at the end, so every route ends at a virtual node that costs nothing to reach; that makes the routes open. The school pickup and the drop at home are a pickup-and-delivery pair: AddPickupAndDelivery, plus two explicit constraints, same vehicle, and pickup before drop. The 3:30 deadline is an upper bound on the arrival time at the drop. Groceries and CVS are disjunctions over candidates near either of us. The objective has two parts: total time both of us spend, driving plus stops, and the global span, which is the time from the start to when the last person finishes. That second term is what makes splitting the work worthwhile; without it, a plan that only shuffles minutes from one car to the other would look just as good. Stop times come only from the sentence: thirty minutes for groceries because I said so, zero otherwise. Twenty nodes, a 0.3-second matrix, two seconds of search. The plan where one person does everything is solved too, which is where “either alone: 3:51 or 4:11” comes from.

**16. “Send to Wife”: a link, not an account**
<sub>Screenshot: `screenshots/gm-team-phone.png`</sub>

Then “Send to Wife.” No accounts, no app on her side, just a link. It carries an unguessable token that lasts thirty minutes. Her phone opens a plain web page showing her part: one stop, Whole Foods, done by 3:34. “Start turn-by-turn” runs Ferrostar’s navigation core, compiled from Rust to WebAssembly, in her browser on her phone’s own GPS, and it uses the plan’s exact route request, so a reroute keeps every exclusion the planner found. The same page can share her location back, so the next plan can start from where she really is. When the token expires, her position is deleted and the link answers 410 Gone. Limits: a browser shares location only while the page is open, and Google Maps location sharing can’t be read at all; there’s no API for it.

### Every route is checked independently of the planner.

**17. Never trust the planner. Check the line.**

Every demo you saw ended with an “independent check” panel. That’s not decoration. The most dangerous failures in this system are silent: an exclusion polygon Valhalla rejected, a stop the solver quietly skipped, a deadline it couldn’t meet. They all produce a plausible-looking blue line. So the final line is map-matched again from scratch and every constraint is re-checked against those edges. In earlier benchmarking with OR-Tools code generation, a small model plus a verifier loop beat an 80-billion-parameter model on accuracy and wall clock. Same idea here.

### Where it breaks: data, engines, language, and the limits of constraints.

**18. Where it breaks**

Now the honest part. Everything I’ve shown works. Here’s what doesn’t, or what only works because of a patch I want you to see.

**19. The verifier can’t catch a wrong category**

The first time I ran the errands demo, it sent me to Oportun for my prescription. Oportun is a lending company; Overture tags about twenty of its Bay Area sites as pharmacies at 0.92 to 0.99 confidence. Nothing downstream can catch that: the solver did its job, the verifier confirms the route stops at the place labeled pharmacy. Semantic errors in open data pass every geometric check. The patch is a deny list in the code with a comment explaining why. It’s ugly on purpose, so it’s visible. It grew this week: the Sacramento coffee demo first stopped at Gano Excel in Fairfield, “Unit C” of a business park, tagged as a coffee shop. It’s a company that sells packaged coffee.

**20. Silent failures inside the engine**

Two engine-level gotchas. First, Valhalla’s matrix algorithm silently returns null for about two percent of real places: in a 62-by-62 errand matrix, one Whole Foods produced 122 nulls, its entire row and column. It snapped onto a 23-meter parking aisle that isn’t a dead end and has no access restriction; a normal route reaches it fine. Treat null as unreachable and that store silently disappears from the candidate set. Snapping just those points to the nearest street fixed all eight in my sample. Second, the default service limit on exclusion polygons is ten kilometers of total perimeter, so a couple of storm cells blows through it.

**21. When the answer is no, say no, with the number**
<sub>Screenshot: `screenshots/breaks-deadline.png`</sub>

Over-constrained requests are where a system shows its character. The easy failure mode is to drop the constraint that doesn’t fit and return a nice route. Here, at 4:55, three errands and home by five can’t fit. When OR-Tools can’t meet the time windows, it re-solves without them, the verifier marks the deadline as failed with the actual arrival, 5:05, and the assistant asks which stop to drop. When no candidate meets a detour bound, the answer is a sentence with the closest real number.

**22. What constraints can’t express yet**

Ambiguous language is the long tail. “Downtown” has no boundary in OpenStreetMap that a router can use, so it’s a circle I drew. Named roads lose their names in routing graphs. Overture has no opening hours, so “before it closes” only works if you say when. Geocoders rank names, not meaning: “Oakland airport” matched the Oakland Airport Executive Hotel until the kind of place had to match the OpenStreetMap tag. And the deepest one: my Bay Bridge bug passed the verifier because both used the same wrong definition. Checks must come from independent data, not just independent code.

### Takeaways and thanks.

**23. Three things to take home**

If you remember three things. One: keep the model’s job small and typed; that’s what makes a local model reliable and every decision visible. Two: “avoid X” is a predicate over map-matched edges, checked in a loop, because routers route around exclusions in ways you won’t predict. Three: check the final line independently, and when a constraint can’t be met, say so with the number.

**24. Thank you**

Thanks. The code is github.com/johngcarlsson-usc/TurnByTurn. Happy to take questions, and if you want to try a request, I can run it live.
