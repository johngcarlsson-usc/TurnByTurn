# Live traffic (outside data, kept separate)

This package is the only part of Exagion that pulls outside data at runtime.
Its single output is Valhalla's `traffic.tar`. The planner reads the
status file only to decide whether to ask Valhalla for live speeds. It follows
the design in the "Free realtime traffic for navigation" research doc: the prior,
then live corrections, then fusion.

| Source | Module | Cadence | Gives | Key |
|---|---|---|---|---|
| Google Maps traffic-tile colors (research use) | `google_tiles.py` | 5 min | congestion level 1–4, about 68k road segments | none |
| TomTom vector flow tiles, z11 | `tomtom.py` | 15 min | speed ratio 0–1 and closures, about 149k road segments; 52 tiles a sweep, about 5k a day | `FT_TOMTOM_API_KEY` |
| CHP CAD incidents (`media.chp.ca.gov/sa_xml/sa.xml`) | `chp.py` | 2 min | slowdowns and closures by road and direction | none |
| 511 SF Bay events (`api.511.org/traffic/events`) | `sf511.py` | 5 min | official lane closures by road and direction | `FT_SF511_API_KEY` |
| Caltrans Lane Closure System (`cwwp2.dot.ca.gov`, districts 4 and 5) | `caltrans_lcs.py` | 5 min | planned closures in place (crew code 1097), matched begin to end along the route and direction; ramp closures only when the record has a real extent | none |
| Caltrans PeMS loop detectors (prior, not live) | `pems.py` | once per few months | typical speed for each hour of the week on 6,536 freeway segments (84% of numbered Bay Area freeway mainline; detectors up to 20 km away on the same freeway and direction), built into the Valhalla tiles | `PEMS_USER`, `PEMS_PASSWORD` |
| Caltrans District 4 changeable message signs (`cwwp2.dot.ca.gov/data/d4/cms`) | `caltrans_cms.py` | 2 min | about 117 freeway travel times on about 40 signs ("SF DWNTWN 22 MIN"; HERE probe data bought by 511, per the 511 FAQ); each sign compared with its own overnight low; corridors from Photon + Valhalla | none |
| 511 SF Bay regional GTFS-realtime bus positions (`api.511.org/transit/vehiclepositions`) | `transit.py` | 2 min | about 1,900 vehicles; **logged only** (history for a future street prior: a bus's time on a stretch vs buses' usual time there), not used in routing yet | `FT_SF511_API_KEY` |
| 511 SF Bay express-lane toll prices (`api.511.org/toll/programs`) | `toll.py` | 15 min | about 180 dynamic toll prices on I-680, I-880, I-80, US-101 (San Mateo), set from lane congestion; weekdays only; the feed's travel-time field is 0 everywhere; **logged only** | `FT_SF511_API_KEY` |
| Caltrans District 4 camera video (`cwwp2.dot.ca.gov/data/d4/cctv`) | `cctv.py` | 5 min | vehicle speeds from about 110 live HLS streams (of about 195 listed): open-weight detector + tracker, each vehicle's own length as the ruler; **logged only** | none |
| Waze live-map routing | not used | | returns 403 from this connection; not worked around | |

Keys live in the gitignored `backend/.env.local`.

## Two versions: open and full
`run.py --profile open` writes `traffic-open.tar` (served by a second Valhalla, `valhalla-open` on :8003)
and `status-open.json`; the default `full` profile writes `traffic.tar`. The benchmark scores both.

| Source | Terms (checked 2026-10-31) | At scale | Version |
|---|---|---|---|
| PeMS clearinghouse | free account; Caltrans data is public domain unless noted (dot.ca.gov/conditions-of-use); the PeMS-specific terms are behind its login, unread | yes (offline prior) | open |
| CHP CAD | public page; CHP's terms page returned 403, unread | yes, poll once and serve from our side | open |
| 511 SF Bay (events, GTFS-RT, tolls) | 2026 Data Disseminator Agreement allows derivative works for end users; "powered by 511.org" + link; tell MTC within 30 days of launch; 60 requests/hour by default, more on request | yes, Bay Area | open |
| Caltrans LCS, message signs, cameras (CWWP2) | Caltrans Conditions of Use; "no charge"; sign times are HERE-derived, so third-party rights may apply | yes, statewide | open |
| TomTom flow tiles | commercial use allowed, also on the free tier; 200k tiles/month free, then paid; storage of derived speeds unconfirmed (T&C not readable) | paid | full |
| Google tiles and Maps times | prohibited: Maps Additional Terms §2 (no bulk download, no new product or dataset from Google Maps), Google ToS (automated access) | no | full (research only) |
| Waze | prohibited (Waze terms: no automated access); 403 here, not worked around | no | not used |

No public real-time feed of Caltrans loop-detector speeds was found: PeMS shows them only behind its
login, QuickMap's speed layer is Google's, and CWWP2 travel times skip District 4.

## Combining the sources (per road segment)
1. A closure from any source closes the segment. CHP and 511 give one point per incident, so they close
   only the segments within 250 m of it; the 800 m queue behind is slowed (0.3), not closed, so exits
   before it stay usable. A planned Caltrans closure is overruled where both
   TomTom and Google measure moving traffic (except on ramps, where tile samples can't tell the ramp from the freeway beside it) (the status file counts these as `lcsClosuresOverruledEdges`).
2. Measured speed, **on freeway mainline only** (motorway and trunk, not ramps): **free-flow speed** (PeMS night speed, else TomTom's free flow from `python -m livetraffic.tomtom freeflow`, else Valhalla's typical) times the mean of TomTom's
   ratio and the Google color's ratio. Surface streets keep Valhalla's typical speed, which already
   includes signal and intersection delay (secondary streets: 25 km/h in Valhalla vs TomTom's 45 free
   flow), so a ratio on top counted rush hour twice. Re-scoring 187 trips against Google Maps:
   median error 22% with live speeds on every road, 10% with freeway mainline only. Checked on 239
   new trips (seed 2, Friday 4:40-5 PM): live 11% median error, 173 of 239 within 20% of Google;
   PeMS prior alone 12%, 167; typical speeds 17%, 128. The ratio used to multiply Valhalla's typical
   speed, which on freeways is already below free flow, so part of the slowdown was counted twice;
   re-scoring 479 trips (Friday 6:30-6:50 PM) with free flow as the base: median error 9.4% -> 8.7%,
   within 20% of Google 389 -> 419, mean error +2.1 -> -0.8 min. Each color's ratio is
   **calibrated every cycle** as the median TomTom ratio on motorway mainline segments (not ramps) both sources cover.
   On 2026-10-30 that gave green 1.0, yellow 0.755, red 0.446. Ratios are kept in order.
3. Incident slowdowns (CHP, 511, Caltrans) apply only where nothing is measured.
4. Otherwise the segment is left unknown, so Valhalla uses the PeMS time-of-week speed where there is
   one, else its typical speed.

## Pieces
- `catalog.py`: a one-time list of the major Bay Area roads (pyosmium on the California OSM extract,
  map-matched with Valhalla `/trace_attributes`, keeping each segment's use: road, ramp, ...). Rebuild it whenever the Valhalla tiles are rebuilt,
  because Valhalla's segment IDs change.
- `valhalla_traffic.py`: in-place writer for `traffic.tar`, laid out exactly as `valhalla/baldr/traffictile.h`.
- `pems.py`: the time-of-week prior. Medians per hour of the week per mainline detector (hours at
  least 50% observed, holidays left out), matched to segments on the same freeway and signed direction
  (PeMS "E" on I-80 runs north in Berkeley), written as Valhalla predicted-traffic CSVs with the
  codec of `valhalla/baldr/predictedspeeds.cc`. 5-minute profiles from `station_5min` files
  (`download ... --kind station_5min`, `five_min`, `profiles_5min`) were tried and not adopted: on held-out
  2026-09-10..30 they cut weekday-peak median error only from 2.96% to 2.88% after the codec, which
  cannot hold detail shorter than about 50 minutes.
- `benchmark.py`: random, repeatable Bay Area trips (`--seed`); Google Maps' driving time read with
  headless Chromium vs our live, predicted (PeMS only) and typical times at the same moment. Results in
  `~/geo/livetraffic/bench/`. Test-only dependency: `pip install playwright && playwright install chromium`.
- `benchmark.py --fit <jsonl...>`: the research doc's "ETA inversion" in its simplest form. Each live
  route's time is split by road class (Valhalla `/trace_attributes` with the route's `date_time`, whose
  per-node elapsed times add up to the route's time exactly), and one multiplier per class's base time
  plus one on the delay live traffic adds is fitted to Google's times (ridge, pulled toward no
  correction), scored by 5-fold cross-validation on held-out trips only. First run (478 trips, Friday
  6:30-6:50 PM): live delay x0.39, freeway base x1.11, streets x0.80-0.87; held-out median error 9.1%
  vs 10.0% uncorrected, 397 vs 361 within 20%. One evening only, so it is not applied to routing.
- `forecast.py`: LightGBM forecasts of each message sign's time 15/30/60 minutes ahead, trained on the
  sign history (each sign labels itself: its own later reading), with the corridor's other inputs logged
  alongside every reading. Scored on held-out days against "the future equals now" and the usual time
  for that hour of the week. Needs about 2 weeks of history (logging began 2026-10-31).
- `detectors.py`: next-day scoring of every logged source against PeMS loop detectors (each day's
  station_5min file is posted the next day), with the time-of-week prior and free flow as baselines on
  the same points. Free ground truth for freeways that does not involve Google.
- `fusion.py`: LightGBM from every logged source (prior, signs, cameras, tolls, incidents; full adds
  TomTom and Google) to the speed each freeway detector measured, scored on held-out data against the
  prior. First run (full features, Friday 2026-10-30 only, hour blocks held out, so optimistic): mean
  error 3.77 mph with the prior alone, 2.76 fused; where the prior was 10+ mph off, 20.6 -> 9.8.
  The run loop logs every per-edge source (including incidents) so the open version can be trained too.
- `run.py`: polls the sources, combines them per road segment as described above,
  and writes the file and `~/geo/livetraffic/status.json`.

## Assumptions (tunable, uncalibrated)
- Starting color ratios before any calibration: `run.DEFAULT_LEVEL_RATIO`.
- Incident impact by type: `chp.IMPACT` and `sf511.STATE_IMPACT` (Caltrans closures reuse the 511 values). No published model exists.
- Surface streets: TomTom z11 lines are simplified, so street samples can hit a cross street,
  which is why calibration uses motorways only.

## Setup and use
1. Build the empty traffic file once, on the existing Valhalla tiles:
   `docker exec valhalla-usa valhalla_build_extract -c /custom_files/valhalla.json -e /custom_files/tmp.tar --with-traffic --overwrite`.
   Then delete `tmp.tar`, give `traffic.tar` to your user, and restart Valhalla.
2. `venv/bin/python -m livetraffic.catalog ~/geo/nominatim/california-latest.osm.pbf`
3. PeMS prior (after any tile rebuild, since segment IDs change):
   - `venv/bin/python -m livetraffic.pems download 2026-07 2026-08 2026-09`
   - `venv/bin/python -m livetraffic.pems build ~/geo/valhalla-predicted`, then copy it to `custom_files/predicted`
   - unpack `valhalla_tiles.tar` into `custom_files/valhalla_tiles`, then run
     `valhalla_add_predicted_traffic -c <config without tile_extract/traffic_extract> /custom_files/predicted`
     and `valhalla_build_extract -c <same config, tile_extract = new tar> -O`
   - swap the new tar in and restart Valhalla. `traffic.tar` stays valid (segment counts don't change).
4. Planner matrices: set `service_limits.max_timedep_distance_matrix` (200000 here) in `valhalla.json`.
   With the default 0, `/sources_to_targets` silently ignores `date_time`, so the planner's stop choice
   and order used typical speeds while the final route used live ones (Bay Bridge pair, Friday rush:
   matrix 16.4 min, route 24.1).
5. `venv/bin/python -m livetraffic.run` loops; add `--once` for a single cycle, or `--clear` to wipe live data.

## Running it unattended
`systemd/install.sh` installs the full and open loops, the camera logger, Photon and a nightly timer
(`nightly.py`: bus observations, PeMS detector scoring and fusion retraining once each day's PeMS file is
posted; 3 PM with a 9 PM retry) as user services that survive logout and reboot (it enables linger). Logs:
`~/geo/livetraffic/{run,run-open,cctv,nightly}.log`.

On another machine: clone the repo, copy `backend/.env.local` (secrets), copy the same
`valhalla_tiles.tar` (every logged segment id depends on it) and `traffic*.tar`, `~/geo/photon`,
`~/geo/pems`, `~/geo/gtfs`, `~/geo/livetraffic` (about 25 GB), run the two Valhalla containers, make
`backend/venv` (requirements.txt) and `~/geo/cctv/venv` (torch, torchvision, transformers,
opencv-python-headless, supervision, trackers, pillow, requests, numpy; on Pascal GPUs use the cu126 torch
wheels), then `install.sh` (or `install.sh exagion-livetraffic exagion-livetraffic-open exagion-cctv` where
Photon already runs). Run one collector per set of keys: 511 allows 60 requests an hour per key.

Per-machine settings go in `~/.config/exagion.env`, which every unit reads if present, e.g.
`VALHALLA_BASE_URL=http://127.0.0.1:8012`, `VALHALLA_OPEN_URL=http://127.0.0.1:8013` (Valhalla ports),
`CCTV_FPS=5` and `CCTV_SECONDS=6` (fewer camera frames on a CPU without CUDA).

Building instead of copying (when 20 GB over the network is too much): a new graph gives new segment ids,
so on the new machine build the tiles, then the catalog, the PeMS prior, TomTom free flow and the
traffic files there (Setup steps 1-3 above, `python -m livetraffic.tomtom freeflow`); sign corridors
rebuild themselves. History keyed by segment id (TomTom, Google, incidents) does not carry over; signs,
tolls, cameras and bus positions do.

## Switching it off
- Set `LIVE_TRAFFIC=0` for the backend, and every route uses typical speeds and says so.
- Live data older than 10 minutes, or with no healthy source, is ignored automatically.
- Each plan's `traffic` field says which speeds the route used.
