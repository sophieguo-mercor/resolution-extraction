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
import os
import random
import re
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

from prompts import FEW_SHOT, build_system_prompt, build_user_message

NS = {"ss": "urn:schemas-microsoft-com:office:spreadsheet"}
DEFAULT_MODEL = "claude-sonnet-4-6"

# A single Anthropic batch accepts up to 100k requests / 256 MB. The full corpus at
# --batch-size 12 is ~10.4k requests, comfortably inside one batch.
MAX_REQUESTS_PER_BATCH = 100_000

# custom_ids must match ^[a-zA-Z0-9_-]{1,64}$ — we use gNNNNNN and map back via manifest.
MANIFEST_NAME = "batch_manifest.json"


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader — no dependency. Existing env vars win."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip("'\"")
        os.environ.setdefault(key, val)

# ─────────────────────────── spreadsheet loading ────────────────────────────

def _cell(c):
    d = c.find("ss:Data", NS)
    return d.text if d is not None else None


def load_tickets(xls_path: str) -> list[dict]:
    """Parse the SpreadsheetML export into a list of ticket dicts."""
    tree = ET.parse(xls_path)
    root = tree.getroot()
    sheets = {
        ws.get("{urn:schemas-microsoft-com:office:spreadsheet}Name"): ws
        for ws in root.findall("ss:Worksheet", NS)
    }
    if "Ticket Detail" not in sheets:
        raise SystemExit(f"'Ticket Detail' sheet not found. Sheets: {list(sheets)}")

    table = sheets["Ticket Detail"].find("ss:Table", NS)
    rows = [
        [_cell(c) for c in row.findall("ss:Cell", NS)]
        for row in table.findall("ss:Row", NS)
    ]
    header = rows[0]
    idx = {name: i for i, name in enumerate(header)}

    def get(row, name):
        i = idx.get(name)
        return row[i] if i is not None and i < len(row) else None

    tickets = []
    for row in rows[1:]:
        wf = get(row, "Workflow")
        if not wf:
            continue
        try:
            hours = float(get(row, "Total Hours") or 0)
        except ValueError:
            hours = 0.0
        try:
            touches = int(float(get(row, "Touches") or 1))
        except ValueError:
            touches = 1
        tickets.append(
            {
                "ticket_id": get(row, "Ticket ID"),
                "company": get(row, "Company (Label)"),
                "workflow": wf,
                "hours": hours,
                "touches": touches,
                "notes": get(row, "Notes") or "",
            }
        )
    return tickets


# ───────────────────────────── noise pre-filter ─────────────────────────────

_BOILERPLATE = re.compile(
    r"bedankt voor je melding|uw ticket is geclassificeerd|ticket complete|ticket update"
    r"|see internal notes|change advisory board",
    re.I,
)


MIN_CONTENT_CHARS = 5  # notes with fewer real chars than this are treated as empty


def is_probably_empty(notes: str) -> bool:
    """Cheap local check: skip tickets that clearly contain no agent action.

    Only drops notes with essentially nothing in them: fewer than
    MIN_CONTENT_CHARS of text, or nothing left once known boilerplate is removed.
    The threshold is deliberately low so short-but-real notes ("Training
    afgerond", "Gereed") still reach the model; longer boilerplate that slips
    through is handled by the prompt, which returns an empty step list for it.
    """
    text = re.sub(r"[-\s]+", " ", (notes or "")).strip(" -\n\t")
    if len(text) < MIN_CONTENT_CHARS:
        return True
    # Remove boilerplate sentences, see whether anything substantive remains
    residue = _BOILERPLATE.sub(" ", text)
    residue = re.sub(r"\s+", " ", residue).strip(" -\n\t")
    return len(residue) < MIN_CONTENT_CHARS


# ────────────────────────────── API plumbing ────────────────────────────────

def _cached_system(system_text: str) -> list[dict]:
    """Wrap the system prompt as a cache_control'd block so the taxonomy prefix —
    identical across every request — is billed once and read from cache (~0.1x)
    for the rest of the batch."""
    return [{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}]


class BatchExtractor:
    """Builds batch Requests and parses batch results against the taxonomy.

    No network in the constructor beyond client init; submission/polling live in the
    runner so the same object serves --dry-run, submit, and collect paths.
    """

    def __init__(self, taxonomy: dict, model: str, max_tokens: int = 4000):
        self.taxonomy = taxonomy
        self.model = model
        self.max_tokens = max_tokens
        self.system_prompt = build_system_prompt(taxonomy)
        self.valid_actions = set(taxonomy["actions"])
        self.valid_objects = set(taxonomy["objects"])
        self.valid_systems = set(taxonomy["systems"])

        try:
            import anthropic  # noqa: F401
        except ImportError:
            raise SystemExit("pip install anthropic")
        from anthropic import Anthropic
        from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
        from anthropic.types.messages.batch_create_params import Request

        self._MessageCreateParamsNonStreaming = MessageCreateParamsNonStreaming
        self._Request = Request

        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise SystemExit("Set ANTHROPIC_API_KEY in your environment.")
        self.client = Anthropic()

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


# ──────────────────────────────── runner ────────────────────────────────────

def safe_name(wf: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", wf).strip("_")[:80]


def raw_path(raw_dir: Path, wf: str) -> Path:
    return raw_dir / f"{safe_name(wf)}.jsonl"


def ticket_key(rec: dict) -> tuple:
    """Unique identity of a ticket. A ticket_id is only unique *within* a company,
    so the same ticket_id can recur across companies — key on the pair."""
    return (rec.get("company"), rec.get("ticket_id"))


def load_done_ids(path: Path) -> set[tuple]:
    done = set()
    if path.exists():
        with path.open() as f:
            for line in f:
                try:
                    done.add(ticket_key(json.loads(line)))
                except Exception:
                    continue
    return done


def _stub(t: dict) -> dict:
    """The fields needed to write an output record — deliberately drops `notes`
    so the on-disk manifest carries no raw ticket text."""
    return {
        "ticket_id": t["ticket_id"],
        "company": t["company"],
        "workflow": t["workflow"],
        "hours": t["hours"],
        "touches": t["touches"],
    }


class JsonlWriter:
    """Lazily-opened, append-mode, idempotent per-workflow JSONL sink."""

    def __init__(self, raw_dir: Path):
        self.raw_dir = raw_dir
        self._fh: dict[str, object] = {}
        self._done: dict[str, set] = {}

    def _wf(self, wf: str):
        if wf not in self._fh:
            path = raw_path(self.raw_dir, wf)
            self._done[wf] = load_done_ids(path)
            self._fh[wf] = path.open("a", encoding="utf-8")
        return self._fh[wf], self._done[wf]

    def emit(self, stub: dict, steps: list) -> bool:
        wf = stub["workflow"]
        fh, done = self._wf(wf)
        key = ticket_key(stub)
        if key in done:  # idempotent across re-runs
            return False
        rec = {**stub, "steps": steps}
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fh.flush()
        done.add(key)
        return True

    def close(self):
        for fh in self._fh.values():
            fh.close()


def build_groups(by_wf, targets, args, raw_dir):
    """Local pre-pass: apply sampling + noise filter + resume-skip, emit freebies
    now, and return (groups, freebies_written).

    groups: {custom_id: {"workflow": wf, "tickets": [stub, ...], "notes": [str, ...]}}
    """
    writer = JsonlWriter(raw_dir)
    groups: dict[str, dict] = {}
    counter = 0
    freebies_written = 0

    for wf in targets:
        pool = by_wf[wf]
        if args.sample and len(pool) > args.sample:
            pool = random.sample(pool, args.sample)

        done = load_done_ids(raw_path(raw_dir, wf))
        todo = []
        for t in pool:
            if ticket_key(t) in done:
                continue
            if is_probably_empty(t["notes"]):
                if writer.emit(_stub(t), []):
                    freebies_written += 1
            else:
                todo.append(t)

        for i in range(0, len(todo), args.batch_size):
            chunk = todo[i : i + args.batch_size]
            cid = f"g{counter:06d}"
            counter += 1
            groups[cid] = {
                "workflow": wf,
                "tickets": [_stub(t) for t in chunk],
                "notes": [t["notes"] for t in chunk],
            }

    writer.close()
    return groups, freebies_written


def save_manifest(manifest_path: Path, batch_id: str, model: str, groups: dict) -> None:
    # Drop `notes` before persisting — the manifest only needs stubs to route results.
    slim = {
        cid: {"workflow": g["workflow"], "tickets": g["tickets"]}
        for cid, g in groups.items()
    }
    manifest_path.write_text(
        json.dumps({"batch_id": batch_id, "model": model, "groups": slim}, ensure_ascii=False),
        encoding="utf-8",
    )


def collect(extractor: BatchExtractor, manifest_path: Path, raw_dir: Path, poll_interval: int):
    """Poll an in-flight batch to completion, then route results to per-workflow JSONL."""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    batch_id = manifest["batch_id"]
    groups = manifest["groups"]

    print(f"Reconnecting to batch {batch_id} ({len(groups)} requests) …", flush=True)
    while True:
        b = extractor.client.messages.batches.retrieve(batch_id)
        c = b.request_counts
        print(
            f"  status={b.processing_status}  "
            f"processing={c.processing} succeeded={c.succeeded} "
            f"errored={c.errored} canceled={c.canceled} expired={c.expired}",
            flush=True,
        )
        if b.processing_status == "ended":
            break
        time.sleep(poll_interval)

    writer = JsonlWriter(raw_dir)
    written = skipped = errored = bad_json = 0
    usage = {"input": 0, "output": 0, "cache_read": 0, "cache_create": 0}

    for r in extractor.client.messages.batches.results(batch_id):
        grp = groups.get(r.custom_id)
        if grp is None:
            continue
        stubs = grp["tickets"]

        if r.result.type != "succeeded":
            err = r.result.type
            try:
                err = r.result.error.type
            except Exception:
                pass
            errored += len(stubs)
            print(f"    ! {r.custom_id} {err} ({len(stubs)} tickets) — will re-submit next run", flush=True)
            continue

        msg = r.result.message
        u = msg.usage
        usage["input"] += getattr(u, "input_tokens", 0) or 0
        usage["output"] += getattr(u, "output_tokens", 0) or 0
        usage["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
        usage["cache_create"] += getattr(u, "cache_creation_input_tokens", 0) or 0

        text = "".join(blk.text for blk in msg.content if blk.type == "text")
        try:
            steps_by_pos = extractor.parse_result_text(text, len(stubs))
        except Exception as e:
            bad_json += len(stubs)
            print(f"    ! {r.custom_id} bad JSON ({len(stubs)} tickets): {e} — will re-submit next run", flush=True)
            continue

        for pos, stub in enumerate(stubs):
            if writer.emit(stub, steps_by_pos.get(pos, [])):
                written += 1
            else:
                skipped += 1

    writer.close()
    manifest_path.unlink(missing_ok=True)

    print(
        f"\nCollected batch {batch_id}: {written} written, {skipped} already present, "
        f"{errored} errored, {bad_json} unparseable.",
        flush=True,
    )
    print(
        f"Tokens — input {usage['input']:,} (cache read {usage['cache_read']:,}, "
        f"create {usage['cache_create']:,}) / output {usage['output']:,}",
        flush=True,
    )
    if errored or bad_json:
        print("Some tickets weren't written; re-run to submit a fresh batch for them.", flush=True)
    print(f"Raw results: {raw_dir}/   →  next: python aggregate.py", flush=True)


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
