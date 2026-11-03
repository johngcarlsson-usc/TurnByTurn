"""Screenshots for the FOSS4G talk, taken from the running app with the live model in the loop.

The phone's GPS is simulated the way the beacon page sends it: a fix at the Great Mall, Milpitas,
POSTed to /api/gps every few seconds. Each demo opens ?scenario=<id>&mode=live&autorun=1, so the
spoken request goes to the model and the planner exactly as if it were typed, and the screenshot is
taken once the independent check is on screen.

    backend/venv/bin/python scripts/capture_talk.py                 # all demos
    backend/venv/bin/python scripts/capture_talk.py gm-team         # one
    APP_URL=http://127.0.0.1:5174 backend/venv/bin/python scripts/capture_talk.py

Needs the backend (5055), the app (vite preview on 4174 by default), and Playwright's Chromium
(`backend/venv/bin/python -m playwright install chromium`).
"""

import os
import sys
import threading
import time

import requests
from playwright.sync_api import sync_playwright

APP = os.environ.get("APP_URL", "http://127.0.0.1:4174")
API = os.environ.get("API_URL", "http://127.0.0.1:5055")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs", "talk", "screenshots")
GREAT_MALL = {"lat": 37.41572, "lng": -121.89768, "accuracy": 8}
DEMOS = ["gm-gas-right", "gm-coffee-hour", "gm-no-880", "gm-errands", "gm-team"]
SIZE = {"width": 1600, "height": 900}


def keep_phone_fix(stop):
    """POST the Great Mall fix every 5 s, like the beacon page (the app ignores fixes older than 2 min)."""
    while not stop.is_set():
        try:
            requests.post(f"{API}/api/gps", json=GREAT_MALL, timeout=5)
        except requests.RequestException:
            pass
        stop.wait(5)


def capture(page, demo):
    page.goto(f"{APP}/?scenario={demo}&mode=live&autorun=1")
    path = os.path.join(OUT, f"{demo}.png")
    try:  # done when the independent check of the plan is on screen
        page.wait_for_selector("text=Independent check >> visible=true", timeout=120_000)
    except Exception:
        page.screenshot(path=path.replace(".png", ".FAILED.png"))
        print(f"{demo}: no checked plan after 2 minutes; see {os.path.normpath(path.replace('.png', '.FAILED.png'))}")
        return
    time.sleep(4)  # map fit and route animation
    page.screenshot(path=path)
    print("wrote", os.path.normpath(path), flush=True)
    if demo == "gm-team":
        send_to_partner(page)


def send_to_partner(page):
    """Press "Send to Wife" and screenshot the link she gets, on a phone-sized page."""
    with page.expect_response(lambda r: r.url.endswith("/api/share") and r.request.method == "POST") as info:
        page.get_by_role("button", name="Send to Wife").click()
    link = info.value.json()["path"]
    phone = page.context.browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2,
                                          is_mobile=True, has_touch=True)
    try:
        phone.goto(f"{APP}{link}")
        phone.wait_for_selector("#trip >> visible=true", timeout=30_000)
        time.sleep(1)
        path = os.path.join(OUT, "gm-team-phone.png")
        phone.screenshot(path=path)
        print("wrote", os.path.normpath(path), flush=True)
    finally:
        phone.close()


def main(demos):
    stop = threading.Event()
    threading.Thread(target=keep_phone_fix, args=(stop,), daemon=True).start()
    time.sleep(1)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            for demo in demos:
                page = browser.new_page(viewport=SIZE, device_scale_factor=1)
                try:
                    capture(page, demo)
                finally:
                    page.close()
            browser.close()
    finally:
        stop.set()


if __name__ == "__main__":
    main(sys.argv[1:] or DEMOS)
