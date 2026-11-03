"""The live-traffic loop: poll sources, fuse per edge, write Valhalla's traffic.tar.

    venv/bin/python -m livetraffic.run            # loop forever
    venv/bin/python -m livetraffic.run --once     # one cycle of every source
    venv/bin/python -m livetraffic.run --clear    # wipe live data (routes fall back to typical speeds)
    venv/bin/python -m livetraffic.run --profile open   # government/open sources only (see PROFILES)

Fusion per edge, in order:
  1. a closure from any source (CHP, 511, TomTom road_closure) closes the edge;
     a planned Caltrans closure (LCS) does too, unless both TomTom and Google
     measure moving traffic on it right now (not applied to ramps);
  2. on freeway mainline only (motorway/trunk, not ramps), measured speed:
     free-flow speed x the mean of TomTom's ratio, the Google color's ratio and
     the Caltrans message-sign ratio (all fractions of free flow). Free flow is the PeMS night speed, else
     TomTom's, else Valhalla's typical speed. Valhalla's typical freeway speed
     is already below free flow, so a ratio on it counted part of the slowdown
     twice (478 trips vs Google, 2026-10-30 evening, re-scored: median error
     9.4% -> 8.7%, within 20% 389 -> 419). Each color's
     ratio is calibrated every cycle against TomTom on the motorway edges both
     cover (the research doc's "TomTom as calibration target"). Surface streets
     keep Valhalla's typical speed, which already includes signal and
     intersection delay: a ratio to free flow on top of it counted the
     congestion twice (benchmark vs Google, 2026-10-30: median error 22% with
     live speeds on every road, 10% with freeway mainline only);
  3. where nothing is measured, incident slowdowns (CHP, 511 partial closures);
  4. otherwise "unknown", so Valhalla uses its typical speed.
A source that fails is dropped from the fusion until it recovers. Bus positions
(transit.py) and express-lane toll prices (toll.py) are only logged for now.
"""

import gzip
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from statistics import median

from . import caltrans_cms, caltrans_lcs, catalog, chp, google_tiles, pems, sf511, tomtom, toll, transit
from .valhalla_traffic import TrafficTar, pack_speed

STATUS = os.environ.get("LIVETRAFFIC_STATUS", os.path.expanduser("~/geo/livetraffic/status.json"))
# Every measured cycle is also logged (gzip JSON lines, a file per source per
# day): weeks of it become our own time-of-week prior for surface streets,
# where PeMS has no detectors (the research doc's "DIY" prior).
PEMS_CSV_DIR = os.environ.get("PEMS_PREDICTED", os.path.expanduser("~/geo/valhalla/custom_files/predicted"))
HISTORY_DIR = os.environ.get("LIVETRAFFIC_HISTORY", os.path.expanduser("~/geo/livetraffic/history"))
# Two versions, each with its own traffic file and status, served by its own Valhalla:
#   full: every source, including TomTom (licensed free tier) and scraped Google tiles;
#   open: only public-agency data that can run at scale (CHP, 511, Caltrans LCS and
#         message-sign travel times; the PeMS prior is in the tiles).
PROFILES = {
    "full": {"sources": None, "tar": None, "status": STATUS},
    "open": {"sources": {"chp", "sf511", "lcs", "cms"},
             "tar": os.path.expanduser("~/geo/valhalla/custom_files/traffic-open.tar"),
             "status": os.path.expanduser("~/geo/livetraffic/status-open.json")},
}
EVERY_S = {"google": 300, "tomtom": 900, "chp": 120, "sf511": 300, "lcs": 300, "transit": 120, "cms": 120, "toll": 900}
LOGGED_ONLY = {"transit", "toll"}  # history for future priors and models; not fused yet
# per-edge results kept in history (full loop): training features for learned models (detectors.py labels)
LOGGED = {"google", "tomtom", "chp", "sf511", "lcs"} | LOGGED_ONLY
# Google color -> speed ratio before any calibration (research doc's starting values).
# Replaced each cycle by the median TomTom ratio per color where 30+ edges overlap.
DEFAULT_LEVEL_RATIO = {1: 1.0, 2: 0.7, 3: 0.4, 4: 0.2}
MIN_CALIBRATION_EDGES = 30
FREEWAY = {"motorway", "trunk"}


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class LiveTraffic:
    def __init__(self, profile="full"):
        self.profile = PROFILES[profile]
        self.sources = {n: s for n, s in EVERY_S.items()
                        if self.profile["sources"] is None or n in self.profile["sources"]}
        self.edges = catalog.load()
        self.freeways = [e for e in self.edges if e["road_class"] in FREEWAY]
        self.motorways = [e for e in self.edges if e["road_class"] == "motorway" and e.get("use") == "road"]
        self.streets = [e for e in self.edges if e["road_class"] not in FREEWAY]
        self.freeflow = self.load_freeflow()
        self.tar = TrafficTar(self.profile["tar"]) if self.profile["tar"] else TrafficTar()
        self.data = {name: {} for name in EVERY_S}
        self.next_at = {name: 0.0 for name in self.sources}
        self.level_ratio = dict(DEFAULT_LEVEL_RATIO)
        self.status = {"profile": profile, "catalogEdges": len(self.edges), "sources": {}, "tomtomRequestsToday": 0,
                       "freeflowSource": self.status_freeflow}
        self._day = None

    def load_freeflow(self):
        """{edge id: free-flow km/h} for freeway mainline: PeMS night speed, else TomTom's."""
        tom = {}
        if self.profile["sources"] is None and os.path.exists(tomtom.FREEFLOW):  # open: PeMS only
            with open(tomtom.FREEFLOW) as handle:
                tom = {int(k): v for k, v in json.load(handle).items()}
        prior = pems.read_freeflow(PEMS_CSV_DIR) if os.path.isdir(PEMS_CSV_DIR) else {}
        out, counts = {}, {"pems": 0, "tomtom": 0, "typical": 0}
        for edge in self.edges:
            if edge["road_class"] in FREEWAY and edge.get("use") == "road":
                kph = prior.get(edge["id"]) or tom.get(edge["id"])
                counts["pems" if edge["id"] in prior else "tomtom" if kph else "typical"] += 1
                if kph:
                    out[edge["id"]] = kph
        self.status_freeflow = counts
        return out

    # ------------------------------------------------------------ sources
    def poll_google(self):
        levels_fw, info_fw = google_tiles.poll(self.freeways, 14)
        levels_st, info_st = google_tiles.poll(self.streets, 13)
        levels = {**levels_st, **levels_fw}
        return levels, {"edges": len(levels), "tiles": info_fw["fetched"] + info_st["fetched"],
                        **{name: sum(1 for v in levels.values() if v == k)
                           for k, name in ((1, "green"), (2, "yellow"), (3, "red"), (4, "darkred"))}}

    def poll_tomtom(self):
        ratios, info = tomtom.poll(self.edges)
        today = datetime.now(timezone.utc).date()
        if today != self._day:
            self._day, self.status["tomtomRequestsToday"] = today, 0
        self.status["tomtomRequestsToday"] += info["tiles"]
        return ratios, info

    def poll_chp(self):
        effects, used = chp.poll(self.edges)
        return effects, {"edges": len(effects), "incidents": [
            {k: i[k] for k in ("id", "type", "location", "time", "effect", "edges")} for i in used]}

    def poll_sf511(self):
        effects, used = sf511.poll(self.edges)
        return effects, {"edges": len(effects), "events": used}

    def poll_lcs(self):
        effects, used = caltrans_lcs.poll(self.edges)
        return effects, {"edges": len(effects), "closures": used}

    def poll_cms(self):
        ratios, info, shown, corridors = caltrans_cms.poll(self.edges, HISTORY_DIR)
        # each sign's own history is its free-flow reference, and (with these
        # corridor inputs) the training data for forecast.py
        self.log_history("cms", {k: {"min": minutes, **self.corridor_inputs(corridors.get(k) or [])}
                                 for k, minutes in shown.items()})
        return ratios, info

    def corridor_inputs(self, ids):
        """What the other sources say about a sign's corridor this cycle (None: no data)."""
        def mean(values):
            values = [v for v in values if isinstance(v, float)]
            return round(sum(values) / len(values), 3) if values else None
        effects = [self.data[s].get(i) for s in ("chp", "sf511", "lcs") for i in ids]
        tom = [self.data["tomtom"].get(i) for i in ids]
        return {"edges": len(ids),
                "closed": sum(v == "closed" for v in effects) + sum(v == "closed" for v in tom),
                "incident": min([v for v in effects if isinstance(v, float)], default=None),
                "tomtom": mean(tom),
                "google": mean(self.level_ratio[self.data["google"][i]] for i in ids if i in self.data["google"])}

    def poll_transit(self):
        return transit.poll()

    def poll_toll(self):
        return toll.poll()

    def _run_source(self, name):
        try:
            started = time.time()
            result, info = getattr(self, f"poll_{name}")()
            self.status["sources"][name] = {"ok": True, "at": now_iso(),
                                            "seconds": round(time.time() - started, 1), **info}
            if name in LOGGED and result and self.profile["sources"] is None:
                self.log_history(name, result)
            return result
        except Exception as error:  # drop the source until it recovers
            self.status["sources"][name] = {"ok": False, "at": now_iso(), "error": f"{type(error).__name__}: {error}"}
            traceback.print_exc()
            return {}

    # ------------------------------------------------------------ fusion
    def calibrate(self):
        """Google color -> median TomTom ratio on motorway edges both measured.

        Motorway mainline only: ramps are slower than the road they serve, and
        on dense street grids TomTom's simplified z11 lines
        and Google's offset lines can sample the cross street or the other
        direction (measured 2026-10-30: trunk "red" came out 0.75 vs motorway
        0.42). Ratios are kept in order (a worse color is never faster)."""
        google, tom = self.data["google"], self.data["tomtom"]
        by_level = {}
        for edge in self.motorways:
            level, ratio = google.get(edge["id"]), tom.get(edge["id"])
            if level and isinstance(ratio, float):
                by_level.setdefault(level, []).append(ratio)
        for level, ratios in by_level.items():
            if len(ratios) >= MIN_CALIBRATION_EDGES:
                self.level_ratio[level] = round(median(ratios), 3)
        for level in sorted(self.level_ratio)[1:]:
            self.level_ratio[level] = min(self.level_ratio[level], self.level_ratio[level - 1])
        self.status["googleCalibration"] = {
            "ratio": {str(k): v for k, v in self.level_ratio.items()},
            "overlap": {str(k): len(v) for k, v in by_level.items()}}

    def fuse(self, edge):
        eid = edge["id"]
        incidents = [self.data[s].get(eid) for s in ("chp", "sf511", "lcs")]
        tom = self.data["tomtom"].get(eid)
        if (incidents[2] == "closed" and isinstance(tom, float) and eid in self.data["google"]
                and edge.get("use") != "ramp"):
            # planned closure, but traffic is measured moving. Not on ramps: tile
            # samples there land on the freeway beside them as often as on the ramp.
            incidents[2] = None
            self.overruled += 1
        if "closed" in incidents or tom == "closed":
            return "closed", True, False
        measured = []
        if edge["road_class"] in FREEWAY and edge.get("use") == "road":
            if isinstance(tom, float):
                measured.append(tom)
            if eid in self.data["google"]:
                measured.append(self.level_ratio[self.data["google"][eid]])
            if eid in self.data["cms"]:
                measured.append(self.data["cms"][eid])
        incident = any(v is not None for v in incidents) or eid in self.data["lcs"]
        if measured:
            return sum(measured) / len(measured), incident, True
        factors = [v for v in incidents if isinstance(v, float)]
        return (min(factors) if factors else None), incident, False

    def write(self):
        records, counts = {}, {"live": 0, "closed": 0, "unknown": 0}
        self.overruled = 0
        for edge in self.edges:
            ratio, incident, measured = self.fuse(edge)
            if ratio == "closed":
                records[edge["id"]] = pack_speed(closed=True, incident=True)
                counts["closed"] += 1
            elif ratio is None or not edge.get("speed"):
                records[edge["id"]] = pack_speed()  # unknown -> typical speed
                counts["unknown"] += 1
            else:
                ratio = max(0.05, min(1.0, ratio))
                base = self.freeflow.get(edge["id"], edge["speed"]) if measured else edge["speed"]
                records[edge["id"]] = pack_speed(kph=base * ratio, congestion=1 + round(62 * (1 - ratio)),
                                                 incident=incident)
                counts["live"] += 1
        self.tar.set_edges(records)
        self.status.update(written=now_iso(), edgesWritten=counts, lcsClosuresOverruledEdges=self.overruled)

    def log_history(self, source, values):
        stamp = datetime.now(timezone.utc)
        os.makedirs(HISTORY_DIR, exist_ok=True)
        tag = "" if self.profile["sources"] is None else "-open"  # both loops log signs; no shared file
        path = os.path.join(HISTORY_DIR, f"{source}{tag}-{stamp:%Y-%m-%d}.jsonl.gz")
        with gzip.open(path, "at") as handle:
            handle.write(json.dumps({"at": stamp.isoformat(timespec="seconds"),
                                     "values": {str(k): v for k, v in values.items()}}) + "\n")

    # ------------------------------------------------------------ loop
    def cycle(self):
        now = time.time()
        for name, every in self.sources.items():
            if now >= self.next_at[name]:
                self.data[name] = self._run_source(name)
                self.next_at[name] = now + every
        self.calibrate()
        self.write()
        self.save_status()

    def save_status(self):
        path = self.profile["status"]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "w") as handle:
            json.dump(self.status, handle, indent=1)
        os.replace(path + ".tmp", path)

    def clear(self):
        self.tar.set_edges({e["id"]: pack_speed() for e in self.edges})
        self.status.update(written=now_iso(), edgesWritten={"live": 0, "closed": 0, "unknown": len(self.edges)},
                           sources={}, cleared=True)
        self.save_status()


def main(argv):
    live = LiveTraffic(argv[argv.index("--profile") + 1] if "--profile" in argv else "full")
    if "--clear" in argv:
        live.clear()
        print("live traffic cleared")
        return
    while True:
        live.cycle()
        s = live.status
        print(f"{s['written']} {s['edgesWritten']} " + " ".join(
            f"{k}={'ok' if v.get('ok') else 'FAIL'}" for k, v in s["sources"].items())
            + f" calibration={s.get('googleCalibration', {}).get('ratio')}", flush=True)
        if "--once" in argv:
            return
        time.sleep(max(1.0, min(live.next_at.values()) - time.time()))


if __name__ == "__main__":
    main(sys.argv[1:])
