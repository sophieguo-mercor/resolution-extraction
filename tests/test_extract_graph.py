#!/usr/bin/env python3
"""Offline unit checks for the hybrid extractor and the shared JSONL sink.

No network: the Anthropic client is constructed with a dummy key (its __init__
makes no request) and we only exercise pure validation/parsing + file writing.

Run:  python tests/test_extract_graph.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")

try:
    from extract_graph import GraphExtractor, GRAPH_EMPTY
    from batch_runner import JsonlWriter
except SystemExit as e:  # pragma: no cover - anthropic missing
    print(f"skip: {e}")
    sys.exit(0)

ROOT = Path(__file__).resolve().parent.parent
taxonomy = json.loads((ROOT / "taxonomy.json").read_text())
taxonomy_graph = json.loads((ROOT / "taxonomy_graph.json").read_text())

ex = GraphExtractor(taxonomy, taxonomy_graph, model="claude-sonnet-4-6")

# ── parse_result_text: coercion, free-text cap, derived steps, id bounds ──────
model_reply = json.dumps([
    {
        "id": 0,
        "trigger": "password_forgotten",
        "trigger_intent": "  locked out over the weekend  ",
        "trace": [
            {"guard": "hybrid_account", "guard_detail": "synced account",
             "decision_q": "is the account hybrid-synced?", "decision_a": "yes, reset in AD",
             "action": "reset", "object": "password", "system": "active_directory", "intent": "manual AD reset"},
            {"guard": "NONSENSE_GUARD", "guard_detail": None,
             "decision_q": "one two three four five six seven eight nine ten eleven twelve",
             "decision_a": "one two three four five six seven eight nine ten",
             "action": "frobnicate", "object": "widget", "system": "mainframe",
             "intent": "one two three four five six seven eight nine ten eleven twelve thirteen fourteen"},
            {"guard": None, "guard_detail": None,
             "action": "verify", "object": "user_account", "system": "unknown", "intent": None},
        ],
        "outcome": "resolved_first_contact",
        "outcome_intent": None,
    },
    {"id": 1, "trigger": "junk_trigger", "trace": [], "outcome": "junk_outcome", "outcome_intent": None},
    {"id": 99, "trigger": "other", "trace": [], "outcome": "no_action"},  # out of range -> dropped
])

res = ex.parse_result_text(model_reply, n_tickets=2)

assert set(res.keys()) == {0, 1}, f"id bounds not enforced: {res.keys()}"

r0 = res[0]
assert r0["trigger"] == "password_forgotten"
assert r0["trigger_intent"] == "locked out over the weekend", "free text not trimmed"
# step 1: valid coded values kept, guard kept, decision pair carried through
s1 = r0["trace"][0]
assert (s1["action"], s1["object"], s1["system"], s1["guard"]) == ("reset", "password", "active_directory", "hybrid_account")
assert s1["decision_q"] == "is the account hybrid-synced?", s1["decision_q"]
assert s1["decision_a"] == "yes, reset in AD", s1["decision_a"]
# step 2: unknown coded values coerced, unknown guard -> "other", free text capped
s2 = r0["trace"][1]
assert (s2["action"], s2["object"], s2["system"]) == ("other", "other", "unknown"), s2
assert s2["guard"] == "other", s2["guard"]
assert len(s2["intent"].split()) == 12, f"intent not capped: {s2['intent']!r}"
assert len(s2["decision_q"].split()) == 10, f"decision_q not capped to 10: {s2['decision_q']!r}"
assert len(s2["decision_a"].split()) == 8, f"decision_a not capped to 8: {s2['decision_a']!r}"
# step 3: decision keys absent in the model reply -> default to None (not KeyError)
s3 = r0["trace"][2]
assert s3["decision_q"] is None and s3["decision_a"] is None, s3
# derived coded steps mirror the trace triples, no free text (no decision fields)
assert r0["steps"] == [
    {"action": "reset", "object": "password", "system": "active_directory"},
    {"action": "other", "object": "other", "system": "unknown"},
    {"action": "verify", "object": "user_account", "system": "unknown"},
], r0["steps"]

r1 = res[1]
assert r1["trigger"] == "other" and r1["outcome"] == "other", (r1["trigger"], r1["outcome"])
assert r1["trace"] == [] and r1["steps"] == []

# ── malformed JSON raises (so the group is re-submitted, not silently dropped) ─
try:
    ex.parse_result_text("not json at all", 3)
    raise AssertionError("expected malformed JSON to raise")
except json.JSONDecodeError:
    pass

# ── JsonlWriter: dict payload merges, list payload becomes {"steps": [...]} ────
with tempfile.TemporaryDirectory() as d:
    w = JsonlWriter(Path(d))
    stub = {"ticket_id": "T1", "company": "acme", "workflow": "Password Reset", "hours": 0.25, "touches": 1}
    assert w.emit(stub, GRAPH_EMPTY) is True
    assert w.emit(stub, GRAPH_EMPTY) is False, "not idempotent on repeat (company,id)"
    stub2 = {**stub, "ticket_id": "T2"}
    assert w.emit(stub2, [{"action": "reset", "object": "password", "system": "unknown"}]) is True
    w.close()
    lines = [json.loads(l) for l in (Path(d) / "Password_Reset.jsonl").read_text().splitlines()]
    assert lines[0]["outcome"] == "no_action" and lines[0]["trace"] == [], lines[0]
    assert "notes" not in lines[0], "raw notes must never be persisted"
    assert lines[1]["steps"] == [{"action": "reset", "object": "password", "system": "unknown"}], lines[1]
    assert "trigger" not in lines[1], "list payload should not gain hybrid keys"

print("all assertions passed")
