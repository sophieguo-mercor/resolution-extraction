#!/usr/bin/env python3
"""
Extract structured resolution steps from ticket Notes using the Anthropic
Message Batches API.

Reads the SpreadsheetML .xls export, groups tickets per workflow, packs 12 tickets
into each request, submits them all as a single Anthropic batch, then collects the
results and writes one JSONL file per workflow.

Why the Batch API: the whole corpus is ~10k requests. Run synchronously they're
gated by your per-minute rate limit (hence the old thread pool). The batch queue is
a separate, higher-throughput lane and costs 50% less per token — you trade latency
(minutes to a few hours, no SLA) for throughput and price.

Resume-safe on two levels:
  1. JSONL — ticket IDs already present in the output are skipped when building
     requests, and again when writing results, so re-runs never duplicate.
  2. Batch — an in-flight batch is recorded in results/batch_manifest.json. Re-run
     (or Ctrl-C then re-run) and it reconnects to the same batch instead of
     submitting — and paying for — a second one.

The reusable batch machinery (spreadsheet loading, noise pre-filter, JSONL sink,
manifest, submit/poll/collect) lives in batch_runner.py so extract_graph.py can
share it; this module keeps only the coded-step prompt/validation and the CLI.

Quick start
-----------
    cp .env.example .env && edit it        # or: export ANTHROPIC_API_KEY=sk-ant-...
    pip install -r requirements.txt
    # place the export at data/Time_entries_Mercor_v3_workflows_by_company__1_.xls

    # 1. See what would be sent, without spending anything
    python extract.py --sample 20 --workflows "Password Reset" --dry-run

    # 2. Small real run — submits a batch, waits, writes results/raw/Password_Reset.jsonl
    python extract.py --sample 20 --workflows "Password Reset"

    # 3. Full run. Submit and walk away; re-run to reconnect and collect.
    python extract.py
"""
from __future__ import annotations

import argparse
import json
import random
import re
from collections import defaultdict
from pathlib import Path

from prompts import FEW_SHOT, build_system_prompt, build_user_message
from batch_runner import (
    MANIFEST_NAME,
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


# ────────────────────────────── API plumbing ────────────────────────────────

class BatchExtractor(BaseBatchExtractor):
    """Builds batch Requests and parses batch results against the taxonomy.

    No network in the constructor beyond client init; submission/polling live in the
    runner so the same object serves --dry-run, submit, and collect paths.
    """

    def __init__(self, taxonomy: dict, model: str, max_tokens: int = 4000):
        super().__init__(model, max_tokens)
        self.taxonomy = taxonomy
        self.system_prompt = build_system_prompt(taxonomy)
        self.valid_actions = set(taxonomy["actions"])
        self.valid_objects = set(taxonomy["objects"])
        self.valid_systems = set(taxonomy["systems"])

    # -- request construction ----------------------------------------------
    def make_request(self, custom_id: str, notes: list[str], char_limit: int):
        """One batch Request carrying a group of tickets' notes."""
        user_msg = build_user_message(notes, char_limit)
        messages = FEW_SHOT + [{"role": "user", "content": user_msg}]
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
    def _clean_steps(self, steps) -> list[dict]:
        out = []
        seen = set()
        if not isinstance(steps, list):
            return out
        for s in steps[:8]:
            if not isinstance(s, dict):
                continue
            a = str(s.get("action", "")).strip().lower()
            o = str(s.get("object", "")).strip().lower()
            y = str(s.get("system", "unknown")).strip().lower()
            if a not in self.valid_actions:
                a = "other"
            if o not in self.valid_objects:
                o = "other"
            if y not in self.valid_systems:
                y = "unknown"
            key = (a, o, y)
            if key in seen:  # deduplicate within a ticket
                continue
            seen.add(key)
            out.append({"action": a, "object": o, "system": y})
        return out

    # -- result parsing -----------------------------------------------------
    def parse_result_text(self, text: str, n_tickets: int) -> dict[int, list[dict]]:
        """Parse one succeeded response into {position_in_group: cleaned steps}.

        Raises on malformed JSON so the caller can skip the group and let those
        tickets be re-submitted on a later run (they simply stay absent from JSONL).
        """
        text = text.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        parsed = json.loads(text)
        result = {}
        for item in parsed:
            pos = item.get("id")
            if isinstance(pos, int) and 0 <= pos < n_tickets:
                result[pos] = self._clean_steps(item.get("steps"))
        return result


# ──────────────────────────────── main ──────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--xls", default="data/Time_entries_Mercor_v3_workflows_by_company__1_.xls")
    ap.add_argument("--taxonomy", default="taxonomy.json")
    ap.add_argument("--out-dir", default="results")
    ap.add_argument("--workflows", nargs="*", help="only these workflows (default: all)")
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
    ap.add_argument("--concurrency", type=int, default=None,
                    help="(deprecated, ignored) the batch API is not rate-limited the way the old thread pool was")
    args = ap.parse_args()

    if args.concurrency is not None:
        print("note: --concurrency is ignored — batch submission has no client-side concurrency.", flush=True)

    load_dotenv()
    random.seed(args.seed)
    taxonomy = json.loads(Path(args.taxonomy).read_text())

    out_dir = Path(args.out_dir)
    raw_dir = out_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / MANIFEST_NAME

    # ── collect-only path ────────────────────────────────────────────────
    if args.collect:
        if not manifest_path.exists():
            raise SystemExit(f"No {manifest_path} to collect. Submit a batch first.")
        extractor = BatchExtractor(taxonomy, args.model)
        collect(extractor, manifest_path, raw_dir, args.poll_interval)
        return

    # ── reconnect to an in-flight batch instead of double-spending ───────
    if manifest_path.exists() and not args.force_new:
        print(f"Found in-flight batch in {manifest_path} — resuming it.", flush=True)
        print("  (use --force-new to discard it and submit a fresh batch)", flush=True)
        extractor = BatchExtractor(taxonomy, args.model)
        collect(extractor, manifest_path, raw_dir, args.poll_interval)
        return

    # ── build requests locally ───────────────────────────────────────────
    print(f"Loading {args.xls} …", flush=True)
    tickets = load_tickets(args.xls)
    by_wf: dict[str, list[dict]] = defaultdict(list)
    for t in tickets:
        by_wf[t["workflow"]].append(t)
    print(f"  {len(tickets):,} tickets across {len(by_wf)} workflows")

    targets = args.workflows or sorted(by_wf)
    missing = [w for w in targets if w not in by_wf]
    if missing:
        raise SystemExit(f"Unknown workflow(s): {missing}")

    if args.dry_run:
        wf = targets[0]
        batch = by_wf[wf][: args.batch_size]
        print("\n===== SYSTEM PROMPT =====\n")
        print(build_system_prompt(taxonomy))
        print("\n===== USER MESSAGE (first request) =====\n")
        print(build_user_message([t["notes"] for t in batch], args.char_limit)[:4000])
        est = sum(
            -(-min(len(by_wf[w]), args.sample or len(by_wf[w])) // args.batch_size) for w in targets
        )
        print(f"\n===== PLAN =====\nworkflows: {len(targets)}   estimated requests in 1 batch: {est:,}")
        return

    extractor = BatchExtractor(taxonomy, args.model)

    if args.force_new:
        manifest_path.unlink(missing_ok=True)

    print("Building requests (sampling, noise filter, resume-skip) …", flush=True)
    groups, freebies = build_groups(by_wf, targets, args, raw_dir)
    print(f"  {len(groups):,} requests to submit, {freebies:,} empty tickets written locally", flush=True)

    if not groups:
        print("Nothing to submit — everything is already extracted or empty.", flush=True)
        return

    if len(groups) > MAX_REQUESTS_PER_BATCH:
        raise SystemExit(
            f"{len(groups):,} requests exceeds the {MAX_REQUESTS_PER_BATCH:,}/batch limit. "
            f"Narrow with --workflows or --sample."
        )

    requests = [
        extractor.make_request(cid, g["notes"], args.char_limit) for cid, g in groups.items()
    ]
    print(f"Submitting batch of {len(requests):,} requests …", flush=True)
    batch = extractor.client.messages.batches.create(requests=requests)
    save_manifest(manifest_path, batch.id, args.model, groups)
    print(f"  batch {batch.id}  status={batch.processing_status}  (manifest → {manifest_path})", flush=True)

    if args.submit_only:
        print("Submitted. Collect it later with:  python extract.py --collect", flush=True)
        return

    collect(extractor, manifest_path, raw_dir, args.poll_interval)


if __name__ == "__main__":
    main()
