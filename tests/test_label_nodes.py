#!/usr/bin/env python3
"""Offline unit checks for the node labeller (no API).

Exercises the pure pieces — humanisation, word cap, the LLM-vs-deterministic
split, and the cache-hit application path (which never touches the client).

Run:  python tests/test_label_nodes.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from label_nodes import humanize, cap_words, collect_items, label_workflow, _sig, START


# ── humanise + cap ───────────────────────────────────────────────────────────
assert humanize("reset:password") == "reset password"
assert humanize("out:resolved_first_contact") == "resolved first contact"
assert humanize(START) == "Start"
assert humanize("still_failing") == "still failing"
assert cap_words("one two three four five", 3) == "one two three"


# ── a small graph exercising every routing case ──────────────────────────────
g = {
    "n": 10,
    "triggers": [{"trigger": "password_forgotten", "count": 10, "pct": 100.0}],
    "trigger_intents": [{"text": "locked out over the weekend", "count": 1}],
    "outcomes": [{"outcome": "resolved_first_contact", "count": 10, "pct": 100.0}],
    "outcome_intents": {},  # -> out node has no free text -> deterministic
    "nodes": [
        # START has 1/10 trigger-intent coverage -> below gate -> deterministic (dominant trigger)
        {"id": START, "cls": "trig", "count": 10},
        {"id": "reset:password", "cls": "act", "count": 10, "top_intents": []},   # no intent -> deterministic
        # 6/10 coverage -> above gate -> llm
        {"id": "inform_customer:password", "cls": "act", "count": 10,
         "top_intents": [{"text": "sent via secure link", "count": 6}]},
        {"id": "out:resolved_first_contact", "cls": "done", "count": 10},         # no intent -> deterministic
    ],
    "edges": [
        {"src": "reset:password", "dst": "escalate_internal:password",
         "top_guards": [{"guard": "still_failing"}],
         "top_guard_details": [{"text": "could not reach the server"}]},     # llm branch
        {"src": "reset:password", "dst": "inform_customer:password",
         "top_guards": [{"guard": "resolved_after_action"}],
         "top_guard_details": []},                                           # deterministic (guard only)
        {"src": "__start__", "dst": "reset:password",
         "top_guards": [], "top_guard_details": []},                         # nothing
    ],
    "drift": [],
}

llm_items, deterministic = collect_items("WF", g)
llm_ids = {it["id"] for it in llm_items}
# START and reset fall below the coverage gate -> deterministic, not LLM
assert llm_ids == {"inform_customer:password",
                   "reset:password->escalate_internal:password"}, llm_ids
assert deterministic[START] == "password forgotten"          # dominant trigger, not a 1-ticket intent
assert deterministic["reset:password"] == "reset password"
assert deterministic["out:resolved_first_contact"] == "resolved first contact"
assert deterministic["reset:password->inform_customer:password"] == "resolved after action"
assert "__start__->reset:password" not in deterministic  # unguarded edge gets no label


# ── cache-hit path: all llm items pre-cached -> no API, labels applied ────────
cache = {}
for it in llm_items:
    cache[it["sig"]] = f"LBL:{it['id']}"
# sanity: sig is stable for identical inputs
assert _sig("WF", "inform_customer:password", "sent via secure link", []) in cache

labels, sent = label_workflow(extractor=None, workflow="WF", g=g, cache=cache,
                              max_tokens=2000, dry_run=False)
assert sent == 0, "nothing should be sent when all items are cached"
nodes = {n["id"]: n for n in g["nodes"]}
assert nodes[START]["label"] == "password forgotten"                  # deterministic (below gate)
assert nodes["inform_customer:password"]["label"] == "LBL:inform_customer:password"
assert nodes["reset:password"]["label"] == "reset password"           # deterministic
assert nodes["out:resolved_first_contact"]["label"] == "resolved first contact"
edges = {(e["src"], e["dst"]): e for e in g["edges"]}
assert edges[("reset:password", "escalate_internal:password")]["label"].startswith("LBL:")
assert edges[("reset:password", "inform_customer:password")]["label"] == "resolved after action"
assert "label" not in edges[("__start__", "reset:password")]           # unguarded -> unlabelled

print("all assertions passed")
