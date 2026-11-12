"""Build a replay-only copy of the console: one self-contained page with recorded runs embedded.

  python -m console.export_static OUT.html

The page needs no server, Docker or models (for showing runs on any computer, e.g. in class).
Live runs, fault injection and approvals still need the local console (python -m console.server).
"""
import json
import re
import sys
from pathlib import Path

from console.server import _summary
from opsgraph.events import EVENTS_DIR

STATIC = Path(__file__).resolve().parent / "static" / "index.html"
# (label, run_id): live = really executed on the test stack; dry run = investigated and planned only.
RUNS = [
    ("Two faults at once (live)", "20261112-130413-fc23f8"),
    ("Network partition (live)", "20261112-125553-716449"),
    ("Database down (live)", "20261112-125433-d9acaa"),
    ("Cache down (live)", "20261112-125943-fe8298"),
    ("Bad config deploy (live)", "20261112-125715-fb6603"),
    ("Connection exhaustion (dry run)", "20261112-123206-14f368"),
    ("Read-only database (dry run)", "20261112-123307-fa3d65"),
    ("Frozen web app (dry run)", "20261112-123344-368d36"),
]
GOOD_CONFIG = {"db_host": "postgres", "db_port": 5432, "connect_timeout_seconds": 2}
HEALTHY = {"containers": {c: {"state": "running", "networks": {"services_default": [a]}}
                          for c, a in (("opsmind-postgres", "postgres"), ("opsmind-app", "app"),
                                       ("opsmind-redis", "redis"))},
           "app_health": {"status": 200, "db_ok": True, "redis_ok": True},
           "live_config": GOOD_CONFIG, "known_good": GOOD_CONFIG, "paused": False,
           "faults": {}}


def build():
    html = STATIC.read_text(encoding="utf-8")
    style = re.search(r"<style>(.*?)</style>", html, re.S).group(1)
    body = re.search(r"<body>(.*?)<script>", html, re.S).group(1)
    script = re.search(r"<script>(.*?)</script>\s*</body>", html, re.S).group(1)
    runs, events = [], {}
    for label, rid in RUNS:
        path = EVENTS_DIR / f"{rid}.jsonl"
        events[rid] = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
        runs.append({**_summary(path), "label": label, "live": False})
    data = {"topology": HEALTHY, "runs": runs, "events": events, "featured": RUNS[0][1]}
    gutter = ("main,header{padding-left:max(16px, env(safe-area-inset-left, 0px));"
              "padding-right:max(16px, env(safe-area-inset-right, 0px))}\n:root{color-scheme:dark}\n")
    blob = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    return ("<title>OpsMind Console</title>\n"
            '<meta name="claude:viewport-fit-sides" content="cover">\n'
            f"<style>{style}\n{gutter}</style>\n{body}"
            f"<script>window.OPSMIND_STATIC={blob};</script>\n<script>{script}</script>\n")


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "opsmind_console_replay.html")
    out.write_text(build(), encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size // 1024} KB, {len(RUNS)} runs)")
