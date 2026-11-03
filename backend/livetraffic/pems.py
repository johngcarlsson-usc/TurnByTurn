"""Time-of-week speed prior from Caltrans PeMS loop detectors (account: PEMS_USER / PEMS_PASSWORD).

The research doc's "prior": for every mainline freeway detector, the median
speed for each of the 168 hours of the week over a few months of PeMS
station_hour files. Each catalog freeway edge takes the profile of the
nearest detectors on the same freeway and direction; the result is written
in Valhalla's own predicted-traffic input format (one CSV per graph tile,
"level/tile/index,freeflow kph,constrained kph,base64 DCT speeds"), which
`valhalla_add_predicted_traffic` adds to the tiles. Valhalla then uses it
whenever a route has a date_time and no fresher live speed.

    venv/bin/python -m livetraffic.pems download 2026-07 2026-08 2026-09
    venv/bin/python -m livetraffic.pems download 2026-07 2026-08 2026-09 --kind station_5min
    venv/bin/python -m livetraffic.pems build ~/geo/valhalla-predicted

Surface streets have no PeMS detectors; their prior will come from our own
logged live history (run.HISTORY_DIR).
"""

import base64
import csv
import gzip
import math
import os
import re
import sys
import warnings
from collections import defaultdict
from datetime import date, datetime, timedelta

import numpy as np
import requests

from . import _load_local_env, catalog
from .chp import DIRECTIONS, _bearing, _local_m

PEMS = "https://pems.dot.ca.gov/"
DATA_DIR = os.path.expanduser(os.environ.get("PEMS_DIR", "~/geo/pems"))
DISTRICT = 4
MIN_OBSERVED_PCT = 50          # skip hours PeMS mostly imputed
HOLIDAYS = {date(2026, 7, 3), date(2026, 7, 4), date(2026, 9, 7)}
MIN_WEEKS = 4                  # an hour of the week needs this many observations
POOL_5MIN = 3                  # a 5-minute bucket also takes the samples of this many neighbours either side (see profiles_5min)
MAX_STATION_M = 20000          # an edge takes detectors this close on its freeway and direction (see assign)
MPH_TO_KPH = 1.609344

# Valhalla's predicted-speed codec (valhalla/src/baldr/predictedspeeds.cc)
BUCKETS = 2016                 # 5-minute buckets, the week starting Sunday 00:00 local time
COEFFICIENTS = 200
_COS = np.cos(np.pi / BUCKETS * (np.arange(BUCKETS)[:, None] + 0.5) * np.arange(COEFFICIENTS)[None, :])
_NORM = math.sqrt(2.0 / BUCKETS)


def compress(speeds):
    """2016 bucket speeds (kph) -> 200 int16 coefficients, as compress_speed_buckets."""
    coef = speeds @ _COS
    coef[0] *= 1 / math.sqrt(2)
    return np.round(_NORM * coef).astype(np.int16)


def decompress(coef):
    """200 coefficients -> 2016 bucket speeds, as decompress_speed_bucket."""
    c = coef.astype(np.float64).copy()
    c[0] *= 1 / math.sqrt(2)
    return (_COS @ c) * _NORM


def encode(coef):
    return base64.b64encode(coef.astype(">i2").tobytes()).decode()


# ------------------------------------------------------------------ download
def session():
    _load_local_env()
    s = requests.Session()
    s.post(PEMS, timeout=30, data={"redirect": "", "username": os.environ["PEMS_USER"],
                                   "password": os.environ["PEMS_PASSWORD"], "login": "Login"})
    return s


def files(s, kind, year):
    d = s.get(PEMS, timeout=60, params={"srq": "clearinghouse", "district_id": DISTRICT, "geotag": "null",
                                        "yy": year, "type": kind, "returnformat": "text"}).json()
    return {f["file_name"]: f["url"] for month in d["data"].values() for f in month}


def download(months, kind="station_hour"):
    """station_hour (one file a month) or station_5min (one a day) for the given months, plus the newest metadata."""
    s = session()
    os.makedirs(DATA_DIR, exist_ok=True)
    wanted = {}
    for year in sorted({m[:4] for m in months}):
        data = files(s, kind, year)
        wanted.update({n: u for n, u in data.items()
                       if any(re.search(rf"_{m.replace('-', '_')}[._]", n) for m in months)})
        meta = files(s, "meta", year)
        latest = max(meta)
        wanted[latest] = meta[latest]
    for name, url in sorted(wanted.items()):
        out = os.path.join(DATA_DIR, name)
        if os.path.exists(out):
            continue
        with s.get(PEMS.rstrip("/") + url, stream=True, timeout=600) as r:
            r.raise_for_status()
            with open(out + ".part", "wb") as h:
                for chunk in r.iter_content(1 << 20):
                    h.write(chunk)
        os.replace(out + ".part", out)
        print("downloaded", name, flush=True)


# ------------------------------------------------------------------ profiles
def stations():
    """Mainline detectors from the newest metadata file: {id: {fwy, dir, lat, lon}}."""
    meta = sorted(f for f in os.listdir(DATA_DIR) if re.match(rf"d{DISTRICT:02d}_text_meta_.*\.txt$", f))[-1]
    out = {}
    with open(os.path.join(DATA_DIR, meta)) as h:
        for row in csv.DictReader(h, delimiter="\t"):
            if row["Type"] == "ML" and row["Latitude"] and row["Longitude"]:
                out[row["ID"]] = {"fwy": row["Fwy"], "dir": row["Dir"],
                                  "lat": float(row["Latitude"]), "lon": float(row["Longitude"])}
    return out


def hour_of_week(stamp):
    """"09/01/2026 07:00:00" -> 0..167, Sunday 00:00 = 0 (Valhalla's week)."""
    t = datetime.strptime(stamp, "%m/%d/%Y %H:%M:%S")
    return t.date(), ((t.weekday() + 1) % 7) * 24 + t.hour


def profiles(mainline, keep=lambda day: True):
    """{station: 168 median speeds in mph (nan where too few weeks)}, from the days keep() accepts."""
    samples = defaultdict(lambda: [[] for _ in range(168)])
    for name in sorted(f for f in os.listdir(DATA_DIR) if "station_hour" in f and f.endswith(".gz")):
        with gzip.open(os.path.join(DATA_DIR, name), "rt") as h:
            for line in h:
                f = line.split(",", 12)
                if f[1] not in mainline or not f[11] or not f[8] or float(f[8]) < MIN_OBSERVED_PCT:
                    continue
                day, how = hour_of_week(f[0])
                if day not in HOLIDAYS and keep(day):
                    samples[f[1]][how].append(float(f[11]))
        print("read", name, flush=True)
    out = {}
    for sid, hours in samples.items():
        prof = np.array([np.median(v) if len(v) >= MIN_WEEKS else np.nan for v in hours])
        if np.isfinite(prof).sum() >= 150:  # nearly every hour of the week
            out[sid] = prof
    return out


def five_min(mainline, keep=lambda day: True):
    """(station ids, first Sunday, speeds): station_5min speeds in mph as [station, week, 2016 buckets],
    nan where missing, under MIN_OBSERVED_PCT, a holiday or a day keep() rejects."""
    names = sorted(f for f in os.listdir(DATA_DIR) if "station_5min" in f and f.endswith(".gz"))
    days = [date(*map(int, re.search(r"_(\d{4})_(\d\d)_(\d\d)\.", n).groups())) for n in names]
    start = min(days) - timedelta(days=(min(days).weekday() + 1) % 7)
    weeks = (max(days) - start).days // 7 + 1
    ids = sorted(mainline)
    row = {sid: i for i, sid in enumerate(ids)}
    out = np.full((len(ids), weeks * BUCKETS), np.nan, dtype=np.float32)
    for name, day in zip(names, days):
        if day in HOLIDAYS or not keep(day):
            continue
        base = (day - start).days * 288
        with gzip.open(os.path.join(DATA_DIR, name), "rt") as h:
            for line in h:
                f = line.split(",", 12)
                i = row.get(f[1])
                if i is None or not f[11] or not f[8] or float(f[8]) < MIN_OBSERVED_PCT:
                    continue
                out[i, base + int(f[0][11:13]) * 12 + int(f[0][14:16]) // 5] = float(f[11])
        print("read", name, flush=True)
    return ids, start, out.reshape(len(ids), weeks, BUCKETS)


def profiles_5min(ids, speeds, pool=POOL_5MIN):
    """{station: 2016 median speeds in mph (nan where too few weeks)} from five_min().

    A bucket's median also takes the `pool` neighbouring buckets either side (pool=3: a 35-minute
    window), and needs MIN_WEEKS weeks with a sample. One 5-minute reading is noisy and ~10 weeks
    give a lone bucket few samples; on a validation split (train 07-01..08-19, score 08-20..09-09)
    pool 3 beat 0 and 1 over the weekday rush-hour onsets and clearances (2.42% median error vs 2.52%).

    Not used for the tiles: scored on 09-10..09-30, these profiles beat build()'s hourly ones by
    only 0.1 points of median error at weekday peaks after Valhalla's codec (2.96% -> 2.88%; mean
    error unchanged, 18:30-19:30 worse), because the codec's 200 coefficients cannot hold detail
    shorter than about 50 minutes anyway."""
    out = {}
    for i, sid in enumerate(ids):
        line = speeds[i].reshape(-1)  # the timeline, so pooling runs across midnight and weeks
        stack = np.stack([np.roll(line, -k) for k in range(-pool, pool + 1)], axis=-1)
        stack = stack.reshape(speeds.shape[1], BUCKETS, 2 * pool + 1)
        weeks = np.isfinite(stack).any(axis=2).sum(axis=0)
        if (weeks >= MIN_WEEKS).sum() < BUCKETS * 150 // 168:  # the hourly build's "nearly every hour"
            continue
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)  # all-nan buckets
            prof = np.nanmedian(stack.transpose(1, 0, 2).reshape(BUCKETS, -1), axis=1)
        prof[weeks < MIN_WEEKS] = np.nan
        out[sid] = prof
    return out


def to_buckets(profile_mph):
    """168 hourly or 2016 five-minute speeds (mph, slot centres) -> 2016 five-minute speeds in kph,
    cyclic interpolation over missing slots."""
    n = len(profile_mph)
    slots = (np.arange(n) + 0.5) * BUCKETS / n
    ok = np.isfinite(profile_mph)
    t = np.arange(BUCKETS) + 0.5
    x = np.concatenate([slots[ok] - BUCKETS, slots[ok], slots[ok] + BUCKETS])
    y = np.tile(profile_mph[ok], 3)
    return np.interp(t, x, y) * MPH_TO_KPH


# ------------------------------------------------------------------ edges
def _fwy_signed(names):
    """{freeway number: signed direction letter or None}: "I 80 East" -> {"80": "E"}, "CA 24" -> {"24": None}."""
    out = {}
    for n in names:
        m = re.match(r"(?i)(I|US|CA|SR)[ -]?(\d+)(?:\s+(North|South|East|West))?", n)
        if m:
            out[m.group(2)] = out.get(m.group(2)) or (m.group(3)[0].upper() if m.group(3) else None)
    return out


def assign(edges, mainline, profs):
    """{edge id: 2016 bucket speeds}: inverse-distance blend of the two nearest detectors
    on the same freeway number and direction within MAX_STATION_M.

    Why 20 km: predicting each detector's weekday rush-hour speeds from its nearest
    neighbour on the same freeway and direction (2026-07..09) gave a median error of
    3.8% under 1.5 km, 6.1% at 3-5 km, 7.1% at 5-10 km and 8.3% at 10-20 km, while
    Valhalla's typical speed on the detector's own segment was off by 15%."""
    by_fwy = defaultdict(list)
    for sid, st in mainline.items():
        if sid in profs:
            by_fwy[st["fwy"]].append((sid, st))
    out = {}
    for edge in edges:
        if edge["road_class"] not in {"motorway", "trunk"} or edge.get("use") != "road":
            continue
        mid = edge["shape"][len(edge["shape"]) // 2]
        start, end = _local_m(mid[0], mid[1], *edge["shape"][0]), _local_m(mid[0], mid[1], *edge["shape"][-1])
        travel = _bearing(start, end)
        near = []
        for fwy, signed in _fwy_signed(edge["names"]).items():
            for sid, st in by_fwy.get(fwy, []):
                if signed:  # PeMS Dir is the signed route direction (I-80 "E" runs north in Berkeley)
                    if st["dir"] != signed:
                        continue
                else:
                    heading = DIRECTIONS[st["dir"]]
                    if min(abs(travel - heading), 360 - abs(travel - heading)) >= 90:  # the closer of E/W or N/S
                        continue
                d = math.hypot(*_local_m(mid[0], mid[1], st["lat"], st["lon"]))
                if d <= MAX_STATION_M:
                    near.append((d, sid))
        if not near:
            continue
        near = sorted(near)[:2]
        weights = np.array([1 / max(d, 50.0) for d, _ in near])
        hourly = np.array([profs[sid] for _, sid in near])
        with np.errstate(invalid="ignore"):  # an hour no near detector has stays nan; to_buckets fills it
            blended = np.nansum(hourly * weights[:, None], axis=0) / np.sum(np.isfinite(hourly) * weights[:, None], axis=0)
        out[edge["id"]] = to_buckets(blended)
    return out


def tile_path(edge_id):
    """Valhalla tile path for a GraphId: level 2 -> 2/000/818/660, levels 0-1 -> 0/003/196."""
    level, tile = edge_id & 0x7, (edge_id >> 3) & 0x3FFFFF
    digits = 9 if level == 2 else 6
    s = f"{tile:0{digits}d}"
    return "/".join([str(level)] + [s[i:i + 3] for i in range(0, digits, 3)])


def write_csvs(out_dir, speeds):
    """Valhalla predicted-traffic CSVs, one per tile. Night (7pm-7am) median = freeflow,
    day (7am-7pm) median = constrained, as Valhalla defines them."""
    hour = (np.arange(BUCKETS) // 12) % 24
    night, day = (hour < 7) | (hour >= 19), (hour >= 7) & (hour < 19)
    rows, worst = defaultdict(list), 0.0
    for edge_id, buckets in speeds.items():
        buckets = np.clip(buckets, 5, 140)
        coef = compress(buckets)
        worst = max(worst, float(np.max(np.abs(decompress(coef) - buckets))))
        level, tile, index = edge_id & 0x7, (edge_id >> 3) & 0x3FFFFF, (edge_id >> 25) & 0x1FFFFF
        rows[tile_path(edge_id)].append(f"{level}/{tile}/{index},{int(round(np.median(buckets[night])))},"
                                        f"{int(round(np.median(buckets[day])))},{encode(coef)}")
    for path, lines in rows.items():
        target = os.path.join(out_dir, path + ".csv")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w") as h:
            h.write("\n".join(lines) + "\n")
    return len(rows), worst


def read_freeflow(csv_dir):
    """{edge id: free-flow km/h} back from the CSVs write_csvs made (what Valhalla loaded)."""
    out = {}
    for root, _, names in os.walk(csv_dir):
        for name in names:
            if name.endswith(".csv"):
                with open(os.path.join(root, name)) as h:
                    for line in h:
                        key, freeflow = line.split(",")[:2]
                        level, tile, index = map(int, key.split("/"))
                        out[level | (tile << 3) | (index << 25)] = float(freeflow)
    return out


def build(out_dir):
    mainline = stations()
    profs = profiles(mainline)
    print(f"{len(profs)} of {len(mainline)} mainline detectors have a full week profile", flush=True)
    speeds = assign(catalog.load(), mainline, profs)
    tiles, worst = write_csvs(out_dir, speeds)
    print(f"{len(speeds)} freeway edges with a time-of-week prior in {tiles} tiles; "
          f"largest codec round-trip error {worst:.2f} kph", flush=True)


if __name__ == "__main__":
    if sys.argv[1] == "download":
        args = sys.argv[2:]
        kind = args.pop(args.index("--kind") + 1) if "--kind" in args else "station_hour"
        download([a for a in args if a != "--kind"], kind)
    elif sys.argv[1] == "build":
        build(os.path.expanduser(sys.argv[2]))
