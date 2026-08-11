#!/usr/bin/env python3
"""Offline unit checks for the decision-normalization stage.

No network: only the pure functions (aggregation, payload shaping, merge
application, gating) are exercised, with a hand-written merge result standing in
for the LLM. Counts must come only from the ticket tallies, never the merge.

Run:  python tests/test_normalize_decisions.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from normalize_decisions import (  # noqa: E402
    canon, collect_workflow, build_payload, apply_merge, _no_merge,
)

WF = "RMM"


def ticket(tid, dq, da, nxt=("verify", "service"), outcome="resolved_first_contact"):
    """One ticket whose first step carries a decision, followed by a coded step."""
    return {
        "company": "acme", "ticket_id": tid, "workflow": WF, "outcome": outcome,
        "trace": [
            {"action": "restart", "object": "device_desktop",
             "decision_q": dq, "decision_a": da},
            {"action": nxt[0], "object": nxt[1], "decision_q": None, "decision_a": None},
        ],
    }


records = []
# q1: "is the device online and rebooted?" — 12 tickets (t0..t11)
for i in range(9):   # answer: yes -> verify:service
    records.append(ticket(f"t{i}", "is the device online and rebooted?", "yes, back online"))
for i in range(9, 12):  # answer: no -> await_response
    records.append(ticket(f"t{i}", "is the device online and rebooted?", "no, still offline",
                          nxt=("await_response", "user_account")))
# q1 paraphrase: "device online after reboot?" — 2 tickets (t12,t13), answer yes
for i in range(12, 14):
    records.append(ticket(f"t{i}", "device online after reboot?", "yes back online"))
# q2 rare: "is the license valid?" — 1 ticket (t14)
records.append(ticket("t14", "is the license valid?", "yes"))
# pad to n=20 with decision-less tickets (they contribute to n, not to any decision)
for i in range(15, 20):
    records.append({"company": "acme", "ticket_id": f"t{i}", "workflow": WF,
                    "outcome": "no_action", "trace": []})

agg = collect_workflow(records)
assert agg["n"] == 20, agg["n"]

payload = build_payload(WF, agg)
# distinct questions ranked by support: q1(12) > paraphrase(2) > q2(1)
texts = [q["text"] for q in payload["questions"]]
assert texts[0] == "is the device online and rebooted?", texts
assert payload["questions"][0]["pct"] == 60.0, payload["questions"][0]["pct"]
assert payload["questions"][0]["context"] == "restart:device_desktop"
# answer ids are "<qid>.<aid>", most-frequent answer first
q0_answers = {a["id"]: a["text"] for a in payload["questions"][0]["answers"]}
assert q0_answers["0.0"] == "yes, back online" and q0_answers["0.1"] == "no, still offline", q0_answers

# ── merge result (what the LLM would return): fold paraphrase into q1 ──────────
merge = {"decisions": [
    {"canonical_q": "Is the device online and rebooted?",
     "question_ids": [0, 1],
     "branches": [
         {"canonical_a": "Yes, back online", "answer_ids": ["0.0", "1.0"]},
         {"canonical_a": "No, still offline", "answer_ids": ["0.1"]},
     ]},
    # q2 (id 2) intentionally omitted → must be recovered as its own decision
]}

res = apply_merge(WF, agg, payload, merge, min_decision=8.0, min_branch=3.0, max_branches=4)
assert res["n"] == 20
# q1+paraphrase merged (70%) kept; q2 (5%) recovered but gated out at 8%
assert len(res["decisions"]) == 1, [d["canonical_q"] for d in res["decisions"]]
d = res["decisions"][0]
assert d["id"] == "d0"
assert d["canonical_q"] == "Is the device online and rebooted?"
assert d["count"] == 14 and d["support"] == 70.0, (d["count"], d["support"])
# branches sorted by support desc; counts are raw ticket tallies
labels = [(b["canonical_a"], b["count"], b["support"]) for b in d["branches"]]
assert labels == [("Yes, back online", 11, 55.0), ("No, still offline", 3, 15.0)], labels
assert d["branches"][0]["next_hint"] == "verify:service"
assert d["branches"][1]["next_hint"] == "await_response:user_account"
assert set(d["members"]) == {"is the device online and rebooted?", "device online after reboot?"}

# ── no-merge fallback: every question its own decision, same counts ───────────
fb = apply_merge(WF, agg, payload, _no_merge(payload), min_decision=8.0, min_branch=3.0)
# q1 (60%) and paraphrase (10%) survive 8%; q2 (5%) gated. Merge NOT applied.
kept = sorted(d["canonical_q"] for d in fb["decisions"])
assert kept == ["device online after reboot?", "is the device online and rebooted?"], kept
q1 = next(d for d in fb["decisions"] if d["canonical_q"] == "is the device online and rebooted?")
assert q1["count"] == 12 and q1["support"] == 60.0, (q1["count"], q1["support"])

# ── a garbled merge must never crash or invent support ───────────────────────
for junk in [{}, {"decisions": "nope"}, {"decisions": [{"question_ids": [999]}]}, None]:
    r = apply_merge(WF, agg, payload, junk, min_decision=8.0, min_branch=3.0)
    assert r["n"] == 20
    assert all(d["support"] >= 8.0 for d in r["decisions"])

# ── canon() basics ────────────────────────────────────────────────────────────
assert canon("Is the DEVICE online?!") == "is the device online"
assert canon(None) == "" and canon("   ") == ""

print("all assertions passed")
