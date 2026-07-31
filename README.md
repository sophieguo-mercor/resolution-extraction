# Resolution-step extraction & process-variance analysis

Turns free-text ticket `Notes` into structured resolution steps, then measures how
consistently each workflow is actually resolved.

The point: n-gram overlap tells you whether tickets use the same *words*. This tells
you whether they follow the same *process*. Two agents writing "wachtwoord gereset"
and "nieuw WW ingesteld via AD" share almost no words but perform the same action —
this pipeline counts them as the same step.

---

## Layout

```
resolution-extraction/
├── README.md
├── Makefile              convenience wrappers for the commands below
├── requirements.txt
├── .env.example          copy to .env, add your API key
├── .gitignore            excludes data/, results/ and .env
├── taxonomy.json         the controlled vocabulary — tune this
├── prompts.py            prompt construction — tune this
├── extract.py            batch runner: notes → structured steps
├── aggregate.py          steps → per-workflow variance metrics
├── run.py                end-to-end orchestrator: .xls → explorer.html
├── build_explorer.py     the React/JSX explorer template (edit the UI here)
├── build_html.py         renders template + data → one self-contained .html
├── tests/
│   └── test_aggregate.py smoke test for the metric maths
├── data/                 put the .xls here (gitignored — contains PII)
└── results/
    ├── raw/              one .jsonl per workflow
    ├── batch_manifest.json  in-flight batch bookkeeping (auto-created/deleted)
    ├── scorecard.json
    ├── scorecard.csv
    └── patterns.json
```

Generated build artifacts — `explorer.html` and `merged_data.json` — are gitignored:
they're regenerable and derived from the PII-bearing `.xls` (they embed customer
company names and effort aggregates), so they're treated as output, not source.

Extraction runs on the **Anthropic Message Batches API**: the whole corpus is packed
into a single batch (12 tickets per request), submitted once, then collected. That's
~50% cheaper per token than synchronous calls and runs on a separate high-throughput
queue instead of fighting your per-minute rate limit. The trade-off is latency — a
batch completes in anywhere from minutes to a few hours, with no SLA.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate      # on Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env          # then add your key
                              # (or just: export ANTHROPIC_API_KEY=sk-ant-...)
```

Once the venv is activated, `make`/`python` commands below all run inside it.
Deactivate any time with `deactivate`; re-activate with `source .venv/bin/activate`
before running the scripts again in a new shell.

Put the export at `data/Time_entries_Mercor_v3_workflows_by_company__1_.xls`,
or point `--xls` somewhere else.

## Run it

Either use the Makefile:

```bash
make dryrun                              # prompt + request estimate, costs nothing
make sample WF="Password Reset" N=20     # small real run — then READ the jsonl
make pilot                               # 60 tickets per workflow, ~4.4k requests
make full                                # entire corpus, ~10.4k requests
make collect                             # reconnect to an in-flight batch
make aggregate                           # compute metrics
```

Or call the scripts directly:

```bash
# 1. Dry run — prints the exact prompt and a request-count estimate. Costs nothing.
python extract.py --sample 20 --workflows "Password Reset" --dry-run

# 2. Small real run. Submits a batch, waits for it, writes results/raw/Password_Reset.jsonl.
#    Then OPEN the jsonl and read it.
python extract.py --sample 20 --workflows "Password Reset"

# 3. Tune taxonomy.json / prompts.py, delete that jsonl, repeat step 2
#    until the steps look right. This is the important loop — don't skip it.

# 4. Representative run across every workflow (~4.4k requests)
python extract.py --sample 60

# 5. Or the full corpus (~10.4k requests)
python extract.py

# 6. Compute metrics
python aggregate.py
```

### How a run behaves

Each invocation submits **one batch**, then blocks polling until it finishes and
writes the results. A batch can take minutes to hours, so for big runs you can
detach the two halves:

```bash
python extract.py --submit-only     # create the batch, print its id, exit
python extract.py --collect         # later: reconnect, wait, write results
```

`extract.py` is resume-safe on two levels:

- **JSONL** — ticket IDs already in the output are skipped when building requests,
  and again when writing results. Re-runs never duplicate.
- **Batch** — the in-flight batch is recorded in `results/batch_manifest.json`. If
  you Ctrl-C during the wait and re-run, it *reconnects* to the same batch instead
  of submitting (and paying for) a second one. The manifest holds no raw ticket text
  — only the IDs needed to route results back to the right workflow file. Pass
  `--force-new` to discard a stale manifest and start over.

Verify the metric maths at any time with `python -m pytest tests/ -q`
(or `python tests/test_aggregate.py` if you'd rather not install pytest).

---

## Scale and cost

Measured against the real file:

| | |
|---|---|
| Tickets | 144,703 |
| Filtered out locally as pure boilerplate | 20,012 (13.8%) |
| Actually sent to the API | 124,691 |
| Batch requests at `--batch-size 12` | ~10,390 |

All ~10,390 requests go in a single batch. Batch pricing is ~50% of synchronous, and
the shared taxonomy system prompt is `cache_control`'d so it's billed once and read
from cache for the rest of the batch.

The local pre-filter (`is_probably_empty`) drops `-`, `Ticket complete`,
`See Internal Notes`, `Reistijd`, bare `Onsite ondersteuning` and similar before
spending anything. It's deliberately conservative: `"Client secrets opgeruimd."`
is kept, `"Uitvoer remote beheer"` is dropped.

**Start with `--sample 60`.** 60 tickets per workflow gives stable frequency
estimates for the top patterns at ~4% of the cost of the full run. Only go full-corpus
if you need per-company breakdowns within each workflow.

---

## Tuning the taxonomy

`taxonomy.json` is the controlled vocabulary — 30 actions, 44 objects, 16 systems.
Every extracted step is coerced to these values, so unknown output becomes `other`
rather than polluting the counts.

Watch for two failure modes when you read the sample output:

- **Too much `other`** in the action or object slot → the vocabulary is missing
  something real. Add it.
- **One pair dominating everything** (e.g. `configure:application` at 60%) → that
  bucket is too coarse and is hiding real variation. Split it.

The granularity instruction in `prompts.py` matters. Notes like
*"reset in M365 / reset in lokaal AD / sync uitgevoerd"* must stay three steps —
collapsing them to one "reset password" throws away exactly the multi-system
variance you're looking for.

---

## Output

```
results/
├── raw/<Workflow_Name>.jsonl   one line per ticket, with its extracted steps
├── scorecard.json              all metrics per workflow
├── scorecard.csv               same, flat, for spreadsheets
└── patterns.json               top resolution patterns + top action:object pairs
```

A raw record:

```json
{"ticket_id": "T20260113.0010", "company": "datavisual",
 "workflow": "Password Reset", "hours": 0.25, "touches": 1,
 "steps": [{"action": "contact_customer", "object": "user_account", "system": "unknown"},
           {"action": "reset", "object": "password", "system": "active_directory"},
           {"action": "inform_customer", "object": "password", "system": "unknown"}]}
```

---

## Visualization — the HTML explorer

`run.py` joins the effort data from the `.xls` (AHT, first-response rate, effort
distribution per workflow and per company) with the process-variance metrics from
`scorecard.json` / `patterns.json`, and renders a single self-contained
`explorer.html` — an interactive React page that opens in any browser.

```bash
# Build from the results already in results/ (no API spend):
make explorer            # → explorer.html

# Equivalently:
python run.py --html-only

# Full pipeline from scratch (extract → aggregate → build):
python run.py --xls data/<export>.xls          # runs the Batch API extraction
python run.py --xls data/<export>.xls --dry-run # show the plan, spend nothing
```

How the build works:

- **`build_explorer.py`** holds the UI as a JSX template string (`TEMPLATE = r'''…'''`)
  with a `__DATA__` placeholder and `const DATA = __DATA__`. Edit the explorer UI here.
- **`build_html.py`** regex-extracts that template, injects the merged metrics as JSON
  in place of `__DATA__`, and wraps it in an HTML page that loads React + Babel from
  the unpkg CDN (versions pinned). The JSON is embedded with `<` escaped to `<`
  so no data value can break out of the `<script>` block.
- The output embeds **only aggregated metrics** — never raw ticket notes — but it does
  carry customer company names and effort aggregates, so `explorer.html` and the
  intermediate `merged_data.json` are gitignored. Rebuild them anytime with `make explorer`.

The page is self-contained apart from the two CDN `<script>` tags; to run fully
offline, swap those for locally-hosted copies of React and Babel.

---

## Reading the metrics

A **pattern** is the unordered set of `action:object` pairs in one ticket.
Two tickets share a pattern if they did the same things, regardless of wording or order.

### Pattern concentration
| Metric | Meaning | Good |
|---|---|---|
| `top1_pct` / `top3_pct` | % of tickets explained by the 1 / 3 most common patterns | high |
| `n_patterns` | how many distinct patterns exist | low relative to `n_tickets` |
| `pattern_entropy` | 0 = every ticket identical, 1 = every ticket unique | low |

### Action structure
| Metric | Meaning | Good |
|---|---|---|
| `mean_jaccard` | average set-overlap between two random tickets | high |
| `jaccard_bimodality` | 0 = one population, 1 = a clean 50/50 split | **low — see below** |
| `n_distinct_pairs` | breadth of the action vocabulary actually used | low |
| `herfindahl` | concentration of the pair distribution | high |

### Effort shape
| Metric | Meaning |
|---|---|
| `mean_steps` / `median_steps` / `steps_cv` | how many actions, and how consistently |
| `phase_mix` | % of steps that are diagnose / change / coordinate / admin |
| `pct_no_action` | % of tickets where no concrete action was found |

`phase_mix` is the interpretive one. A change-heavy workflow is executable work.
A diagnose-heavy one is investigation whose length you can't predict. A
coordinate-heavy one is mostly waiting on other people — which is a scheduling
problem, not an automation target.

### `process_score` (0–100)

```
0.25 · top3_pct           ↑  concentrated patterns
0.20 · pattern_entropy    ↓  inverted
0.20 · mean_jaccard       ↑  tickets resemble each other
0.15 · action_coverage    ↑  (100 − pct_no_action)
0.10 · steps_cv           ↓  inverted
0.10 · phase_mix.change   ↑  real changes, not investigation
```

Each term is min-max normalised **across the workflows in your run**, so the score
is a *ranking within this dataset*, not a portable rating. Re-running on a different
set of workflows shifts the numbers. Weights are one line in `aggregate.py` — change
them if you weight things differently.

---

## The two signals worth acting on

**`jaccard_bimodality` above ~0.5 is the split signal.** It means the workflow contains
two distinct sub-populations that happen to share a label. That's a taxonomy problem,
and no amount of process standardisation fixes it — you split the workflow first.

**High `top3_pct` with low `mean_steps` is the automation signal.** A handful of
patterns cover most tickets, and each takes few actions. Look up those patterns in
`patterns.json` and you have the runbook, already written.

A useful cross-check: compare `process_score` against the distributional CV/tail
metrics from the explorer. Where they *disagree* is where the interesting findings
are — a workflow with tight effort but scattered processes means agents reach the
same outcome by different routes (standardise the procedure), while scattered effort
but tight process means the same procedure sometimes hits a hard case (add triage).

---

## Known limitations

- **Sampling.** At `--sample 60`, pattern frequencies for rare patterns are noisy.
  Top-3 coverage is stable; the tail is not.
- **LLM variance.** The same note can extract slightly differently across runs.
  Frequencies are stable in aggregate; individual tickets are not. Set `--seed` for
  reproducible sampling, but the model output itself isn't deterministic.
- **Order is significant.** Patterns are ordered sequences of `action:object`
  pairs, so "investigate → escalate → fix" and "fix → escalate → investigate"
  count as different patterns (duplicate pairs are dropped on first occurrence).
  This makes the pattern-concentration metrics stricter. The Jaccard family
  (`mean_jaccard`, `jaccard_bimodality`, `n_distinct_pairs`) remains order-free
  set-overlap. To go back to unordered patterns, revert `pattern_key()` in
  `aggregate.py` to `tuple(sorted({pair_key(s) for s in real_steps(steps)}))`.
- **`pct_no_action` conflates two things:** tickets with genuinely no work, and
  tickets whose notes were too terse to extract from. Check a few by hand before
  reading it as a finding.
