"""OpsMind v2: the incident pipeline as a LangGraph multi-agent graph.

Specialist agents investigate in parallel with narrow read-only toolsets, a commander agent plans,
a critic agent reviews, and deterministic code still makes every decision with consequences
(routing, evidence grounding, action mapping, policy, governor, execution targets, verification).
See opsgraph/graph.py for the graph and docs/ARCHITECTURE.md for the design rationale.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "agents"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
