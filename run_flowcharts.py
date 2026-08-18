#!/usr/bin/env python3
"""
End-to-end decision-graph pipeline: spreadsheet → resolution decision flowcharts.

Stages
------
1. parse         — load the .xls, compute effort/category metrics      (no API, run.py)
2. extract_graph — notes → hybrid traces, results/graph_raw/*.jsonl    (LLM, Batch API)
3. aggregate     — traces → results/graph.json (decision graphs)       (no API)
4. label         — LLM node/branch display labels                      (LLM, cheap)
5. build         — graph.json + merged_data.json → workflow_flowcharts.html  (no API)

Only stages 2 and 4 spend money. Stage 2 is skipped automatically once
results/graph.json exists; --html-only rebuilds just the HTML from existing
results with no API spend.

Usage
-----
    python run_flowcharts.py --category "Identity & Access"     # the pilot
    python run_flowcharts.py --category "Identity & Access" --sample 60
    python run_flowcharts.py --html-only                        # rebuild HTML only
    python run_flowcharts.py --dry-run                          # show the plan, spend nothing
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import build_flowcharts
from run import log, parse_spreadsheet  # reuse the existing parse stage + logger


def run(cmd: list[str]) -> None:
    log(f"$ {' '.join(cmd)}")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        raise SystemExit(f"Command failed: {' '.join(cmd)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--xls", default="data/Time_entries_Mercor_v3_workflows_by_company__1_.xls")
    ap.add_argument("--category", help="limit extraction to one Workflow-Dashboard category")
    ap.add_argument("--workflows", nargs="*", help="limit extraction to these workflows")
    ap.add_argument("--sample", type=int, default=0, help="tickets per workflow for extraction (0=all)")
    ap.add_argument("--results-dir", default="results")
    ap.add_argument("--out", default="workflow_flowcharts.html")
    ap.add_argument("--label-model", default=None, help="override label_nodes model")
    ap.add_argument("--html-only", action="store_true", help="skip extraction/aggregate/label; rebuild HTML")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, no API spend")
    args = ap.parse_args()

    results = Path(args.results_dir)
    graph_path = results / "graph.json"
    merged_path = Path("merged_data.json")
    t0 = time.time()

    # ── stage 1: parse spreadsheet (needed for metrics + --category) ─────────
    if args.html_only:
        log("HTML-only mode", section=True)
        if not graph_path.exists():
            raise SystemExit(f"Missing {graph_path} — run without --html-only first.")
        merged = json.loads(merged_path.read_text()) if merged_path.exists() \
            else parse_spreadsheet(args.xls)
        graphs = json.loads(graph_path.read_text())
        build_flowcharts.build(graphs, merged, args.out)
        log(f"\nDone in {time.time()-t0:.0f}s → {args.out}", section=True)
        return

    log("Stage 1 — Parse spreadsheet", section=True)
    if not Path(args.xls).exists():
        raise SystemExit(f"XLS not found: {args.xls}")
    merged = parse_spreadsheet(args.xls)
    merged_path.write_text(json.dumps(merged, separators=(",", ":")))

    # ── stage 2: hybrid extraction (skippable) ───────────────────────────────
    extract_cmd = [sys.executable, "extract_graph.py", "--xls", args.xls]
    if args.workflows:
        extract_cmd += ["--workflows", *args.workflows]
    elif args.category:
        extract_cmd += ["--category", args.category]
    if args.sample:
        extract_cmd += ["--sample", str(args.sample)]

    if graph_path.exists():
        log(f"Stage 2 — Extraction SKIPPED (using {graph_path})", section=True)
    elif args.dry_run:
        log("Stage 2 — Extraction DRY RUN", section=True)
        run(extract_cmd + ["--dry-run"])
        log("\nDry run complete. Remove --dry-run to extract.")
        return
    else:
        log("Stage 2 — Hybrid extraction (Batch API)", section=True)
        run(extract_cmd)

        # ── stage 3: aggregate ────────────────────────────────────────────────
        log("Stage 3 — Aggregate decision graphs", section=True)
        run([sys.executable, "aggregate_graph.py",
             "--raw-dir", str(results / "graph_raw"), "--out", str(graph_path)])

        # ── stage 4: label ────────────────────────────────────────────────────
        log("Stage 4 — LLM node/branch labels", section=True)
        label_cmd = [sys.executable, "label_nodes.py", "--graph", str(graph_path)]
        if args.label_model:
            label_cmd += ["--model", args.label_model]
        run(label_cmd)

    # ── stage 5: build HTML ──────────────────────────────────────────────────
    log("Stage 5 — Build flowcharts HTML", section=True)
    graphs = json.loads(graph_path.read_text())
    build_flowcharts.build(graphs, merged, args.out)

    log(f"\nComplete in {(time.time()-t0)/60:.1f} min  →  {args.out}", section=True)


if __name__ == "__main__":
    main()
