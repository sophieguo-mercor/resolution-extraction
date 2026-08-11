#!/usr/bin/env python3
"""
Roll the hybrid per-ticket traces up into a per-workflow decision graph.

Reads  results/graph_raw/*.jsonl   (written by extract_graph.py)
Writes results/graph.json

The graph is a Directly-Follows Graph (process-mining style): nodes are the
distinct coded `action:object` states a workflow passes through, edges are the
transitions actually observed between consecutive states, and every count is
ticket support over the whole population — so branch percentages are real, not a
model's impression of a sample.

Each node carries a bag of the free-text `intent` strings seen on it, and each
edge a bag of coded `guard`s + free-text `guard_detail`s. label_nodes.py (step 4)
summarises those bags into display labels; nothing here calls an LLM.

Deterministic and fully local: no network, no secrets, no LLM. The internal
per-node/per-edge ticket-id sets used for de-duplicated counting are dropped
before serialisation, so graph.json holds only counts + distilled phrases.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

START = "__start__"

# Outcome -> terminal node class (matches the target flowchart's vocabulary).
_OUTCOME_CLS = {
    "resolved_first_contact": "done",
    "resolved_after_followup": "done",
    "resolved_unspecified": "done",
    "no_action": "done",
    "customer_wait": "block",
    "reopened": "block",
    "awaiting_approval": "block",
    "escalated_internal": "hand",
    "escalated_vendor": "hand",
    "other": "done",
}


def out_node_id(outcome: str) -> str:
    return f"out:{outcome}"


def canonical_sequence(trace: list[dict]):
    """Collapse adjacent duplicate `action:object` states so the spine stays legible.

    Returns (seq, retries):
      - seq: the collapsed [(key, step), ...] in observed order (first occurrence kept).
      - retries: the collapsed-away duplicates that carried a guard (e.g. a
        `still_failing` retry) — rendered as self-loops so that branch survives
        instead of being mislabelled onto the entering edge.
    """
    seq: list[tuple[str, dict]] = []
    retries: list[tuple[str, dict]] = []
    for s in trace:
        key = f'{s["action"]}:{s["object"]}'
        if seq and seq[-1][0] == key:
            if s.get("guard"):
                retries.append((key, s))
            continue
        seq.append((key, s))
    return seq, retries


def build_graph(records: list[dict], phase_of: dict[str, str], drift_pct: float,
                guard_dec: bool = True) -> dict:
    n = len(records)
    nodes: dict[str, dict] = {}
    edges: dict[tuple[str, str], dict] = {}
    trigger_counter: Counter = Counter()
    trigger_intents: Counter = Counter()
    outcome_counter: Counter = Counter()
    outcome_intents: dict[str, Counter] = defaultdict(Counter)

    def node(key: str, phase: str | None) -> dict:
        nd = nodes.get(key)
        if nd is None:
            nd = nodes[key] = {
                "phase": phase,
                "systems": Counter(),
                "intents": Counter(),
                "_tickets": set(),
            }
        return nd

    def edge(src: str, dst: str) -> dict:
        e = edges.get((src, dst))
        if e is None:
            e = edges[(src, dst)] = {
                "guards": Counter(),
                "guard_details": Counter(),
                "_tickets": set(),
            }
        return e

    for idx, r in enumerate(records):
        tid = (r.get("company"), r.get("ticket_id"), idx)  # unique per record
        trigger_counter[r.get("trigger", "other")] += 1
        if r.get("trigger_intent"):
            trigger_intents[r["trigger_intent"]] += 1
        outcome = r.get("outcome", "other")
        outcome_counter[outcome] += 1
        if r.get("outcome_intent"):
            outcome_intents[outcome][r["outcome_intent"]] += 1

        trace = r.get("trace") or []
        # per-occurrence node stats (systems, intents) over the FULL trace
        for s in trace:
            nd = node(f'{s["action"]}:{s["object"]}', phase_of.get(s["action"], "admin"))
            nd["systems"][s.get("system", "unknown")] += 1
            if s.get("intent"):
                nd["intents"][s["intent"]] += 1

        seq, retries = canonical_sequence(trace)

        # ticket-level node support (distinct keys) + the terminal outcome node
        for key in {k for k, _ in seq}:
            node(key, None)["_tickets"].add(tid)
        onode = out_node_id(outcome)
        node(onode, "outcome")["_tickets"].add(tid)

        def bump(src, dst, step):
            e = edge(src, dst)
            e["_tickets"].add(tid)
            if step and step.get("guard"):
                e["guards"][step["guard"]] += 1
            if step and step.get("guard_detail"):
                e["guard_details"][step["guard_detail"]] += 1

        # directly-follows edges; guard belongs to the transition INTO a step
        if not seq:
            bump(START, onode, None)
        else:
            bump(START, seq[0][0], seq[0][1])
            for (k1, _), (k2, s2) in zip(seq, seq[1:]):
                bump(k1, k2, s2)
            bump(seq[-1][0], onode, None)
        # guarded retries -> self-loops (e.g. still_failing: reset -> reset)
        for key, s in retries:
            bump(key, key, s)

    # ── finalise edges: counts, pct, main-vs-drift split ─────────────────────
    def pct(c: int) -> float:
        return round(c / n * 100, 1) if n else 0.0

    main_edges, drift_edges = [], []
    main_out_degree: Counter = Counter()
    guarded_out: set = set()
    for (src, dst), e in edges.items():
        c = len(e["_tickets"])
        rec = {
            "src": src, "dst": dst, "count": c, "pct": pct(c),
            "top_guards": [{"guard": g, "count": k} for g, k in e["guards"].most_common(5)],
            "top_guard_details": [{"text": t, "count": k} for t, k in e["guard_details"].most_common(5)],
        }
        if rec["pct"] >= drift_pct:
            main_edges.append(rec)
            if src != dst:  # a self-loop retry is not a branch point
                main_out_degree[src] += 1
                if rec["top_guards"]:
                    guarded_out.add(src)
        else:
            drift_edges.append(rec)
    main_edges.sort(key=lambda x: -x["count"])
    drift_edges.sort(key=lambda x: -x["count"])

    # ── finalise nodes: counts, class, bags ──────────────────────────────────
    node_list = []
    for key, nd in nodes.items():
        c = len(nd["_tickets"])
        if key == START:
            cls = "trig"
        elif key.startswith("out:"):
            cls = _OUTCOME_CLS.get(key[4:], "done")
        else:
            # a decision node genuinely branches (>=2 forward routes) AND at least
            # one of those routes was gated by a recovered guard — otherwise it is
            # just an action whose tickets happened to end in different outcomes.
            # When decisions.json drives classification (guard_dec=False), every
            # non-terminal node starts as `act`; overlay_decisions() promotes the
            # discovered decisions to `dec` with their canonical question label.
            cls = "act"
            if guard_dec and main_out_degree.get(key, 0) >= 2 and key in guarded_out:
                cls = "dec"
        node_list.append({
            "id": key,
            "cls": cls,
            "phase": nd["phase"],
            "count": c,
            "pct": pct(c),
            "top_systems": [{"system": s, "count": k} for s, k in nd["systems"].most_common(5)],
            "top_intents": [{"text": t, "count": k} for t, k in nd["intents"].most_common(5)],
        })
    node_list.sort(key=lambda x: -x["count"])

    # explicit START node (carries the trigger bag) if not already present
    if not any(x["id"] == START for x in node_list):
        node_list.insert(0, {"id": START, "cls": "trig", "phase": None,
                             "count": n, "pct": 100.0, "top_systems": [], "top_intents": []})

    # ── prune to the legible spine ───────────────────────────────────────────
    # At population scale a workflow has hundreds of distinct states and a long
    # tail of thousands of sub-threshold transitions. Only the main-edge spine is
    # renderable, so keep just the nodes it touches; the off-spine states collapse
    # into a compact "also seen" drift summary (the most common ones), not a graph.
    main_ids = {START}
    for e in main_edges:
        main_ids.add(e["src"])
        main_ids.add(e["dst"])
    spine_nodes = [nd for nd in node_list if nd["id"] in main_ids]

    drift_summary = [
        {"key": nd["id"], "count": nd["count"], "pct": nd["pct"], "top_intents": nd["top_intents"]}
        for nd in node_list
        if nd["id"] not in main_ids and nd["id"] != START and not nd["id"].startswith("out:")
    ]
    drift_summary.sort(key=lambda x: -x["count"])
    drift_summary = drift_summary[:8]

    return {
        "n": n,
        "triggers": [{"trigger": t, "count": k, "pct": pct(k)} for t, k in trigger_counter.most_common()],
        "trigger_intents": [{"text": t, "count": k} for t, k in trigger_intents.most_common(5)],
        "outcomes": [{"outcome": o, "count": k, "pct": pct(k)} for o, k in outcome_counter.most_common()],
        "outcome_intents": {o: [{"text": t, "count": k} for t, k in c.most_common(5)]
                            for o, c in outcome_intents.items()},
        "nodes": spine_nodes,
        "edges": main_edges,
        "drift": drift_summary,
        "dropped_edges": len(drift_edges),   # count of sub-threshold transitions (informational)
        "n_spine_nodes": len(spine_nodes),
        "n_tail_nodes": len(node_list) - len(spine_nodes),
    }


# ─────────────────────── decision overlay (Stage 3) ─────────────────────────

def overlay_decisions(graph: dict, dwf: dict | None) -> tuple[int, int, int]:
    """Promote the discovered decisions (normalize_decisions.py) onto the DFG spine.

    Each decision's `coded_context` node becomes a `dec` diamond labelled with the
    canonical question; each branch labels the matching out-edge with the canonical
    answer. Counts stay the DFG's population-true ticket tallies — the overlay only
    reclassifies and relabels. Returns (decisions_matched, decisions_unplaced,
    branches_matched) for reporting.
    """
    if not dwf:
        return (0, 0, 0)
    nodes_by_id = {nd["id"]: nd for nd in graph["nodes"]}
    edges_by = {(e["src"], e["dst"]): e for e in graph["edges"]}
    matched = unplaced = branches = 0
    for d in dwf.get("decisions", []):
        nd = nodes_by_id.get(d["coded_context"])
        if nd is None:
            # the decision sits on a state that fell below the spine threshold —
            # its branches are in the drift tail, not renderable as a diamond here.
            unplaced += 1
            continue
        nd["cls"] = "dec"
        nd["label"] = d["canonical_q"]
        nd["decision_support"] = d["support"]
        matched += 1
        for b in d.get("branches", []):
            e = edges_by.get((d["coded_context"], b["next_hint"]))
            if e is not None:
                e["label"] = b["canonical_a"]
                e["branch_support"] = b["support"]
                branches += 1
    return matched, unplaced, branches


# ────────────────────────────────── main ────────────────────────────────────

def load_records(raw_dir: Path) -> dict[str, list[dict]]:
    files = sorted(raw_dir.glob("*.jsonl"))
    if not files:
        raise SystemExit(f"No .jsonl files in {raw_dir} — run extract_graph.py first.")
    by_wf: dict[str, list[dict]] = defaultdict(list)
    for f in files:
        with f.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                by_wf[rec["workflow"]].append(rec)
    # de-duplicate by (company, ticket_id) — a resumed run can append a record twice
    for wf, recs in by_wf.items():
        seen, uniq = set(), []
        for r in recs:
            key = (r.get("company"), r.get("ticket_id"))
            if key in seen:
                continue
            seen.add(key)
            uniq.append(r)
        by_wf[wf] = uniq
    return by_wf


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw-dir", default="results/graph_raw")
    ap.add_argument("--taxonomy", default="taxonomy.json")
    ap.add_argument("--out", default="results/graph.json")
    ap.add_argument("--decisions", default="results/decisions.json",
                    help="canonical decisions from normalize_decisions.py; overlaid as diamonds when present")
    ap.add_argument("--min-tickets", type=int, default=20)
    ap.add_argument("--drift-pct", type=float, default=2.0, help="edges below this %% of tickets become dashed 'drift'")
    args = ap.parse_args()

    taxonomy = json.loads(Path(args.taxonomy).read_text())
    phase_of = {a: m["phase"] for a, m in taxonomy["actions"].items()}

    by_wf = load_records(Path(args.raw_dir))

    # When decisions.json is present it is authoritative for which nodes are
    # decisions (guard_dec=False disables the older out-degree+guard heuristic).
    dpath = Path(args.decisions)
    decisions = json.loads(dpath.read_text(encoding="utf-8")) if dpath.exists() else None

    graphs = {}
    overlay_tally = [0, 0, 0]
    for wf, recs in by_wf.items():
        if len(recs) < args.min_tickets:
            print(f"  skip (only {len(recs)} tickets): {wf}")
            continue
        g = build_graph(recs, phase_of, args.drift_pct, guard_dec=(decisions is None))
        if decisions is not None:
            m, u, b = overlay_decisions(g, decisions.get(wf))
            overlay_tally[0] += m
            overlay_tally[1] += u
            overlay_tally[2] += b
        graphs[wf] = g

    if not graphs:
        raise SystemExit("Nothing met --min-tickets.")

    if decisions is not None:
        print(f"  decisions overlaid: {overlay_tally[0]} diamonds placed, "
              f"{overlay_tally[1]} unplaced (in drift), {overlay_tally[2]} branches labelled")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(graphs, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\n{'N':>7} {'NODES':>6} {'EDGES':>6} {'DRIFT':>6}  WORKFLOW")
    print("-" * 60)
    for wf in sorted(graphs, key=lambda w: -graphs[w]["n"]):
        g = graphs[wf]
        print(f"{g['n']:>7} {len(g['nodes']):>6} {len(g['edges']):>6} {g['dropped_edges']:>6}  {wf[:40]}")
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
