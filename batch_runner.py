"""Shared batch-extraction machinery for the resolution-extraction pipeline.

Lifted verbatim out of extract.py so that more than one extractor — the
coded-step extract.py and the forthcoming hybrid extract_graph.py — share a
single implementation of:
  - SpreadsheetML loading (load_tickets)
  - the cheap local noise pre-filter (is_probably_empty)
  - the idempotent per-workflow JSONL sink (JsonlWriter)
  - the Anthropic Message Batches submit / poll / collect loop and its manifest

Data-minimization invariants preserved from the original (do not regress):
  - the on-disk manifest carries only routing stubs, never raw ticket notes
  - output JSONL records drop the `notes` field
  - the Anthropic API key is read from the environment, never embedded or logged

Extractor-specific pieces (system prompt, request construction, result
validation) live in the BatchExtractor subclasses, not here.
"""
from __future__ import annotations

import json
import os
import random
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path

NS = {"ss": "urn:schemas-microsoft-com:office:spreadsheet"}

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


class BaseBatchExtractor:
    """Shared Anthropic batch client setup.

    Subclasses build the system prompt, define make_request(), and define
    parse_result_text(). No network beyond client init happens here;
    submission/polling live in the runner so one object serves the --dry-run,
    submit, and collect paths.
    """

    def __init__(self, model: str, max_tokens: int = 4000):
        self.model = model
        self.max_tokens = max_tokens

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

    def emit(self, stub: dict, result) -> bool:
        """Write one record. `result` is either a legacy steps list — stored as
        {**stub, "steps": [...]} — or a full payload dict, merged as {**stub,
        **result}. Both extractors share this sink; the dict form carries the
        hybrid graph schema. Idempotent across re-runs (keyed on company+id)."""
        wf = stub["workflow"]
        fh, done = self._wf(wf)
        key = ticket_key(stub)
        if key in done:  # idempotent across re-runs
            return False
        rec = {**stub, **result} if isinstance(result, dict) else {**stub, "steps": result}
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fh.flush()
        done.add(key)
        return True

    def close(self):
        for fh in self._fh.values():
            fh.close()


def build_groups(by_wf, targets, args, raw_dir, empty_payload=None):
    """Local pre-pass: apply sampling + noise filter + resume-skip, emit freebies
    now, and return (groups, freebies_written).

    groups: {custom_id: {"workflow": wf, "tickets": [stub, ...], "notes": [str, ...]}}

    `empty_payload` is what to write for a pre-filtered empty ticket: the default
    (None -> []) reproduces the coded extractor's {"steps": []}; the graph
    extractor passes a dict so empties get the hybrid record shape.
    """
    empty = [] if empty_payload is None else empty_payload
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
                if writer.emit(_stub(t), empty):
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


def collect(extractor, manifest_path: Path, raw_dir: Path, poll_interval: int, empty_result=None):
    """Poll an in-flight batch to completion, then route results to per-workflow JSONL.

    `empty_result` is written for a position missing from a succeeded response
    (default None -> [], the coded extractor's empty steps list; the graph
    extractor passes its empty record dict)."""
    empty = [] if empty_result is None else empty_result
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
            results_by_pos = extractor.parse_result_text(text, len(stubs))
        except Exception as e:
            bad_json += len(stubs)
            print(f"    ! {r.custom_id} bad JSON ({len(stubs)} tickets): {e} — will re-submit next run", flush=True)
            continue

        for pos, stub in enumerate(stubs):
            if writer.emit(stub, results_by_pos.get(pos, empty)):
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
