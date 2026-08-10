#!/usr/bin/env python3
"""
Render the labelled decision graphs as a single self-contained HTML page of
Mermaid flowcharts — one per workflow, in the style of the scoping deck.

Reads results/graph.json (nodes/edges/labels from aggregate_graph.py +
label_nodes.py) and merged_data.json (effort metrics + category), writes
workflow_flowcharts.html.

Pure-local: no network, no secrets, no LLM. Every injected string is sanitised
for Mermaid and HTML-escaped so no value can break out of a tag or a
<pre>/<script> block. The page embeds only aggregated metrics + short distilled
labels + customer/category names — never raw ticket notes.
"""
from __future__ import annotations

import argparse
import html
import json
import re
from pathlib import Path

# Node classes -> Mermaid shape. Palette mirrors the scoping deck's classDefs.
CLASSDEFS = """
  classDef trig fill:#DDE4EA,stroke:#7C8B9B,stroke-width:1px,color:#12161C
  classDef act fill:#FFFFFF,stroke:#46586B,stroke-width:1.3px,color:#12161C
  classDef dec fill:#F7EFD6,stroke:#B0912C,stroke-width:1.2px,color:#12161C
  classDef block fill:#F9E3E5,stroke:#B03A48,stroke-width:1.2px,color:#12161C
  classDef drift fill:#EDE7F6,stroke:#7B5EA7,stroke-width:1.1px,stroke-dasharray:5 3,color:#12161C
  classDef done fill:#E2EEE7,stroke:#3F7A5A,stroke-width:1.2px,color:#12161C
  classDef hand fill:#E4EEF1,stroke:#2E7D8F,stroke-width:1.2px,color:#12161C""".rstrip()

_STADIUM = {"trig", "done"}


def humanize(node_id: str) -> str:
    s = node_id
    if s == "__start__":
        return "Start"
    if s.startswith("out:"):
        s = s[4:]
    return re.sub(r"[:_]+", " ", s).strip()


def mm(text: str, max_len: int = 64) -> str:
    """Sanitise a string for use inside a quoted Mermaid label: collapse
    whitespace, neutralise the characters that break Mermaid syntax, cap length."""
    t = re.sub(r"\s+", " ", str(text)).strip()
    t = t.replace('"', "'").replace("|", "/").replace("`", "").replace("{", "(").replace("}", ")")
    if len(t) > max_len:
        t = t[: max_len - 1].rstrip() + "…"
    return t


def _shape(cls: str, label: str) -> str:
    q = f'"{label}"'
    if cls in _STADIUM:
        return f"([{q}])"
    if cls == "dec":
        return f"{{{q}}}"
    return f"[{q}]"


def mermaid_for(g: dict) -> str:
    nodes_by_id = {n["id"]: n for n in g["nodes"]}
    ids: dict[str, str] = {}

    def nid(key: str) -> str:
        if key not in ids:
            ids[key] = f"N{len(ids)}"
        return ids[key]

    lines = ["flowchart TD"]
    for node in g["nodes"]:
        label = mm(node.get("label") or humanize(node["id"]))
        lines.append(f'  {nid(node["id"])}{_shape(node["cls"], label)}:::{node["cls"]}')

    def edge_label(e: dict) -> str | None:
        cond = e.get("label")
        if cond:
            return mm(f'{e["pct"]}% · {cond}', max_len=48)
        if nodes_by_id.get(e["src"], {}).get("cls") == "dec":
            return f'{e["pct"]}%'
        return None

    for e in g.get("edges", []):
        s, d = nid(e["src"]), nid(e["dst"])
        lbl = edge_label(e)
        lines.append(f'  {s} -->|"{lbl}"| {d}' if lbl else f"  {s} --> {d}")

    # Drift = the most common off-spine states, collapsed into one dashed
    # "also seen" annotation (like the scoping deck), not a graph of rare edges.
    drift = g.get("drift", [])
    if drift:
        items = ", ".join(humanize(d["key"]) for d in drift[:6])
        lines.append(f'  DRIFT["Also seen: {mm(items, 90)}"]:::drift')
        lines.append(f'  {nid("__start__")} -.-> DRIFT')

    lines.append("")
    lines.append(CLASSDEFS)
    return "\n".join(lines)


# ── metrics from merged_data (workflows[wf]['g']) ────────────────────────────

def metrics_row(gm: dict | None) -> str:
    if not gm:
        return ""
    n = gm.get("n", 0)
    aht = gm.get("aht", 0)
    hours = round(n * aht / 60, 1) if n and aht else 0
    frr = gm.get("frr", 0)
    cells = [
        ("Tickets", f"{n:,}"),
        ("Hours", f"{hours:,}"),
        ("AHT", f"{aht} min"),
        ("Touches", f"{gm.get('tch', 0)}"),
    ]
    m = "".join(f'<div class="m"><span class="mk">{html.escape(k)}</span>'
                f'<span class="mv">{html.escape(v)}</span></div>' for k, v in cells)
    m += ('<div class="m frr"><span class="mk">First-contact resolution</span>'
          f'<span class="mv">{frr}%</span>'
          f'<span class="bar"><i style="width:{max(0, min(100, frr))}%"></i></span></div>')
    return f'<div class="metrics">{m}</div>'


PAGE_CSS = """
:root{--ink:#12161C;--ink3:#6B7885;--ground:#171C24;--ground2:#202836;--paper:#FFFFFF;
--line:#C9D2DA;--signal:#E8C547;--disp:system-ui,-apple-system,"Segoe UI",sans-serif;
--mono:ui-monospace,SFMono-Regular,Menlo,monospace;}
*{box-sizing:border-box}body{margin:0;background:var(--ground);color:var(--ink);
font-family:Georgia,serif;line-height:1.5}
.mast{background:var(--ground);color:#E7ECF1;padding:48px 32px 32px;border-bottom:1px solid #2C3644}
.mast-in{max-width:1180px;margin:0 auto}
.eyebrow{font-family:var(--mono);font-size:11px;letter-spacing:.18em;text-transform:uppercase;color:var(--signal);margin:0 0 12px}
.mast h1{font-family:var(--disp);font-weight:700;font-size:clamp(28px,5vw,48px);margin:0 0 14px;letter-spacing:-.02em}
.tally{display:flex;flex-wrap:wrap;gap:26px;font-family:var(--mono)}
.tally b{display:block;font-size:22px;color:#E7ECF1}.tally span{font-size:10.5px;letter-spacing:.14em;text-transform:uppercase;color:#78899A}
.shell{max-width:1180px;margin:0 auto;padding:32px}
.cat{font-family:var(--disp);font-weight:700;font-size:22px;color:#E7ECF1;margin:36px 0 10px;padding-bottom:8px;border-bottom:2px solid var(--signal)}
.wf{background:var(--paper);border:1px solid var(--line);border-radius:10px;padding:20px 22px;margin:0 0 22px;overflow:hidden}
.wf-name{font-family:var(--disp);font-weight:700;font-size:18px;margin:0 0 4px}
.wf-sub{font-family:var(--mono);font-size:11px;color:var(--ink3);margin:0 0 14px}
.metrics{display:flex;flex-wrap:wrap;gap:22px;margin:0 0 16px;padding:12px 0;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
.m{display:flex;flex-direction:column;gap:3px}.mk{font-family:var(--mono);font-size:9.5px;letter-spacing:.12em;text-transform:uppercase;color:var(--ink3)}
.mv{font-family:var(--mono);font-size:15px;font-weight:600}
.frr{min-width:180px}.bar{display:block;height:5px;background:#E7ECF1;border-radius:3px;margin-top:4px}
.bar i{display:block;height:100%;background:#3F7A5A;border-radius:3px}
.chart{margin:6px 0 0;overflow-x:auto}
.legend{font-family:var(--mono);font-size:11px;color:#A9B6C4;max-width:1180px;margin:0 auto;padding:0 32px 8px}
.legend b{color:#78899A;font-weight:500}
.mermaid{min-height:40px}
"""

LEGEND = ('<div class="legend"><b>legend:</b> stadium = trigger / outcome · '
          'rectangle = action · diamond = decision · red = blocker/loop · '
          'teal = handoff · dashed purple = rare "also seen" (&lt;drift cutoff)</div>')


def build(graphs: dict, merged: dict, out_path: str) -> None:
    wf_meta = merged.get("workflows", {})
    # group workflows by category (fall back to "Other")
    by_cat: dict[str, list[str]] = {}
    for wf in graphs:
        cat = wf_meta.get(wf, {}).get("cat", "Other")
        by_cat.setdefault(cat, []).append(wf)

    total_tickets = sum(g.get("n", 0) for g in graphs.values())
    sections = []
    for cat in sorted(by_cat):
        cards = []
        for wf in sorted(by_cat[cat], key=lambda w: -graphs[w].get("n", 0)):
            g = graphs[wf]
            gm = wf_meta.get(wf, {}).get("g")
            desc = wf_meta.get(wf, {}).get("desc", "")
            drift_note = (f'{g["dropped_edges"]} rare route(s) shown dashed'
                          if g.get("dropped_edges") else "")
            cards.append(
                f'<article class="wf">'
                f'<h3 class="wf-name">{html.escape(wf)}</h3>'
                f'<p class="wf-sub">{html.escape(desc)}{("  ·  " + drift_note) if drift_note else ""}</p>'
                f'{metrics_row(gm)}'
                f'<figure class="chart"><pre class="mermaid">{html.escape(mermaid_for(g))}</pre></figure>'
                f'</article>'
            )
        sections.append(f'<h2 class="cat">{html.escape(cat)}</h2>' + "".join(cards))

    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Resolution Decision Flows</title>
<style>{PAGE_CSS}</style></head><body>
<header class="mast"><div class="mast-in">
<p class="eyebrow">TechOne · Decision Graph</p>
<h1>Resolution decision flows</h1>
<div class="tally">
  <div><b>{len(graphs)}</b><span>workflows</span></div>
  <div><b>{total_tickets:,}</b><span>tickets</span></div>
</div></div></header>
{LEGEND}
<main class="shell">
{''.join(sections)}
</main>
<script src="https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.min.js"></script>
<script>mermaid.initialize({{startOnLoad:true,securityLevel:'strict',theme:'neutral',flowchart:{{useMaxWidth:true}}}});</script>
</body></html>
"""
    Path(out_path).write_text(page, encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--graph", default="results/graph.json")
    ap.add_argument("--merged-data", default="merged_data.json")
    ap.add_argument("--out", default="workflow_flowcharts.html")
    args = ap.parse_args()

    graphs = json.loads(Path(args.graph).read_text(encoding="utf-8"))
    merged = json.loads(Path(args.merged_data).read_text(encoding="utf-8")) \
        if Path(args.merged_data).exists() else {"workflows": {}}
    build(graphs, merged, args.out)
    print(f"Wrote {args.out}  ({len(graphs)} workflows)")


if __name__ == "__main__":
    main()
