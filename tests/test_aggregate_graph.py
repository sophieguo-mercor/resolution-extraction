#!/usr/bin/env python3
"""Offline unit checks for the decision-graph aggregator (no network).

Run:  python tests/test_aggregate_graph.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aggregate_graph import build_graph, canonical_sequence, START, out_node_id

PHASE_OF = {"reset": "change", "inform_customer": "coordinate",
            "escalate_internal": "coordinate", "contact_customer": "coordinate"}


def step(action, obj, system="unknown", guard=None, guard_detail=None, intent=None):
    return {"action": action, "object": obj, "system": system,
            "guard": guard, "guard_detail": guard_detail, "intent": intent}


def rec(trace, outcome, company="c", tid="t", trigger="password_forgotten"):
    return {"company": company, "ticket_id": tid, "workflow": "WF",
            "trigger": trigger, "trigger_intent": None,
            "trace": trace, "steps": [], "outcome": outcome, "outcome_intent": None}


# ── canonical_sequence: collapse + guarded-retry capture ─────────────────────
seq, retries = canonical_sequence([
    step("reset", "password"),
    step("reset", "password", guard="still_failing", guard_detail="first try failed"),
    step("inform_customer", "password"),
])
assert [k for k, _ in seq] == ["reset:password", "inform_customer:password"], seq
assert len(retries) == 1 and retries[0][1]["guard"] == "still_failing", retries

# ── build a 10-ticket workflow with a clean spine + a branch + an empty ──────
records = []
for i in range(6):  # 60%: reset -> inform -> resolved
    records.append(rec([step("reset", "password"),
                        step("inform_customer", "password", intent=f"sent via link {i%2}")],
                       "resolved_first_contact", tid=f"a{i}"))
for i in range(2):  # 20%: reset -> escalate -> escalated_internal
    records.append(rec([step("reset", "password", guard="self_service_failed"),
                        step("escalate_internal", "password", guard="still_failing",
                             guard_detail="could not connect to DC")],
                       "escalated_internal", tid=f"b{i}"))
records.append(rec([], "no_action", tid="e0"))                       # 10%: empty
records.append(rec([step("reset", "password"),                        # 10%: retry self-loop
                    step("reset", "password", guard="still_failing", guard_detail="retry"),
                    step("inform_customer", "password")],
                   "resolved_after_followup", tid="r0"))

g = build_graph(records, PHASE_OF, drift_pct=2.0)

assert g["n"] == 10, g["n"]

nodes = {x["id"]: x for x in g["nodes"]}
edges = {(e["src"], e["dst"]): e for e in g["edges"]}

# node support counts (ticket-level): reset in 9 tickets, inform in 7
assert nodes["reset:password"]["count"] == 9, nodes["reset:password"]["count"]
assert nodes["inform_customer:password"]["count"] == 7
assert nodes[out_node_id("resolved_first_contact")]["count"] == 6
assert nodes[out_node_id("no_action")]["count"] == 1

# reset is a branch point (-> inform AND -> escalate), so it's a decision node
assert nodes["reset:password"]["cls"] == "dec", nodes["reset:password"]["cls"]
assert nodes["inform_customer:password"]["cls"] == "act"
assert nodes[out_node_id("escalated_internal")]["cls"] == "hand"
assert nodes[out_node_id("no_action")]["cls"] == "done"

# edges + pct (ticket support / n)
assert edges[(START, "reset:password")]["count"] == 9
assert edges[(START, out_node_id("no_action"))]["pct"] == 10.0
e_esc = edges[("reset:password", "escalate_internal:password")]
assert e_esc["count"] == 2 and e_esc["pct"] == 20.0
# the guard that gated the escalation was recovered onto that edge
assert e_esc["top_guards"][0]["guard"] == "still_failing", e_esc["top_guards"]

# guarded retry -> self-loop edge on reset:password
assert ("reset:password", "reset:password") in edges, "retry self-loop missing"
assert edges[("reset:password", "reset:password")]["top_guards"][0]["guard"] == "still_failing"

# free-text intents bagged on the inform node (2 distinct link variants)
inform_intents = {d["text"] for d in nodes["inform_customer:password"]["top_intents"]}
assert inform_intents == {"sent via link 0", "sent via link 1"}, inform_intents

# outcomes summary present
outc = {o["outcome"]: o["pct"] for o in g["outcomes"]}
assert outc["resolved_first_contact"] == 60.0 and outc["escalated_internal"] == 20.0, outc

# ── drift split: a rare transition falls below the threshold ─────────────────
records2 = [rec([step("reset", "password"), step("inform_customer", "password")],
                "resolved_first_contact", tid=f"x{i}") for i in range(49)]
records2.append(rec([step("reset", "password"), step("investigate", "user_account")],
                    "reopened", tid="rare"))  # 1/50 = 2% exactly is kept; make it clearly <2%
records2 += [rec([step("reset", "password"), step("inform_customer", "password")],
                 "resolved_first_contact", tid=f"y{i}") for i in range(50)]  # n=100, rare=1%
PHASE_OF["investigate"] = "diagnose"
g2 = build_graph(records2, PHASE_OF, drift_pct=2.0)
main = {(e["src"], e["dst"]) for e in g2["edges"]}
drift = {(e["src"], e["dst"]) for e in g2["drift"]}
assert ("reset:password", "investigate:user_account") in drift, "rare edge should be drift"
assert ("reset:password", "investigate:user_account") not in main
assert g2["dropped_edges"] == len(g2["drift"]) >= 1

print("all assertions passed")
