#!/usr/bin/env python3
"""
Turn raw extracted steps into per-workflow process-variance metrics.

Reads  results/raw/*.jsonl   (written by extract.py)
Writes results/scorecard.json, results/scorecard.csv, results/patterns.json

Metrics per workflow
--------------------
Pattern concentration
  n_patterns          distinct resolution patterns (a pattern = the set of
                      (action,object) pairs in one ticket)
  top1/top3/top5_pct  % of tickets explained by the N most common patterns
  pattern_entropy     normalised Shannon entropy, 0 = every ticket identical,
                      1 = every ticket unique

Action structure
  n_distinct_pairs    how many different (action,object) pairs appear at all
  herfindahl          concentration of the pair distribution (1 = one pair only)
  mean_jaccard        average pairwise set-similarity between two random tickets
  jaccard_bimodality  share of pairs that are near-identical (>=0.8) or
                      near-disjoint (<=0.2); high = two sub-populations

Effort shape
  mean_steps, median_steps, steps_cv
  phase_mix           % of steps that are diagnose / change / coordinate / admin
  pct_no_action       % of tickets where no concrete action was extracted

Composite
  process_score       0-100, higher = more replicable process
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
import statistics
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path

JACCARD_SAMPLE_PAIRS = 4000  # cap pairwise comparisons for large workflows

# Contentless coverage markers: they record that a ticket WAS resolved without
# saying how. Excluded from every pattern/pair/variance metric so they can't
# inflate concentration or deflate entropy; counted separately as coverage.
STOPWORD_ACTIONS = frozenset({"resolve_unspecified"})


# ────────────────────────────── helpers ─────────────────────────────────────

def pair_key(step: dict) -> tuple[str, str]:
    return (step["action"], step["object"])


def real_steps(steps: list[dict]) -> list[dict]:
    """Steps that carry process information — stopword markers dropped."""
    return [s for s in steps if s["action"] not in STOPWORD_ACTIONS]


def pattern_key(steps: list[dict]) -> tuple:
    """Ordered pattern for a ticket: the (action, object) pairs in the sequence
    they were performed, so "investigate -> fix" and "fix -> investigate" are
    distinct patterns. Duplicate pairs are dropped on first occurrence; stopword
    markers are excluded."""
    seen: set = set()
    out: list = []
    for s in real_steps(steps):
        k = pair_key(s)
        if k not in seen:
            seen.add(k)
            out.append(k)
    return tuple(out)


def norm_entropy(counts: list[int], n_items: int) -> float:
    """Shannon entropy normalised by ln(n_items).

    0.0 -> all tickets share one pattern.  1.0 -> every ticket is unique.
    """
    if n_items <= 1:
        return 0.0
    total = sum(counts)
    h = -sum((c / total) * math.log(c / total) for c in counts if c)
    return round(h / math.log(n_items), 3)


def herfindahl(counts: list[int]) -> float:
    total = sum(counts)
    if not total:
        return 0.0
    return round(sum((c / total) ** 2 for c in counts), 3)


def jaccard(a: frozenset, b: frozenset) -> float:
    if not a and not b:
        return 1.0
    u = len(a | b)
    return len(a & b) / u if u else 0.0


def minmax(values: list[float], invert: bool = False) -> list[float]:
    lo, hi = min(values), max(values)
    if hi == lo:
        return [0.5] * len(values)
    out = [(v - lo) / (hi - lo) for v in values]
    return [1 - v for v in out] if invert else out


# ─────────────────────────── per-workflow metrics ───────────────────────────

def analyse(records: list[dict], phase_of: dict[str, str], rng: random.Random) -> dict:
    n = len(records)
    step_sets = [frozenset(pair_key(s) for s in real_steps(r["steps"])) for r in records]
    patterns = [pattern_key(r["steps"]) for r in records]
    # coverage split for tickets with no real steps: genuinely empty vs a bare
    # "resolved but undescribed" marker
    has_marker = [any(s["action"] in STOPWORD_ACTIONS for s in r["steps"]) for r in records]

    pat_counts = Counter(patterns)
    ranked_pats = pat_counts.most_common()
    pat_vals = [c for _, c in ranked_pats]

    def top_pct(k: int) -> float:
        return round(sum(pat_vals[:k]) / n * 100, 1)

    pair_counts = Counter(p for ss in step_sets for p in ss)

    def sample_pairs(indices: list[int]) -> list[tuple[int, int]]:
        """All pairs, or a capped random sample, of the given ticket indices."""
        m = len(indices)
        if m < 2:
            return []
        if m * (m - 1) // 2 <= JACCARD_SAMPLE_PAIRS:
            return list(combinations(indices, 2))
        return [tuple(rng.sample(indices, 2)) for _ in range(JACCARD_SAMPLE_PAIRS)]

    # Mean pairwise Jaccard over ALL tickets (empties included) — "how alike are
    # two random tickets". Drawn first so its RNG sequence is unchanged.
    jacs = [jaccard(step_sets[i], step_sets[j]) for i, j in sample_pairs(list(range(n)))]
    mean_jac = round(statistics.mean(jacs), 3) if jacs else 0.0

    # Bimodality: only meaningful when pairs pile up at BOTH ends. Computed over
    # NON-EMPTY tickets only — empty step-sets pair at jaccard 1.0 with each other
    # and ~0 with everything else, which manufactures a false "two populations"
    # split that is really just extracted-vs-not. Taking the smaller of the two
    # shares (x2 to scale to 0-1) means an all-similar or all-dissimilar workflow
    # scores ~0, and a genuine 50/50 split scores ~1.
    ne_jacs = [
        jaccard(step_sets[i], step_sets[j])
        for i, j in sample_pairs([i for i in range(n) if step_sets[i]])
    ]
    if ne_jacs:
        share_hi = sum(1 for v in ne_jacs if v >= 0.8) / len(ne_jacs)
        share_lo = sum(1 for v in ne_jacs if v <= 0.2) / len(ne_jacs)
        bimodal = round(2 * min(share_hi, share_lo), 3)
    else:
        bimodal = 0.0

    step_counts = [len(ss) for ss in step_sets]
    mean_steps = statistics.mean(step_counts) if step_counts else 0
    sd_steps = statistics.stdev(step_counts) if len(step_counts) > 1 else 0.0

    phase_counter = Counter()
    for r in records:
        for s in real_steps(r["steps"]):
            phase_counter[phase_of.get(s["action"], "admin")] += 1
    total_steps = sum(phase_counter.values()) or 1
    phase_mix = {
        p: round(phase_counter.get(p, 0) / total_steps * 100, 1)
        for p in ("diagnose", "change", "coordinate", "admin")
    }

    return {
        "n_tickets": n,
        # no real steps found (genuinely empty OR resolved-but-undescribed) — kept
        # as-is so process_score's coverage term is not gamed by vague completions
        "pct_no_action": round(sum(1 for ss in step_sets if not ss) / n * 100, 1),
        # of those, the share that at least stated a resolution (a coverage signal,
        # not a measured process)
        "pct_resolved_unspecified": round(
            sum(1 for ss, m in zip(step_sets, has_marker) if not ss and m) / n * 100, 1
        ),
        # pattern concentration
        "n_patterns": len(pat_counts),
        "top1_pct": top_pct(1),
        "top3_pct": top_pct(3),
        "top5_pct": top_pct(5),
        "pattern_entropy": norm_entropy(pat_vals, n),
        # action structure
        "n_distinct_pairs": len(pair_counts),
        "herfindahl": herfindahl(list(pair_counts.values())),
        "mean_jaccard": mean_jac,
        "jaccard_bimodality": bimodal,
        # effort shape
        "mean_steps": round(mean_steps, 2),
        "median_steps": statistics.median(step_counts) if step_counts else 0,
        "steps_cv": round(sd_steps / mean_steps, 3) if mean_steps else 0.0,
        "phase_mix": phase_mix,
        # detail for drill-down
        "top_patterns": [
            {
                "pattern": [f"{a}:{o}" for a, o in pat],
                "count": c,
                "pct": round(c / n * 100, 1),
            }
            for pat, c in ranked_pats[:10]
        ],
        "top_pairs": [
            {"pair": f"{a}:{o}", "count": c, "pct": round(c / n * 100, 1)}
            for (a, o), c in pair_counts.most_common(15)
        ],
    }


# ────────────────────────────────── main ────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw-dir", default="results/raw")
    ap.add_argument("--taxonomy", default="taxonomy.json")
    ap.add_argument("--out-dir", default="results")
    ap.add_argument("--min-tickets", type=int, default=20)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    taxonomy = json.loads(Path(args.taxonomy).read_text())
    phase_of = {a: m["phase"] for a, m in taxonomy["actions"].items()}

    raw_dir = Path(args.raw_dir)
    files = sorted(raw_dir.glob("*.jsonl"))
    if not files:
        raise SystemExit(f"No .jsonl files in {raw_dir} — run extract.py first.")

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

    # de-duplicate by (company, ticket_id) — a ticket_id is only unique within a
    # company, and a resumed run can append the same record twice
    for wf, recs in by_wf.items():
        seen, uniq = set(), []
        for r in recs:
            key = (r.get("company"), r.get("ticket_id"))
            if key in seen:
                continue
            seen.add(key)
            uniq.append(r)
        by_wf[wf] = uniq

    results = {}
    for wf, recs in by_wf.items():
        if len(recs) < args.min_tickets:
            print(f"  skip (only {len(recs)} tickets): {wf}")
            continue
        results[wf] = analyse(recs, phase_of, rng)

    if not results:
        raise SystemExit("Nothing met --min-tickets.")

    # Composite score: concentrated patterns, similar tickets, consistent step
    # counts, change-heavy rather than diagnose-heavy — and weighted down when
    # the model found no action at all, since an unmeasured workflow is not a
    # demonstrably replicable one.
    names = list(results)
    top3 = minmax([results[w]["top3_pct"] for w in names])
    ent = minmax([results[w]["pattern_entropy"] for w in names], invert=True)
    jac = minmax([results[w]["mean_jaccard"] for w in names])
    cov = minmax([100 - results[w]["pct_no_action"] for w in names])
    scv = minmax([results[w]["steps_cv"] for w in names], invert=True)
    chg = minmax([results[w]["phase_mix"]["change"] for w in names])

    for i, w in enumerate(names):
        results[w]["process_score"] = round(
            100
            * (
                0.25 * top3[i]
                + 0.20 * ent[i]
                + 0.20 * jac[i]
                + 0.15 * cov[i]
                + 0.10 * scv[i]
                + 0.10 * chg[i]
            ),
            1,
        )

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    (out_dir / "scorecard.json").write_text(
        json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    patterns = {w: {"top_patterns": r.pop("top_patterns"), "top_pairs": r.pop("top_pairs")}
                for w, r in ((w, dict(results[w])) for w in names)}
    (out_dir / "patterns.json").write_text(
        json.dumps(patterns, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    cols = [
        "workflow", "n_tickets", "process_score", "top1_pct", "top3_pct", "top5_pct",
        "n_patterns", "pattern_entropy", "mean_jaccard", "jaccard_bimodality",
        "n_distinct_pairs", "herfindahl", "mean_steps", "median_steps", "steps_cv",
        "pct_no_action", "pct_resolved_unspecified",
        "phase_diagnose", "phase_change", "phase_coordinate", "phase_admin",
    ]
    with (out_dir / "scorecard.csv").open("w", newline="", encoding="utf-8") as f:
        w_ = csv.writer(f)
        w_.writerow(cols)
        for wf in sorted(names, key=lambda x: -results[x]["process_score"]):
            r = results[wf]
            pm = r["phase_mix"]
            w_.writerow(
                [wf, r["n_tickets"], r["process_score"], r["top1_pct"], r["top3_pct"],
                 r["top5_pct"], r["n_patterns"], r["pattern_entropy"], r["mean_jaccard"],
                 r["jaccard_bimodality"], r["n_distinct_pairs"], r["herfindahl"],
                 r["mean_steps"], r["median_steps"], r["steps_cv"], r["pct_no_action"],
                 r["pct_resolved_unspecified"],
                 pm["diagnose"], pm["change"], pm["coordinate"], pm["admin"]]
            )

    print(f"\n{'SCORE':>6} {'TOP3':>6} {'ENT':>6} {'JAC':>6} {'PATS':>6} {'N':>7}  WORKFLOW")
    print("-" * 92)
    for wf in sorted(names, key=lambda x: -results[x]["process_score"]):
        r = results[wf]
        print(
            f"{r['process_score']:>6} {r['top3_pct']:>5}% {r['pattern_entropy']:>6} "
            f"{r['mean_jaccard']:>6} {r['n_patterns']:>6} {r['n_tickets']:>7}  {wf[:44]}"
        )

    print(f"\nWrote {out_dir}/scorecard.json, scorecard.csv, patterns.json")


if __name__ == "__main__":
    main()
