"""OpsMind Console: a live view of the multi-agent incident graph.

  python -m console.server            # http://127.0.0.1:8765

What it serves:
  GET  /                              the console (console/static/index.html)
  GET  /api/topology                  containers, networks, app health, live config, pause state
  GET  /api/runs                      recorded graph runs (trajectories/graph/*.jsonl), newest first
  GET  /api/runs/{id}/events          all events of a run as JSON (replay)
  GET  /api/runs/{id}/stream          Server-Sent Events, live tail of a running run
  POST /api/runs                      start a run {"mode": "dry"|"live", "hitl": bool}
  POST /api/runs/{id}/decision        answer a paused run {"approve": [step, ...]}
  POST /api/faults/{name}/{op}        inject or clear a test fault (op = inject | clear)
  POST /api/pause                     {"on": bool}: the kill switch (OPSMIND_PAUSED)

It binds to 127.0.0.1 only: fault injection and live actions act on the local test stack.
Live and replayed runs are drawn by the same code from the same event files.
"""
import asyncio
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "agents"), str(ROOT / "eval"), str(ROOT / "faults")]

import fast_tier  # noqa: E402
from langgraph.types import Command  # noqa: E402
from opsgraph.events import EVENTS_DIR  # noqa: E402
from opsgraph.graph import APP, CACHE, PG, build, default_cfg, new_run_id  # noqa: E402
from tools import deploy, governor  # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"
FAULTS = {
    "cache-down": "Stop the Redis cache",
    "dependency-down": "Stop the Postgres database",
    "network-partition": "Detach Postgres from its network",
    "config-regression": "Deploy a config with the wrong DB port",
    "conn-exhaust": "Another client takes every DB connection",
    "read-only": "Switch Postgres to read-only",
    "app-paused": "Freeze the web app container",
    "compound": "Cache down AND a bad deploy at once",
}

app = FastAPI(title="OpsMind Console")
GRAPH = build()
RUNS = {}            # run_id -> {"thread", "pending", "answer", "event", "done", "error"}
LOCK = threading.Lock()


# ------------------------------------------------------------------ topology

def _networks(name):
    p = subprocess.run(["docker", "inspect", "-f", "{{json .NetworkSettings.Networks}}", name],
                       capture_output=True, text=True, timeout=5)
    try:
        nets = json.loads(p.stdout or "null") or {}
        return {k: (v.get("Aliases") or []) for k, v in nets.items()}
    except ValueError:
        return None


_topo_cache = {"t": 0, "v": None}


@app.get("/api/topology")
def topology():
    if time.time() - _topo_cache["t"] < 1.0 and _topo_cache["v"]:
        return _topo_cache["v"]
    out = {"containers": {}, "app_health": None, "live_config": None, "known_good": None}
    for c in (PG, APP, CACHE):
        out["containers"][c] = {"state": fast_tier.container_state(c), "networks": _networks(c)}
    status, err, body = fast_tier.probe_app(int(default_cfg()["app_port"]), timeout=3)
    out["app_health"] = {"status": status, "error": err,
                         "db_ok": body.get("db_ok"), "redis_ok": body.get("redis_ok")}
    try:
        out["live_config"] = deploy.read_live()
        good = deploy.last_good(deploy.read_history())
        out["known_good"] = good["config"] if good else None
    except Exception:
        pass
    out["paused"] = governor.PAUSE_FILE.exists()
    out["deploy_in_progress"] = governor.DEPLOY_LOCK.exists()
    try:
        import requests
        out["ollama"] = requests.get("http://localhost:11434/api/tags", timeout=1).ok
    except Exception:
        out["ollama"] = False
    out["faults"] = FAULTS
    _topo_cache.update(t=time.time(), v=out)
    return out


# ------------------------------------------------------------------ runs

def _summary(path):
    first = last = outcome = last_diag = None
    agents, n = set(), 0
    with path.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            e = json.loads(line)
            n += 1
            first = first or e
            last = e
            if e["type"] == "outcome":
                outcome = e["data"]
            if e["type"] == "node_start" and e["agent"].startswith("specialist:"):
                agents.add(e["agent"].split(":", 1)[1])
            if e["type"] == "diagnosis":
                last_diag = e["data"].get("root_cause")
    return {"run_id": path.stem, "started": first["at"] if first else None,
            "seconds": round(last["t"] - first["t"], 1) if first else None, "events": n,
            "specialists": sorted(agents), "outcome": outcome,
            "root_cause": last_diag, "live": path.stem in RUNS
            and not RUNS[path.stem]["done"]}


@app.get("/api/runs")
def runs():
    files = sorted(EVENTS_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [_summary(p) for p in files[:60]]


@app.get("/api/runs/{run_id}/events")
def events(run_id: str):
    path = EVENTS_DIR / f"{run_id}.jsonl"
    if not path.exists() or "/" in run_id:
        raise HTTPException(404, "no such run")
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


@app.get("/api/runs/{run_id}/stream")
async def stream(run_id: str):
    path = EVENTS_DIR / f"{run_id}.jsonl"
    if "/" in run_id:
        raise HTTPException(404)

    async def gen():
        pos, idle = 0, 0
        while True:
            if path.exists():
                with path.open(encoding="utf-8") as f:
                    f.seek(pos)
                    chunk = f.read()
                    pos = f.tell()
                for line in chunk.splitlines():
                    if line.strip():
                        idle = 0
                        yield f"data: {line}\n\n"
                        if json.loads(line)["type"] == "outcome":
                            yield "event: end\ndata: {}\n\n"
                            return
            r = RUNS.get(run_id)
            if r and r.get("pending") is not None:
                yield f"event: pending\ndata: {json.dumps(r['pending'], default=str)}\n\n"
            if r and r.get("error"):
                yield f"event: error\ndata: {json.dumps({'error': r['error']})}\n\n"
                return
            idle += 1
            if idle > 2400:          # 10 minutes without a new event
                return
            await asyncio.sleep(0.25)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


def _worker(run_id, cfg):
    r = RUNS[run_id]
    thread = {"configurable": {"thread_id": run_id}}
    try:
        result = GRAPH.invoke({"run_id": run_id, "cfg": cfg, "revisions": 0}, thread)
        while "__interrupt__" in result:
            r["pending"] = result["__interrupt__"][0].value
            r["event"].wait(timeout=1800)
            answer = r.get("answer") or {"approve": [], "by": "timeout"}
            r["pending"], r["answer"] = None, None
            r["event"].clear()
            result = GRAPH.invoke(Command(resume=answer), thread)
    except Exception as e:      # shown in the console; the run file keeps what happened so far
        r["error"] = f"{type(e).__name__}: {e}"
    finally:
        r["done"] = True


@app.post("/api/runs")
def start(body: dict):
    with LOCK:
        if any(not r["done"] for r in RUNS.values()):
            raise HTTPException(409, "a run is already in progress (one incident at a time)")
        run_id = new_run_id()
        cfg = default_cfg(dry_run=body.get("mode", "dry") != "live", hitl=bool(body.get("hitl")),
                          knowledge=body.get("knowledge", True))
        RUNS[run_id] = {"pending": None, "answer": None, "event": threading.Event(),
                        "done": False, "error": None, "cfg": cfg}
        t = threading.Thread(target=_worker, args=(run_id, cfg), daemon=True)
        RUNS[run_id]["thread"] = t
        t.start()
    return {"run_id": run_id, "dry_run": cfg["dry_run"], "hitl": cfg["hitl"]}


@app.post("/api/runs/{run_id}/decision")
def decide(run_id: str, body: dict):
    r = RUNS.get(run_id)
    if not r or r.get("pending") is None:
        raise HTTPException(409, "this run is not waiting for a decision")
    r["answer"] = {"approve": [int(i) for i in body.get("approve", [])], "by": "console"}
    r["event"].set()
    return {"ok": True}


# ------------------------------------------------------------------ faults and switches

@app.post("/api/faults/{name}/{op}")
def fault(name: str, op: str):
    if name not in FAULTS or op not in ("inject", "clear"):
        raise HTTPException(404, "unknown fault or operation")
    from novel_faults import Injector
    t = time.time()
    try:
        msg = Injector.inject(name) if op == "inject" else Injector.clear(name)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=500)
    _topo_cache["t"] = 0
    return {"ok": True, "message": str(msg)[-300:], "seconds": round(time.time() - t, 1)}


@app.post("/api/pause")
def pause(body: dict):
    if body.get("on"):
        governor.PAUSE_FILE.touch()
    else:
        governor.PAUSE_FILE.unlink(missing_ok=True)
    _topo_cache["t"] = 0
    return {"paused": governor.PAUSE_FILE.exists()}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


def main():
    import argparse

    import uvicorn
    p = argparse.ArgumentParser(description="OpsMind console")
    p.add_argument("--port", type=int, default=8765)
    a = p.parse_args()
    EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
