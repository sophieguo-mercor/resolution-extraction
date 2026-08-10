#!/usr/bin/env python3
"""
Give every graph node/branch a concise, human-readable display label.

Reads results/graph.json (from aggregate_graph.py), writes it back with a `label`
on every node and on every guarded edge. Labels are DISPLAY-ONLY — the counts and
topology are already frozen upstream, so nothing here can change a percentage.

For each node the label is an LLM summary of its most-frequent free-text intent
(the mode), with the runners-up passed as context so the summary stays
representative rather than latching onto an outlier. Nodes with no free text get a
deterministic humanised label and cost no API call. One synchronous call per
workflow (not the Batch API — there are only a handful of workflows), cheap model
by default.

Labels are cached on the item's input signature (results/label_cache.json) so
re-runs are stable and only re-label nodes whose intent distribution changed.

Security: the only external call is Anthropic messages.create with the env-
provided key. Its inputs are the already-distilled, word-capped intent/guard
phrases from graph.json — never raw notes. Output labels are word-capped and
written only into aggregated graph.json; the cache stores hashes + short labels.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from batch_runner import BaseBatchExtractor, _cached_system, load_dotenv

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
START = "__start__"

SYSTEM_PROMPT = """You write concise labels for a support-workflow flowchart.

You receive, as JSON, a list of nodes and a list of branches for ONE workflow.
- For each node: write a label of AT MOST 8 words that summarises its `primary`
  text. The `others` are lower-frequency variants for context — stay true to the
  primary; do not invent detail beyond what is given.
- For each branch: write a condition label of AT MOST 6 words from its `primary`
  (the guard detail) — phrase it as the situation that sends a ticket down that
  branch.
NEVER include personal names, email addresses, phone numbers, or credentials.

Return ONLY JSON, no prose, no markdown fences:
{"node_labels": {"<node id>": "<label>"}, "branch_labels": {"<branch id>": "<label>"}}
"""


def humanize(node_id: str) -> str:
    """Deterministic fallback label from a coded id, e.g. 'reset:password' ->
    'reset password', 'out:resolved_first_contact' -> 'resolved first contact'."""
    s = node_id
    if s == START:
        return "Start"
    if s.startswith("out:"):
        s = s[4:]
    return re.sub(r"[:_]+", " ", s).strip()


def cap_words(text: str, n: int = 10) -> str:
    return " ".join(str(text).split()[:n])


def _sig(workflow: str, item_id: str, primary: str, others: list[str]) -> str:
    payload = json.dumps([workflow, item_id, primary, others], ensure_ascii=False)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def freetext_items(g: dict, node: dict) -> list[dict]:
    """The {text, count} free-text bag describing this node, most-frequent first."""
    nid = node["id"]
    if nid == START:
        return g.get("trigger_intents", [])
    if nid.startswith("out:"):
        return g.get("outcome_intents", {}).get(nid[4:], [])
    return node.get("top_intents", [])


def deterministic_label(g: dict, node_id: str) -> str:
    """A clean label with no API call: the humanised coded id, or for START the
    workflow's dominant trigger."""
    if node_id == START and g.get("triggers"):
        return humanize(g["triggers"][0]["trigger"])
    return humanize(node_id)


def collect_items(workflow: str, g: dict, min_coverage: float = 0.34):
    """Return (llm_items, deterministic) where llm_items need an API label and
    deterministic maps item_id -> label we can assign without one.

    A node is summarised by the LLM only when enough of its occurrences actually
    carry an intent (coverage >= min_coverage); otherwise the 'most frequent
    intent' is a vocal minority (sometimes one ticket) that would mislabel the
    node, so we fall back to the deterministic label."""
    llm_items, deterministic = [], {}

    for node in g["nodes"]:
        items = freetext_items(g, node)
        denom = node.get("count") or 0
        coverage = (sum(d.get("count", 0) for d in items) / denom) if denom else 0.0
        if items and coverage >= min_coverage:
            texts = [d["text"] for d in items]
            llm_items.append({
                "kind": "node", "id": node["id"],
                "sig": _sig(workflow, node["id"], texts[0], texts[1:4]),
                "primary": texts[0], "others": texts[1:4],
            })
        else:
            deterministic[node["id"]] = deterministic_label(g, node["id"])

    for e in [*g.get("edges", []), *g.get("drift", [])]:
        details = [d["text"] for d in e.get("top_guard_details", [])]
        guards = [d["guard"] for d in e.get("top_guards", [])]
        eid = f'{e["src"]}->{e["dst"]}'
        if details:
            llm_items.append({
                "kind": "branch", "id": eid,
                "sig": _sig(workflow, eid, details[0], details[1:3]),
                "primary": details[0], "others": details[1:3],
            })
        elif guards:
            deterministic[eid] = humanize(guards[0])

    return llm_items, deterministic


def label_workflow(extractor, workflow: str, g: dict, cache: dict, max_tokens: int, dry_run: bool,
                   min_coverage: float = 0.34):
    llm_items, deterministic = collect_items(workflow, g, min_coverage)

    # split into cached vs to-send
    to_send = [it for it in llm_items if it["sig"] not in cache]
    labels = {it["id"]: cache[it["sig"]] for it in llm_items if it["sig"] in cache}
    labels.update(deterministic)

    if dry_run:
        print(f"  {workflow}: {len(llm_items)} llm items ({len(to_send)} uncached), "
              f"{len(deterministic)} deterministic")
        return labels, len(to_send)

    if to_send:
        payload = {
            "nodes":    [{"id": it["id"], "primary": it["primary"], "others": it["others"]}
                         for it in to_send if it["kind"] == "node"],
            "branches": [{"id": it["id"], "primary": it["primary"], "others": it["others"]}
                         for it in to_send if it["kind"] == "branch"],
        }
        msg = extractor.client.messages.create(
            model=extractor.model,
            max_tokens=max_tokens,
            system=_cached_system(SYSTEM_PROMPT),
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        )
        text = "".join(b.text for b in msg.content if b.type == "text").strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        parsed = json.loads(text)
        returned = {**parsed.get("node_labels", {}), **parsed.get("branch_labels", {})}
        for it in to_send:
            lbl = returned.get(it["id"])
            lbl = cap_words(lbl) if isinstance(lbl, str) and lbl.strip() else humanize(it["id"])
            cache[it["sig"]] = lbl
            labels[it["id"]] = lbl

    # apply onto the graph
    for node in g["nodes"]:
        if node["id"] in labels:
            node["label"] = labels[node["id"]]
    for e in [*g.get("edges", []), *g.get("drift", [])]:
        eid = f'{e["src"]}->{e["dst"]}'
        if eid in labels:
            e["label"] = labels[eid]

    return labels, len([it for it in to_send])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--graph", default="results/graph.json")
    ap.add_argument("--out", default=None, help="default: overwrite --graph in place")
    ap.add_argument("--cache", default="results/label_cache.json")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--max-tokens", type=int, default=2000)
    ap.add_argument("--min-intent-coverage", type=float, default=0.34,
                    help="only summarise a node's intent when >= this share of its "
                         "occurrences carry one; else use a humanised label")
    ap.add_argument("--dry-run", action="store_true", help="list what would be labelled, no API calls")
    args = ap.parse_args()

    load_dotenv()
    graph_path = Path(args.graph)
    graphs = json.loads(graph_path.read_text(encoding="utf-8"))

    cache_path = Path(args.cache)
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}

    extractor = None
    if not args.dry_run:
        extractor = BaseBatchExtractor(args.model)  # builds client, checks ANTHROPIC_API_KEY

    total_sent = 0
    for wf in sorted(graphs):
        _, sent = label_workflow(extractor, wf, graphs[wf], cache, args.max_tokens,
                                 args.dry_run, args.min_intent_coverage)
        total_sent += sent

    if args.dry_run:
        print(f"\nWould send {total_sent} items across {len(graphs)} workflow calls.")
        return

    out_path = Path(args.out) if args.out else graph_path
    out_path.write_text(json.dumps(graphs, indent=2, ensure_ascii=False), encoding="utf-8")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    print(f"Labelled {len(graphs)} workflows ({total_sent} items sent, {len(cache)} cached) → {out_path}")


if __name__ == "__main__":
    main()
