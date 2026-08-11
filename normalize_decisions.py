#!/usr/bin/env python3
"""
Normalize per-step decision text into canonical decision nodes (Stage 2).

The Stage-1 extractor records, on each trace step, the QUESTION the engineer
resolved (`decision_q`, English) and this ticket's ANSWER (`decision_a`). This
stage rolls those up, per workflow, into a small set of CANONICAL decisions —
each with a few canonical branches — that aggregate_graph.py turns into decision
diamonds.

The design keeps counts honest and the LLM's job narrow:
  1. Deterministic aggregation counts DISTINCT-ticket support for every question
     and answer. These numbers are plain ticket tallies — the LLM never produces
     one.
  2. A single frequency-anchored LLM merge per workflow only decides GROUPING and
     WORDING: which phrasings are the same decision, and one crisp canonical
     question / branch label per cluster. If the call fails or returns malformed
     JSON, we fall back to "no merge" (each question its own cluster) so a bad
     response can never corrupt the counts.
  3. Frequency gates drop thin decisions/branches, so only the common structures
     — the ones with enough tickets to cluster reliably — reach the tree.

Output: results/decisions.json (per workflow: n + ranked decisions with branch
supports). Cached on the per-workflow payload hash in results/decision_cache.json
so re-runs are free and reproducible.

Security: the only external call is Anthropic messages.create with the env key.
Its inputs are the already-distilled, word-capped English decision phrases and
coded action:object strings from graph_raw — never raw notes. decisions.json and
the cache carry only aggregated counts + short canonical labels.

Quick start
-----------
    python normalize_decisions.py --dry-run     # aggregate + print candidates, no API
    python normalize_decisions.py               # merge + write results/decisions.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from batch_runner import BaseBatchExtractor, _cached_system, load_dotenv, ticket_key

DEFAULT_MODEL = "claude-sonnet-4-6"
DECISIONS_NAME = "decisions.json"
CACHE_NAME = "decision_cache.json"

# Frequency gates (percent of workflow tickets). Only the common decision
# structures — the ones with enough support to cluster reliably — reach the tree.
# 3% / 2% was calibrated on the pilot: it surfaces the real decisions of compound
# workflows (e.g. RMM's parallel CPU/Memory/Service/Patch/AV paths, none dominant)
# while frequency-ranking still foregrounds the common ones.
MIN_DECISION_SUPPORT = 3.0
MIN_BRANCH_SUPPORT = 2.0
MAX_BRANCHES = 4
# How many distinct questions / answers to hand the merge call. Descending by
# support. Fragmented workflows spread one real decision across many ticket-specific
# phrasings, so the cap must be generous enough that merging can accumulate support.
TOP_N_QUESTIONS = 120
TOP_N_ANSWERS = 8


SYSTEM_PROMPT = """You group free-text decision questions from IT support tickets into canonical decisions.

You receive, as JSON, ONE workflow's distinct decision questions. Each has an id, the
question text, its support (percent of tickets that hit it), the coded step it sits on,
and its observed answers (each with an id, text, and support).

The questions are noisy paraphrases of a smaller set of real decisions. Your job:
- MERGE questions that are the same decision into one cluster. The most frequent
  phrasings are the anchors; fold rarer paraphrases into them.
- Merge by the UNDERLYING question, ignoring ticket-specific detail (error codes,
  symptom names, product names). "did password reset resolve the RDP 0x1108 error?"
  and "did the reset restore login?" are the same decision.
- Two questions can be the same decision even if they sit on DIFFERENT coded steps.
  A "did the fix resolve it?" verification is one decision whether the fix was a
  reset, a reboot, or a reinstall — merge those. Use the coded step only to keep
  genuinely different decisions (e.g. "is the account hybrid?" vs "did the fix work?")
  apart.
- Aim for a SMALL set of decisions (typically 3-8 for a workflow); prefer merging
  when in doubt, so the common decisions accumulate real support.
- Leave a genuinely distinct question as its own single-question cluster.
- For each cluster, write ONE crisp canonical question, phrased as a short yes/no-style
  question ending in "?" (e.g. "Is the device online and rebooted?").
- Within each cluster, MERGE the answers the same way into canonical branches, and write
  a short canonical label for each branch (e.g. "Yes, back online", "No, still offline").
  Each answer id must appear in exactly one branch of its cluster.

NEVER include personal names, email addresses, phone numbers, or credentials.

Return ONLY JSON, no prose, no markdown fences:
{"decisions": [
  {"canonical_q": "<question>", "question_ids": [<int>, ...],
   "branches": [{"canonical_a": "<label>", "answer_ids": ["<qid>.<aid>", ...]}]}
]}
"""


# ─────────────────────────── deterministic aggregation ──────────────────────

def canon(s) -> str:
    """Cheap canonical key for a free-text phrase: lowercase, drop punctuation,
    collapse whitespace. Only used to tally repeats; display uses the raw text."""
    if not isinstance(s, str):
        return ""
    s = s.lower()
    s = re.sub(r"[^\w\s]", " ", s)
    return " ".join(s.split())


def _rep(counter: Counter) -> str:
    """The most frequent raw phrasing, ties broken lexicographically — stable."""
    return min(counter.items(), key=lambda kv: (-kv[1], kv[0]))[0] if counter else ""


def collect_workflow(records: list[dict]) -> dict:
    """Pure aggregation for one workflow's ticket records (already deduped upstream
    is fine; we dedupe defensively on (company, ticket_id)).

    Returns a candidate structure keyed by canonical question:
      q_canon -> {
        tickets: set[ticket_key],           # for distinct-ticket support
        raw: Counter(raw decision_q),        # to pick a representative phrasing
        context: Counter(coded action:object),
        answers: { a_canon -> {tickets:set, raw:Counter, next: Counter} },
      }
    plus n (distinct tickets in the workflow).
    """
    seen_tickets: set = set()
    q: dict = defaultdict(lambda: {
        "tickets": set(), "raw": Counter(), "context": Counter(),
        "answers": defaultdict(lambda: {"tickets": set(), "raw": Counter(), "next": Counter()}),
    })

    seen_records: set = set()
    for rec in records:
        tk = ticket_key(rec)
        if tk in seen_records:
            continue
        seen_records.add(tk)
        seen_tickets.add(tk)

        trace = rec.get("trace") or []
        for i, step in enumerate(trace):
            dq = step.get("decision_q")
            qk = canon(dq)
            if not qk:
                continue
            entry = q[qk]
            entry["tickets"].add(tk)
            entry["raw"][dq.strip()] += 1
            entry["context"][f"{step.get('action', 'other')}:{step.get('object', 'other')}"] += 1

            da = step.get("decision_a")
            ak = canon(da) or "(unspecified)"
            a_raw = da.strip() if isinstance(da, str) and da.strip() else "(unspecified)"
            if i + 1 < len(trace):
                nxt = trace[i + 1]
                next_hint = f"{nxt.get('action', 'other')}:{nxt.get('object', 'other')}"
            else:
                next_hint = f"out:{rec.get('outcome', 'other')}"
            ans = entry["answers"][ak]
            ans["tickets"].add(tk)
            ans["raw"][a_raw] += 1
            ans["next"][next_hint] += 1

    return {"n": len(seen_tickets), "questions": q}


def build_payload(workflow: str, agg: dict,
                  top_n_q: int = TOP_N_QUESTIONS, top_n_a: int = TOP_N_ANSWERS) -> dict:
    """Shape the top questions (by support, descending) into the merge-call payload.
    Assigns stable integer ids to questions and "<qid>.<aid>" ids to answers."""
    n = max(agg["n"], 1)
    q_items = sorted(
        agg["questions"].items(),
        key=lambda kv: (-len(kv[1]["tickets"]), kv[0]),
    )[:top_n_q]

    questions = []
    for qid, (qk, entry) in enumerate(q_items):
        a_items = sorted(
            entry["answers"].items(),
            key=lambda kv: (-len(kv[1]["tickets"]), kv[0]),
        )[:top_n_a]
        answers = [
            {"id": f"{qid}.{aid}", "text": _rep(ans["raw"]),
             "pct": round(len(ans["tickets"]) / n * 100, 1)}
            for aid, (ak, ans) in enumerate(a_items)
        ]
        questions.append({
            "id": qid,
            "text": _rep(entry["raw"]),
            "pct": round(len(entry["tickets"]) / n * 100, 1),
            "context": entry["context"].most_common(1)[0][0] if entry["context"] else "other:other",
            "answers": answers,
        })
    return {"workflow": workflow, "questions": questions}


def _no_merge(payload: dict) -> dict:
    """Fallback grouping: every question its own cluster, every answer its own
    branch. Used when the LLM call fails or returns unusable JSON."""
    decisions = []
    for q in payload["questions"]:
        decisions.append({
            "canonical_q": q["text"],
            "question_ids": [q["id"]],
            "branches": [{"canonical_a": a["text"], "answer_ids": [a["id"]]}
                         for a in q["answers"]],
        })
    return {"decisions": decisions}


def apply_merge(workflow: str, agg: dict, payload: dict, merge: dict,
                min_decision: float = MIN_DECISION_SUPPORT,
                min_branch: float = MIN_BRANCH_SUPPORT,
                max_branches: int = MAX_BRANCHES) -> dict:
    """Combine the deterministic aggregation with the LLM's cluster assignments
    into gated, ranked canonical decisions. Pure: counts come only from `agg`.

    Robust to a partial/garbled merge — any question id the merge omits is
    recovered as its own single-question decision, so no support is silently lost.
    """
    n = max(agg["n"], 1)
    # index: payload question id -> (q_canon, entry); "<qid>.<aid>" -> (a_canon, ans)
    q_items = sorted(agg["questions"].items(), key=lambda kv: (-len(kv[1]["tickets"]), kv[0]))
    q_by_id, a_by_id = {}, {}
    for qid, (qk, entry) in enumerate(q_items):
        if qid >= len(payload["questions"]):
            break
        q_by_id[qid] = (qk, entry)
        a_items = sorted(entry["answers"].items(), key=lambda kv: (-len(kv[1]["tickets"]), kv[0]))
        for aid, (ak, ans) in enumerate(a_items[:TOP_N_ANSWERS]):
            a_by_id[f"{qid}.{aid}"] = (ak, ans)

    clusters = merge.get("decisions") if isinstance(merge, dict) else None
    if not isinstance(clusters, list) or not clusters:
        clusters = _no_merge(payload)["decisions"]

    used_qids: set = set()
    raw_decisions = []
    for c in clusters:
        qids = [i for i in (c.get("question_ids") or []) if i in q_by_id]
        if not qids:
            continue
        used_qids.update(qids)
        raw_decisions.append((c.get("canonical_q"), qids, c.get("branches") or []))

    # recover any question the merge dropped as its own decision (no support lost)
    for qid in q_by_id:
        if qid not in used_qids:
            raw_decisions.append((q_by_id[qid][1] and _rep(q_by_id[qid][1]["raw"]), [qid], None))

    decisions = []
    for canonical_q, qids, branches in raw_decisions:
        tickets: set = set()
        context = Counter()
        for qid in qids:
            entry = q_by_id[qid][1]
            tickets |= entry["tickets"]
            context.update(entry["context"])
        support = round(len(tickets) / n * 100, 1)
        if support < min_decision:
            continue

        # branch clusters: use the LLM's answer grouping if present, else one
        # branch per answer id belonging to these questions.
        if branches:
            groups = [(b.get("canonical_a"), [aid for aid in (b.get("answer_ids") or []) if aid in a_by_id])
                      for b in branches]
        else:
            groups = []
            for qid in qids:
                a_items = sorted(q_by_id[qid][1]["answers"].items(),
                                 key=lambda kv: (-len(kv[1]["tickets"]), kv[0]))
                for aid, (ak, ans) in enumerate(a_items[:TOP_N_ANSWERS]):
                    groups.append((_rep(ans["raw"]), [f"{qid}.{aid}"]))

        br_out = []
        for label, aids in groups:
            a_tickets: set = set()
            nxt = Counter()
            raw = Counter()
            for aid in aids:
                if aid not in a_by_id:
                    continue
                _, ans = a_by_id[aid]
                a_tickets |= ans["tickets"]
                nxt.update(ans["next"])
                raw.update(ans["raw"])
            if not a_tickets:
                continue
            b_support = round(len(a_tickets) / n * 100, 1)
            if b_support < min_branch:
                continue
            br_out.append({
                "canonical_a": (label or _rep(raw) or "(unspecified)"),
                "count": len(a_tickets),
                "support": b_support,
                "next_hint": nxt.most_common(1)[0][0] if nxt else None,
            })
        br_out.sort(key=lambda b: (-b["support"], b["canonical_a"]))
        br_out = br_out[:max_branches]

        members = []
        for qid in qids:
            members.append(_rep(q_by_id[qid][1]["raw"]))
        decisions.append({
            "canonical_q": (canonical_q or (members[0] if members else "decision")),
            "count": len(tickets),
            "support": support,
            "coded_context": context.most_common(1)[0][0] if context else "other:other",
            "branches": br_out,
            "members": members,
        })

    decisions.sort(key=lambda d: (-d["support"], d["canonical_q"]))
    for i, d in enumerate(decisions):
        d["id"] = f"d{i}"
    return {"n": agg["n"], "decisions": decisions}


# ──────────────────────────────── LLM merge ─────────────────────────────────

def _payload_sig(payload: dict) -> str:
    return hashlib.sha1(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def merge_llm(extractor, payload: dict, max_tokens: int) -> dict:
    """One synchronous merge call. Crash-proof: any failure or unusable JSON
    returns the no-merge grouping, so counts never depend on the model."""
    try:
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
        if isinstance(parsed, dict) and isinstance(parsed.get("decisions"), list):
            return parsed
    except Exception as e:
        print(f"  ! merge fell back to no-merge ({type(e).__name__}: {e})", flush=True)
    return _no_merge(payload)


# ──────────────────────────── file plumbing ─────────────────────────────────

def load_raw(raw_dir: Path) -> dict[str, list[dict]]:
    """Read results/graph_raw/*.jsonl, grouped by the workflow recorded in each record."""
    by_wf: dict[str, list[dict]] = defaultdict(list)
    for p in sorted(raw_dir.glob("*.jsonl")):
        with p.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                wf = rec.get("workflow")
                if wf:
                    by_wf[wf].append(rec)
    return by_wf


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw-dir", default="results/graph_raw")
    ap.add_argument("--out", default=None, help="default: results/decisions.json")
    ap.add_argument("--cache", default=None, help="default: results/decision_cache.json")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--max-tokens", type=int, default=10000)
    ap.add_argument("--min-decision-support", type=float, default=MIN_DECISION_SUPPORT)
    ap.add_argument("--min-branch-support", type=float, default=MIN_BRANCH_SUPPORT)
    ap.add_argument("--max-branches", type=int, default=MAX_BRANCHES)
    ap.add_argument("--workflows", nargs="*", help="only these workflows (default: all present)")
    ap.add_argument("--dry-run", action="store_true", help="aggregate + print candidate counts, no API")
    args = ap.parse_args()

    load_dotenv()
    raw_dir = Path(args.raw_dir)
    if not raw_dir.exists():
        raise SystemExit(f"{raw_dir} not found — run extract_graph.py first.")
    out_path = Path(args.out) if args.out else raw_dir.parent / DECISIONS_NAME
    cache_path = Path(args.cache) if args.cache else raw_dir.parent / CACHE_NAME
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}

    by_wf = load_raw(raw_dir)
    targets = args.workflows or sorted(by_wf)
    missing = [w for w in targets if w not in by_wf]
    if missing:
        raise SystemExit(f"No raw data for: {missing}")

    extractor = None
    if not args.dry_run:
        extractor = BaseBatchExtractor(args.model)  # builds client, checks ANTHROPIC_API_KEY

    out: dict = {}
    sent = 0
    for wf in targets:
        agg = collect_workflow(by_wf[wf])
        payload = build_payload(wf, agg)
        n_q = len(payload["questions"])

        if args.dry_run:
            print(f"  {wf}: n={agg['n']}, {n_q} distinct decision questions "
                  f"(top pct={payload['questions'][0]['pct'] if n_q else 0})")
            continue

        if n_q == 0:
            out[wf] = {"n": agg["n"], "decisions": []}
            continue

        sig = _payload_sig(payload)
        cached = cache.get(sig)
        if cached is not None:
            merge = cached
        else:
            merge = merge_llm(extractor, payload, args.max_tokens)
            cache[sig] = merge
            sent += 1

        out[wf] = apply_merge(wf, agg, payload, merge,
                              args.min_decision_support, args.min_branch_support, args.max_branches)
        print(f"  {wf}: {len(out[wf]['decisions'])} decisions kept "
              f"(>= {args.min_decision_support}% support)", flush=True)

    if args.dry_run:
        print(f"\n{len(targets)} workflows aggregated (no API spend).")
        return

    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    print(f"\nWrote {len(out)} workflows ({sent} merge calls, {len(cache)} cached) → {out_path}")


if __name__ == "__main__":
    main()
