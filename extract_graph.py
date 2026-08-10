#!/usr/bin/env python3
"""
Hybrid decision-graph extraction (coded + free-text traces).

Same Anthropic Batch-API machinery as extract.py (shared via batch_runner.py),
but the per-ticket schema is a SUPERSET of the coded one: alongside the ordered
coded steps it captures a presenting `trigger`, per-step branch `guard`s, a
terminal `outcome`, and short free-text `intent` / `*_detail` fields for the
workflow-specific colour. aggregate_graph.py rolls these up into per-workflow
decision graphs; the coded `steps` array is derived from the trace so the
existing aggregate.py / explorer keep working from the same files.

Writes results/graph_raw/*.jsonl and uses its OWN manifest
(results/graph_batch_manifest.json) so a graph batch never collides with a
coded extract.py batch.

Quick start
-----------
    python extract_graph.py --sample 20 --workflows "Password Reset" --dry-run
    python extract_graph.py --sample 20 --workflows "Password Reset"   # then READ the jsonl
    python extract_graph.py --category "Identity & Access"             # the pilot set
"""
from __future__ import annotations

import argparse
import json
import random
import re
from collections import defaultdict
from pathlib import Path

from prompts import build_user_message
from prompts_graph import GRAPH_FEW_SHOT, build_graph_system_prompt
from batch_runner import (
    MAX_REQUESTS_PER_BATCH,
    BaseBatchExtractor,
    _cached_system,
    build_groups,
    collect,
    load_dotenv,
    load_tickets,
    save_manifest,
)

DEFAULT_MODEL = "claude-sonnet-4-6"
GRAPH_MANIFEST_NAME = "graph_batch_manifest.json"

# What a pre-filtered / empty ticket is written as (mirrors the coded {"steps": []}
# freebie, but in the hybrid record shape).
GRAPH_EMPTY = {
    "trigger": "other", "trigger_intent": None,
    "trace": [], "steps": [],
    "outcome": "no_action", "outcome_intent": None,
}


# ────────────────────────────── API plumbing ────────────────────────────────

class GraphExtractor(BaseBatchExtractor):
    """Builds hybrid batch Requests and validates hybrid batch results."""

    def __init__(self, taxonomy: dict, taxonomy_graph: dict, model: str, max_tokens: int = 8000):
        super().__init__(model, max_tokens)
        self.system_prompt = build_graph_system_prompt(taxonomy, taxonomy_graph)
        self.valid_actions = set(taxonomy["actions"])
        self.valid_objects = set(taxonomy["objects"])
        self.valid_systems = set(taxonomy["systems"])
        self.valid_triggers = set(taxonomy_graph["triggers"])
        self.valid_guards = set(taxonomy_graph["guards"])
        self.valid_outcomes = set(taxonomy_graph["outcomes"])

    # -- request construction ----------------------------------------------
    def make_request(self, custom_id: str, notes: list[str], char_limit: int):
        user_msg = build_user_message(notes, char_limit)
        messages = GRAPH_FEW_SHOT + [{"role": "user", "content": user_msg}]
        return self._Request(
            custom_id=custom_id,
            params=self._MessageCreateParamsNonStreaming(
                model=self.model,
                max_tokens=self.max_tokens,
                system=_cached_system(self.system_prompt),
                messages=messages,
            ),
        )

    # -- validation ---------------------------------------------------------
    @staticmethod
    def _clean_text(s, max_words: int = 12):
        """Distil a free-text field: trim, cap length, drop empties. The word cap
        is a hard backstop on data-minimization — even a verbose model reply is
        clipped to a short phrase, never a raw note."""
        if not isinstance(s, str):
            return None
        s = s.strip()
        if not s:
            return None
        return " ".join(s.split()[:max_words])

    def _coerce(self, value, valid: set, fallback: str) -> str:
        v = str(value or "").strip().lower()
        return v if v in valid else fallback

    def _clean_trace(self, trace) -> list[dict]:
        out = []
        if not isinstance(trace, list):
            return out
        for s in trace[:8]:
            if not isinstance(s, dict):
                continue
            g = s.get("guard")
            g = str(g).strip().lower() if isinstance(g, str) and g.strip() else None
            if g is not None and g not in self.valid_guards:
                g = "other"
            out.append({
                "guard": g,
                "guard_detail": self._clean_text(s.get("guard_detail")),
                "action": self._coerce(s.get("action"), self.valid_actions, "other"),
                "object": self._coerce(s.get("object"), self.valid_objects, "other"),
                "system": self._coerce(s.get("system", "unknown"), self.valid_systems, "unknown"),
                "intent": self._clean_text(s.get("intent")),
            })
        return out

    # -- result parsing -----------------------------------------------------
    def parse_result_text(self, text: str, n_tickets: int) -> dict[int, dict]:
        """Parse one succeeded response into {position_in_group: hybrid record}.

        Raises on malformed JSON so the caller can skip the group and let those
        tickets be re-submitted on a later run (they stay absent from JSONL)."""
        text = text.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        parsed = json.loads(text)
        result = {}
        for item in parsed:
            pos = item.get("id")
            if not (isinstance(pos, int) and 0 <= pos < n_tickets):
                continue
            trace = self._clean_trace(item.get("trace"))
            result[pos] = {
                "trigger": self._coerce(item.get("trigger"), self.valid_triggers, "other"),
                "trigger_intent": self._clean_text(item.get("trigger_intent")),
                "trace": trace,
                # derived coded steps — keeps aggregate.py / the explorer compatible
                "steps": [{"action": s["action"], "object": s["object"], "system": s["system"]}
                          for s in trace],
                "outcome": self._coerce(item.get("outcome"), self.valid_outcomes, "other"),
                "outcome_intent": self._clean_text(item.get("outcome_intent")),
            }
        return result


# ─────────────────────────── workflow selection ─────────────────────────────

def resolve_category(category: str, merged_data_path: str) -> list[str]:
    """Return the workflows whose Workflow-Dashboard category matches, by reading
    merged_data.json (produced by run.py's parse stage)."""
    p = Path(merged_data_path)
    if not p.exists():
        raise SystemExit(
            f"--category needs {merged_data_path}; run `python run.py --html-only` or the "
            f"parse stage first, or pass --workflows explicitly."
        )
    data = json.loads(p.read_text(encoding="utf-8"))
    wfs = [w for w in data.get("wf_order", data["workflows"].keys())
           if data["workflows"].get(w, {}).get("cat") == category]
    if not wfs:
        raise SystemExit(f"No workflows found in category {category!r}.")
    return wfs


# ──────────────────────────────── main ──────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--xls", default="data/Time_entries_Mercor_v3_workflows_by_company__1_.xls")
    ap.add_argument("--taxonomy", default="taxonomy.json")
    ap.add_argument("--taxonomy-graph", default="taxonomy_graph.json")
    ap.add_argument("--merged-data", default="merged_data.json", help="source of workflow->category for --category")
    ap.add_argument("--out-dir", default="results")
    ap.add_argument("--workflows", nargs="*", help="only these workflows (default: all, or --category)")
    ap.add_argument("--category", help="target every workflow in this Workflow-Dashboard category")
    ap.add_argument("--sample", type=int, default=0, help="tickets per workflow; 0 = all")
    ap.add_argument("--batch-size", type=int, default=12, help="tickets packed into one batch request")
    ap.add_argument("--char-limit", type=int, default=1500, help="max chars of notes per ticket")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--poll-interval", type=int, default=30, help="seconds between batch status checks")
    ap.add_argument("--submit-only", action="store_true", help="create the batch and exit; collect later")
    ap.add_argument("--collect", action="store_true", help="collect an already-submitted batch (from the manifest)")
    ap.add_argument("--force-new", action="store_true", help="discard an existing manifest and submit a fresh batch")
    ap.add_argument("--dry-run", action="store_true", help="print one prompt and the request estimate, no API calls")
    args = ap.parse_args()

    load_dotenv()
    random.seed(args.seed)
    taxonomy = json.loads(Path(args.taxonomy).read_text())
    taxonomy_graph = json.loads(Path(args.taxonomy_graph).read_text())

    out_dir = Path(args.out_dir)
    raw_dir = out_dir / "graph_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / GRAPH_MANIFEST_NAME

    def new_extractor():
        return GraphExtractor(taxonomy, taxonomy_graph, args.model)

    # ── collect-only path ────────────────────────────────────────────────
    if args.collect:
        if not manifest_path.exists():
            raise SystemExit(f"No {manifest_path} to collect. Submit a batch first.")
        collect(new_extractor(), manifest_path, raw_dir, args.poll_interval, empty_result=GRAPH_EMPTY)
        return

    # ── reconnect to an in-flight batch instead of double-spending ───────
    if manifest_path.exists() and not args.force_new:
        print(f"Found in-flight batch in {manifest_path} — resuming it.", flush=True)
        print("  (use --force-new to discard it and submit a fresh batch)", flush=True)
        collect(new_extractor(), manifest_path, raw_dir, args.poll_interval, empty_result=GRAPH_EMPTY)
        return

    # ── build requests locally ───────────────────────────────────────────
    print(f"Loading {args.xls} …", flush=True)
    tickets = load_tickets(args.xls)
    by_wf: dict[str, list[dict]] = defaultdict(list)
    for t in tickets:
        by_wf[t["workflow"]].append(t)
    print(f"  {len(tickets):,} tickets across {len(by_wf)} workflows")

    if args.workflows:
        targets = args.workflows
    elif args.category:
        targets = resolve_category(args.category, args.merged_data)
        print(f"  category {args.category!r} → {len(targets)} workflows")
    else:
        targets = sorted(by_wf)
    missing = [w for w in targets if w not in by_wf]
    if missing:
        raise SystemExit(f"Unknown workflow(s): {missing}")

    if args.dry_run:
        wf = targets[0]
        batch = by_wf[wf][: args.batch_size]
        print("\n===== SYSTEM PROMPT =====\n")
        print(build_graph_system_prompt(taxonomy, taxonomy_graph))
        print("\n===== USER MESSAGE (first request) =====\n")
        print(build_user_message([t["notes"] for t in batch], args.char_limit)[:4000])
        est = sum(
            -(-min(len(by_wf[w]), args.sample or len(by_wf[w])) // args.batch_size) for w in targets
        )
        print(f"\n===== PLAN =====\nworkflows: {len(targets)}   estimated requests in 1 batch: {est:,}")
        return

    if args.force_new:
        manifest_path.unlink(missing_ok=True)

    print("Building requests (sampling, noise filter, resume-skip) …", flush=True)
    groups, freebies = build_groups(by_wf, targets, args, raw_dir, empty_payload=GRAPH_EMPTY)
    print(f"  {len(groups):,} requests to submit, {freebies:,} empty tickets written locally", flush=True)

    if not groups:
        print("Nothing to submit — everything is already extracted or empty.", flush=True)
        return

    if len(groups) > MAX_REQUESTS_PER_BATCH:
        raise SystemExit(
            f"{len(groups):,} requests exceeds the {MAX_REQUESTS_PER_BATCH:,}/batch limit. "
            f"Narrow with --workflows / --category or --sample."
        )

    extractor = new_extractor()
    requests = [extractor.make_request(cid, g["notes"], args.char_limit) for cid, g in groups.items()]
    print(f"Submitting batch of {len(requests):,} requests …", flush=True)
    batch = extractor.client.messages.batches.create(requests=requests)
    save_manifest(manifest_path, batch.id, args.model, groups)
    print(f"  batch {batch.id}  status={batch.processing_status}  (manifest → {manifest_path})", flush=True)

    if args.submit_only:
        print("Submitted. Collect it later with:  python extract_graph.py --collect", flush=True)
        return

    collect(extractor, manifest_path, raw_dir, args.poll_interval, empty_result=GRAPH_EMPTY)


if __name__ == "__main__":
    main()
