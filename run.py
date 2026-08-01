#!/usr/bin/env python3
"""
End-to-end pipeline: spreadsheet → HTML explorer

Usage
-----
# Full run (LLM extraction + HTML build):
    python run.py --xls data/export.xls

# Skip extraction if you already have a scorecard:
    python run.py --xls data/export.xls --scorecard results/scorecard.json

# Just rebuild the HTML from existing results:
    python run.py --html-only

# Dry run: show what would be called, no API spend:
    python run.py --xls data/export.xls --dry-run

Stages
------
1. parse       — load the .xls, compute effort/distribution data (no API)
2. extract     — send notes to Claude via the Batch API, write results/raw/*.jsonl
3. aggregate   — compute process-variance metrics → scorecard.json, patterns.json
4. merge       — join effort data + scorecard + patterns → merged_data.json
5. build       — inject merged_data.json into HTML template → distributional_shape_explorer.html

Extraction here uses this repo's Batch-API extract.py: it submits one batch,
waits, then writes results. It is resume-safe — Ctrl-C and re-run to reconnect.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

# ── helpers ──────────────────────────────────────────────────────────────────

def log(msg: str, *, section: bool = False) -> None:
    if section:
        print(f"\n{'─'*60}\n  {msg}\n{'─'*60}", flush=True)
    else:
        print(f"  {msg}", flush=True)


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    log(f"$ {' '.join(cmd)}")
    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0:
        raise SystemExit(f"Command failed: {' '.join(cmd)}")
    return result


# ── Stage 1: parse the spreadsheet into effort/distribution data ─────────────

NS = {"ss": "urn:schemas-microsoft-com:office:spreadsheet"}

def _cell(c):
    d = c.find("ss:Data", NS)
    return d.text if d is not None else None

def parse_spreadsheet(xls_path: str) -> dict:
    """
    Parse the SpreadsheetML .xls export into a compact data structure
    containing per-workflow and per-company effort distributions.

    Returns a structure the merge step can join with the LLM-derived
    scorecard/patterns.
    """
    import statistics

    log(f"Parsing {xls_path} …")
    tree = ET.parse(xls_path)
    root = tree.getroot()
    wss = {
        ws.get("{urn:schemas-microsoft-com:office:spreadsheet}Name"): ws
        for ws in root.findall("ss:Worksheet", NS)
    }

    def get_rows(ws):
        table = ws.find("ss:Table", NS)
        return [
            [_cell(c) for c in row.findall("ss:Cell", NS)]
            for row in table.findall("ss:Row", NS)
        ]

    # ── Workflow Dashboard (category + description + complexity) ───────────
    dash = get_rows(wss["Workflow Dashboard"])[1:]
    wf_cat  = {r[1]: r[0] for r in dash if r[1]}
    wf_desc = {r[1]: r[2] for r in dash if r[1] and len(r) > 2 and r[2]}
    wf_cx   = {r[1]: r[8] for r in dash if r[1]}

    # ── Ticket Detail ──────────────────────────────────────────────────────
    ticket_rows = get_rows(wss["Ticket Detail"])[1:]
    by_wf: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    company_all: dict[str, list] = defaultdict(list)

    for r in ticket_rows:
        company = r[1]; wf = r[2]
        try:
            h = float(r[3]) if r[3] else 0.0
            t = int(float(r[4])) if r[4] else 1
        except (TypeError, ValueError):
            continue
        if not wf:
            continue
        by_wf[wf][company].append((h, t))
        company_all[company].append((h, t))

    def stats(records):
        if not records:
            return None
        H = [r[0] for r in records]; T = [r[1] for r in records]; n = len(records)
        mean = statistics.mean(H)
        sd   = statistics.stdev(H) if n > 1 else 0.0
        cv   = round(sd / mean, 3) if mean else 0.0
        sH   = sorted(H)
        def pct(p):
            i = (p / 100) * (len(sH) - 1)
            lo, hi = int(i), min(int(i)+1, len(sH)-1)
            return sH[lo] + (i - lo) * (sH[hi] - sH[lo])
        med = pct(50); p90 = pct(90)
        tail = round(p90 / med, 2) if med else 0.0
        b = {"a": 0, "b": 0, "c": 0, "d": 0}
        for h in H:
            if h < 0.5:   b["a"] += 1
            elif h < 2.0: b["b"] += 1
            elif h < 6.0: b["c"] += 1
            else:          b["d"] += 1
        bp  = {k: round(v/n*100, 1) for k, v in b.items()}
        frr = round(sum(1 for r in records if r[1] == 1) / n * 100, 1)
        return {
            "n": n, "aht": round(mean * 60, 1), "frr": frr,
            "tch": round(statistics.mean(T), 2), "cv": cv, "tail": tail,
            "med": round(med, 3), "p90": round(p90, 3),
            "p25": round(pct(25), 3), "p75": round(pct(75), 3),
            "p95": round(pct(95), 3), "max": round(max(H), 2),
            "b": bp,
        }

    # ── Build output structure ─────────────────────────────────────────────
    wf_order = sorted(by_wf.keys())
    cats: dict[str, list] = {}
    workflows: dict[str, dict] = {}

    for wf in wf_order:
        allrec = [rec for comp_recs in by_wf[wf].values() for rec in comp_recs]
        by_comp = {c: stats(recs) for c, recs in by_wf[wf].items()}
        cat = wf_cat.get(wf, "Other")
        cats.setdefault(cat, []).append(wf)
        workflows[wf] = {
            "cat":  cat,
            "cx":   wf_cx.get(wf, ""),
            "desc": wf_desc.get(wf, ""),
            "g":    stats(allrec),
            "by":   {c: v for c, v in by_comp.items() if v},
            "pr":   None,   # filled in by merge step
        }

    log(f"  → {len(wf_order)} workflows, {len(company_all)} companies, "
        f"{sum(len(r) for wf in by_wf.values() for r in wf.values())} tickets")

    return {
        "workflows":      workflows,
        "wf_order":       wf_order,
        "cats":           cats,
        "companies":      sorted(company_all.keys()),
        "company_summary": {c: stats(recs) for c, recs in company_all.items()},
    }


# ── Stage 4: merge effort data + scorecard + patterns ───────────────────────

def merge(effort_data: dict, scorecard_path: str, patterns_path: str) -> dict:
    log("Merging effort data with scorecard + patterns …")
    sc  = json.loads(Path(scorecard_path).read_text())
    pat = json.loads(Path(patterns_path).read_text())
    matched = 0
    for wf, rec in effort_data["workflows"].items():
        v = sc.get(wf)
        if not v:
            rec["pr"] = None
            continue
        matched += 1
        tp = (pat.get(wf) or {}).get("top_patterns") or v.get("top_patterns") or []
        tq = (pat.get(wf) or {}).get("top_pairs")    or v.get("top_pairs")    or []
        rec["pr"] = {
            "score":  v["process_score"],
            "n":      v["n_tickets"],
            "top1":   v["top1_pct"],
            "top3":   v["top3_pct"],
            "top5":   v["top5_pct"],
            "npat":   v["n_patterns"],
            "ent":    v["pattern_entropy"],
            "jac":    v["mean_jaccard"],
            "bimod":  v["jaccard_bimodality"],
            "npairs": v["n_distinct_pairs"],
            "hhi":    v["herfindahl"],
            "steps":  v["mean_steps"],
            "scv":    v["steps_cv"],
            "noact":  v["pct_no_action"],
            "unspec": v.get("pct_resolved_unspecified", 0),
            "ph":     v["phase_mix"],
            "pats":  [{"p": x["pattern"], "pct": x["pct"]} for x in tp[:6]],
            "pairs": [{"p": x["pair"],    "pct": x["pct"]} for x in tq[:10]],
        }
    log(f"  → matched {matched}/{len(effort_data['workflows'])} workflows")
    return effort_data


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--xls",        default="data/Time_entries_Mercor_v3_workflows_by_company__1_.xls")
    ap.add_argument("--scorecard",  default=None, help="skip extraction, use this scorecard.json")
    ap.add_argument("--patterns",   default=None, help="patterns.json (default: results/patterns.json)")
    ap.add_argument("--out",        default="distributional_shape_explorer.html")
    ap.add_argument("--results-dir",default="results")
    ap.add_argument("--sample",     type=int, default=0, help="tickets per workflow for LLM (0=all)")
    ap.add_argument("--html-only",  action="store_true", help="skip all extraction, just rebuild HTML")
    ap.add_argument("--dry-run",    action="store_true")
    args = ap.parse_args()

    results  = Path(args.results_dir)
    scorecard_path = args.scorecard or str(results / "scorecard.json")
    patterns_path  = args.patterns  or str(results / "patterns.json")

    t0 = time.time()

    # ── html-only shortcut ────────────────────────────────────────────────
    if args.html_only:
        log("HTML-only mode", section=True)
        for p in [scorecard_path, patterns_path]:
            if not Path(p).exists():
                raise SystemExit(f"Missing: {p}  (run without --html-only first)")
        effort_data = json.loads(Path("merged_data.json").read_text()) if Path("merged_data.json").exists() \
                      else parse_spreadsheet(args.xls)
        data = merge(effort_data, scorecard_path, patterns_path)
        Path("merged_data.json").write_text(json.dumps(data, separators=(",", ":")))
        import build_html as bh
        bh.build(data, "build_explorer.py", args.out)
        log(f"\nDone in {time.time()-t0:.0f}s → {args.out}", section=True)
        return

    # ── stage 1: parse spreadsheet ────────────────────────────────────────
    log("Stage 1 — Parse spreadsheet", section=True)
    if not Path(args.xls).exists():
        raise SystemExit(f"XLS not found: {args.xls}")
    effort_data = parse_spreadsheet(args.xls)
    Path("merged_data.json").write_text(json.dumps(effort_data, separators=(",", ":")))

    # ── stage 2: LLM extraction (skippable) ──────────────────────────────
    if Path(scorecard_path).exists():
        log(f"Stage 2 — Extraction SKIPPED (using {scorecard_path})", section=True)
    elif args.dry_run:
        log("Stage 2 — Extraction DRY RUN", section=True)
        cmd = [sys.executable, "extract.py", "--xls", args.xls, "--dry-run",
               "--sample", "20", "--workflows", "Password Reset"]
        run(cmd)
        log("\nDry run complete. Remove --dry-run and re-run to extract all workflows.")
        return
    else:
        log("Stage 2 — LLM extraction (Batch API)", section=True)
        cmd = [sys.executable, "extract.py", "--xls", args.xls]
        if args.sample:
            cmd += ["--sample", str(args.sample)]
        run(cmd)

    # ── stage 3: aggregate ────────────────────────────────────────────────
    if args.scorecard:
        log(f"Stage 3 — Aggregation SKIPPED (using {scorecard_path})", section=True)
    else:
        log("Stage 3 — Aggregate metrics", section=True)
        run([sys.executable, "aggregate.py",
             "--raw-dir", str(results / "raw"),
             "--out-dir", str(results)])

    # ── stage 4: merge ───────────────────────────────────────────────────
    log("Stage 4 — Merge", section=True)
    data = merge(effort_data, scorecard_path, patterns_path)
    Path("merged_data.json").write_text(json.dumps(data, separators=(",", ":")))

    # ── stage 5: build HTML ──────────────────────────────────────────────
    log("Stage 5 — Build HTML", section=True)
    import build_html as bh
    bh.build(data, "build_explorer.py", args.out)

    elapsed = time.time() - t0
    log(f"\nComplete in {elapsed/60:.1f} min  →  {args.out}", section=True)


if __name__ == "__main__":
    main()
