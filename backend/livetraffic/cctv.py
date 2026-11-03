"""Vehicle speeds from Caltrans District 4 live camera video (cwwp2.dot.ca.gov, public, no key).

About 195 of D4's ~770 freeway cameras list an HLS video stream (352x240, 30 fps);
about 110 of those streams answer at a time. Every cycle each live stream is
read for a few seconds at 10 fps, vehicles are found with an open-weight
detector (RT-DETRv2, Apache-2.0) and followed with ByteTrack (Roboflow trackers, Apache-2.0).

Speed without calibrating any camera: a vehicle's own size is the ruler. Each
step a tracked vehicle moves is divided by its box's extent along the direction
of motion, giving "vehicle lengths per second", which does not change when an
operator zooms. Times 4.5 m it is a rough km/h; the usable signal is the ratio
to the same camera's own free-flow value (as with the message signs), which
cancels the per-camera bias of that rough figure.

A camera usually sees both directions. Tracks are split into two groups by
their motion along the view's main axis; which group is which carriageway is
learned later from history (each group's ratio against the signs and TomTom
per direction). A small hash of the view is logged so a camera that was
pointed elsewhere starts a new baseline.

Runs in its own environment (torch), separate from run.py:
    ~/geo/cctv/venv/bin/python -m livetraffic.cctv            # loop, logs history/cctv-YYYY-MM-DD.jsonl.gz
    ~/geo/cctv/venv/bin/python -m livetraffic.cctv --once --show DIR   # one cycle, annotated frames in DIR
"""

import argparse
import gzip
import json
import math
import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import numpy as np
import requests

URL = "https://cwwp2.dot.ca.gov/data/d4/cctv/cctvStatusD04.json"
HISTORY_DIR = os.environ.get("LIVETRAFFIC_HISTORY", os.path.expanduser("~/geo/livetraffic/history"))
MODEL = "PekingU/rtdetr_v2_r18vd"
VEHICLES = {"car", "truck", "bus", "motorcycle"}
# frames per camera per cycle; on a CPU only (no CUDA), fewer: CCTV_FPS=5 CCTV_SECONDS=6
SECONDS = int(os.environ.get("CCTV_SECONDS", "8"))
FPS = int(os.environ.get("CCTV_FPS", "10"))
WIDTH, HEIGHT = 352, 240
MIN_SCORE = 0.35
MIN_TRACK_S = 0.8          # shorter tracks are mostly detector flicker
STOPPED_VL = 0.3           # moved under 0.3 vehicle lengths in the whole track: stopped
VEHICLE_M = 4.5
EVERY_S = 300
CAPTURE_WORKERS = 12
FFMPEG = os.environ.get("FFMPEG") or shutil.which("ffmpeg") or "ffmpeg"


def cameras():
    """[{id, route, direction, lat, lon, stream}] for in-service cameras with a video stream."""
    out = []
    for item in requests.get(URL, timeout=30).json()["data"]:
        c = item["cctv"]
        stream = c["imageData"].get("streamingVideoURL")
        if c.get("inService") == "true" and stream:
            loc = c["location"]
            out.append({"id": loc["locationName"].split()[0], "route": loc["route"], "direction": loc["direction"],
                        "lat": float(loc["latitude"]), "lon": float(loc["longitude"]), "stream": stream})
    return out


def capture(stream):
    """(SECONDS*FPS, HEIGHT, WIDTH, 3) uint8 frames, or None when the stream is down."""
    try:
        raw = subprocess.run([FFMPEG, "-loglevel", "error", "-rw_timeout", "10000000", "-i", stream,
                              "-t", str(SECONDS), "-vf", f"fps={FPS},scale={WIDTH}:{HEIGHT}",
                              "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                             capture_output=True, timeout=SECONDS + 30).stdout
    except subprocess.TimeoutExpired:
        return None
    n = len(raw) // (WIDTH * HEIGHT * 3)
    return np.frombuffer(raw[:n * WIDTH * HEIGHT * 3], np.uint8).reshape(n, HEIGHT, WIDTH, 3) if n >= FPS * 3 else None


def view_hash(frames):
    """64-bit average hash of the median frame (the road without the cars), as hex."""
    gray = np.median(frames[::5], axis=0).mean(axis=2)
    small = gray[: HEIGHT // 8 * 8, : WIDTH // 8 * 8].reshape(8, HEIGHT // 8, 8, WIDTH // 8).mean(axis=(1, 3))
    bits = (small > small.mean()).flatten()
    return f"{int(''.join('1' if b else '0' for b in bits), 2):016x}"


class Detector:
    def __init__(self):
        import torch
        from transformers import AutoImageProcessor, RTDetrV2ForObjectDetection
        self.torch = torch
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.processor = AutoImageProcessor.from_pretrained(MODEL)
        self.model = RTDetrV2ForObjectDetection.from_pretrained(MODEL).to(self.device).eval()
        # half precision only where it is fast (Volta and newer); Pascal (GTX 10xx) runs it at 1/64 speed
        self.half = self.device == "cuda" and torch.cuda.get_device_capability()[0] >= 7
        if self.half:
            self.model = self.model.half()
        labels = self.model.config.id2label
        self.keep = {i for i, name in labels.items() if name in VEHICLES}

    def __call__(self, frames, batch=40):
        """Per frame: (xyxy array, scores, class ids) of vehicles."""
        out = []
        for i in range(0, len(frames), batch):
            chunk = list(frames[i:i + batch])
            inputs = self.processor(images=chunk, return_tensors="pt").to(self.device)
            if self.half:
                inputs["pixel_values"] = inputs["pixel_values"].half()
            with self.torch.no_grad():
                result = self.model(**inputs)
            for r in self.processor.post_process_object_detection(
                    result, target_sizes=[(HEIGHT, WIDTH)] * len(chunk), threshold=MIN_SCORE):
                keep = [j for j, label in enumerate(r["labels"].tolist()) if label in self.keep]
                out.append((r["boxes"][keep].float().cpu().numpy(), r["scores"][keep].float().cpu().numpy(),
                            r["labels"][keep].cpu().numpy()))
        return out


def tracks(detections):
    """{track id: [(frame, cx, cy, w, h)]} via ByteTrack (Roboflow trackers)."""
    import supervision as sv
    from trackers import ByteTrackTracker
    # small, low-contrast vehicles: start tracks from medium-confidence boxes too
    tracker = ByteTrackTracker(frame_rate=FPS, track_activation_threshold=0.5, high_conf_det_threshold=0.5)
    out = {}
    for f, (xyxy, scores, labels) in enumerate(detections):
        dets = tracker.update(sv.Detections(xyxy=xyxy.reshape(-1, 4), confidence=scores, class_id=labels.astype(int)))
        for box, tid in zip(dets.xyxy, dets.tracker_id):
            if tid < 0:  # not (yet) a confirmed track
                continue
            x1, y1, x2, y2 = box
            out.setdefault(int(tid), []).append((f, (x1 + x2) / 2, (y1 + y2) / 2, x2 - x1, y2 - y1))
    return out


def track_motion(points):
    """(vehicle lengths per second, unit motion (ux, uy)) for one track, or None if too short."""
    if (points[-1][0] - points[0][0]) / FPS < MIN_TRACK_S:
        return None
    dx, dy = points[-1][1] - points[0][1], points[-1][2] - points[0][2]
    norm = math.hypot(dx, dy)
    if norm < 1e-6:
        return 0.0, (0.0, 0.0)
    ux, uy = dx / norm, dy / norm
    lengths = 0.0
    for a, b in zip(points, points[1:]):
        step = (b[1] - a[1]) * ux + (b[2] - a[2]) * uy                  # along the overall direction
        extent = abs(ux) * (a[3] + b[3]) / 2 + abs(uy) * (a[4] + b[4]) / 2  # box size along it
        lengths += step / max(extent, 1.0)
    return max(lengths, 0.0) / ((points[-1][0] - points[0][0]) / FPS), (ux, uy)


def summarize(track_points, n_frames, detections):
    """{"vehicles", "perFrame", "groups": [{"angle", "n", "vlps", "kph", "stopped"}]}."""
    moving, stopped = [], 0
    for points in track_points.values():
        m = track_motion(points)
        if m is None:
            continue
        vlps, u = m
        if vlps * (points[-1][0] - points[0][0]) / FPS < STOPPED_VL:
            stopped += 1
            moving.append((0.0, None))
        else:
            moving.append((vlps, u))
    directions = [u for _, u in moving if u]
    groups = {}
    if directions:
        # the main axis of travel (angles doubled so opposite directions agree), then which way along it
        axis = math.atan2(sum(math.sin(2 * math.atan2(uy, ux)) for ux, uy in directions),
                          sum(math.cos(2 * math.atan2(uy, ux)) for ux, uy in directions)) / 2
        ax, ay = math.cos(axis), math.sin(axis)
        for vlps, u in moving:
            if u:
                groups.setdefault("a" if u[0] * ax + u[1] * ay >= 0 else "b", []).append(vlps)
        angle = {"a": math.degrees(axis) % 360, "b": (math.degrees(axis) + 180) % 360}
    out = {"vehicles": len(moving), "stopped": stopped,
           "perFrame": round(sum(len(d[1]) for d in detections) / max(n_frames, 1), 2), "groups": {}}
    for g, speeds in groups.items():
        out["groups"][g] = {"angle": round(angle[g]), "n": len(speeds), "vlps": round(float(np.median(speeds)), 3),
                            "kph": round(float(np.median(speeds)) * VEHICLE_M * 3.6, 1)}
    return out


def draw(path, frames, track_points, summary):
    import cv2
    img = cv2.cvtColor(frames[len(frames) // 2], cv2.COLOR_RGB2BGR)
    img = cv2.resize(img, (WIDTH * 2, HEIGHT * 2))
    for points in track_points.values():
        pts = np.array([(p[1] * 2, p[2] * 2) for p in points], np.int32)
        cv2.polylines(img, [pts], False, (0, 255, 255), 1)
        cv2.circle(img, tuple(pts[-1]), 3, (0, 0, 255), -1)
    text = " ".join(f'{g}:{v["n"]}@{v["kph"]}' for g, v in summary["groups"].items()) + f' stop:{summary["stopped"]}'
    cv2.putText(img, text, (5, HEIGHT * 2 - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
    cv2.imwrite(path, img)


def cycle(detector, show=None):
    cams = cameras()
    values, down = {}, 0
    started = time.time()
    with ThreadPoolExecutor(CAPTURE_WORKERS) as pool:
        futures = {pool.submit(capture, c["stream"]): c for c in cams}
        for future in as_completed(futures):
            cam, frames = futures[future], future.result()
            if frames is None:
                down += 1
                continue
            detections = detector(frames)
            track_points = tracks(detections)
            summary = summarize(track_points, len(frames), detections)
            summary.update(route=cam["route"], direction=cam["direction"], view=view_hash(frames))
            values[cam["id"]] = summary
            if show:
                os.makedirs(show, exist_ok=True)
                draw(os.path.join(show, f'{cam["id"]}.jpg'), frames, track_points, summary)
    info = {"cameras": len(cams), "live": len(values), "down": down, "seconds": round(time.time() - started)}
    return values, info


def log(values, info):
    stamp = datetime.now(timezone.utc)
    os.makedirs(HISTORY_DIR, exist_ok=True)
    with gzip.open(os.path.join(HISTORY_DIR, f"cctv-{stamp:%Y-%m-%d}.jsonl.gz"), "at") as handle:
        handle.write(json.dumps({"at": stamp.isoformat(timespec="seconds"), "info": info, "values": values}) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--show", help="write annotated middle frames here")
    args = parser.parse_args()
    detector = Detector()
    while True:
        started = time.time()
        try:
            values, info = cycle(detector, args.show)
            log(values, info)
            print(datetime.now().strftime("%H:%M:%S"), json.dumps(info), flush=True)
        except Exception as error:  # a feed hiccup should not end the loop
            print(datetime.now().strftime("%H:%M:%S"), "cycle failed:", repr(error), flush=True)
        if args.once:
            return
        time.sleep(max(0, EVERY_S - (time.time() - started)))


if __name__ == "__main__":
    main()
