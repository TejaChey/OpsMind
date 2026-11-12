"""Event stream for one graph run: every node start/end, tool call, finding, decision, gate
check, action and verification becomes one JSON line in trajectories/graph/<run_id>.jsonl.

The console (console/server.py) streams that file to the browser, so a live run and a replay
of a recorded run are shown by exactly the same code. Events also go to LangGraph's custom
stream when a writer is available."""
import itertools
import time
from datetime import datetime, timezone
from pathlib import Path

from tools.jsonl_io import append_jsonl

EVENTS_DIR = Path(__file__).resolve().parent.parent / "trajectories" / "graph"
_seq = itertools.count()


class Emitter:
    def __init__(self, run_id, events_dir=None):
        self.run_id = run_id
        self.path = Path(events_dir or EVENTS_DIR) / f"{run_id}.jsonl"

    def emit(self, type, agent=None, data=None):
        ev = {"run_id": self.run_id, "seq": next(_seq), "t": time.time(),
              "at": datetime.now(timezone.utc).isoformat(), "type": type, "agent": agent,
              "data": data or {}}
        append_jsonl(self.path, ev)
        try:
            from langgraph.config import get_stream_writer
            get_stream_writer()(ev)
        except Exception:      # outside a graph run there is no writer: the file is enough
            pass
        return ev

    def node(self, name, phase):
        return self.emit(type=f"node_{phase}", agent=name, data={"node": name})
