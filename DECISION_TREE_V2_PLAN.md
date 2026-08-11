# Decision-Tree v2 — discovering the decision structure from ticket data

## Context

The goal is an **accurate decision-tree model of the decisions engineers actually took** to
resolve tickets — one diagram per workflow, in the style of the hand-authored MERCOR "workflow
deep dive" deck (`72_workflow_flowcharts_1.html`): a trigger, a spine of actions, **question
diamonds** ("Is the device online and rebooted?") with descriptively-labeled branches, wait-loops
back to blockers, a dashed out-of-scope branch, and the first-contact-resolution rate on the
success edge.

**Why the current pipeline falls short.** The existing decision-graph pipeline (`extract_graph.py`
→ `aggregate_graph.py` → `label_nodes.py` → `build_flowcharts.py`) produces a *directly-follows
frequency graph*, not a decision tree:

- ~1 decision diamond per workflow; **33 of 73 workflows have none**.
- 66% of edges are bare arrows; the rest carry an anecdotal sample sentence, not a clean condition.
- ~250 distinct step-types per workflow are discarded into one opaque `DRIFT` node — that tail is
  exactly the "diagnosis/resolution paths captured in the data" we want surfaced.

**Root cause.** Each step records a one-sided causal `guard` (the reason the *taken* action was
taken) but never the **decision axis** — the *question* the engineer resolved. So aggregation
cannot know that `reboot_resolved` and `still_failing` are two answers to the same question, and
cannot cluster answers into a decision.

**Intended outcome.** Discover the decision structure from the notes (free-text), let **ticket
frequency decide what earns a place in the tree** (common decisions become diamonds; the long tail
collapses), keep every branch percentage a plain, auditable ticket count, and render in the target
grammar. Cost/re-extraction is acceptable — accuracy is the priority.

## Decisions locked (with the user)

1. **Discovery-first, free-text** capture of decisions (not a pre-defined coded decision vocab).
2. **Frequency-prioritized**: the most common decision structures are foregrounded; rare ones are
   gated out.
3. Clustering of the discovered questions uses **LLM merge** (not embeddings) — fits the
   Anthropic-only stack, does translation + canonical phrasing in one pass, no cosine threshold to
   tune. Counts never depend on the merge; they come from the raw ticket tally.
4. **Pilot before the full re-extract**: validate the schema on 2–3 workflows (including the
   Infrastructure/RMM one) before committing to the full 139k-ticket run.

## Approach — five stages

```
extract_graph.py  →  normalize_decisions.py  →  aggregate_graph.py  →  label_nodes.py  →  build_flowcharts.py
  (+decision_q/a)      (NEW: LLM merge)           (decision-aware)        (unchanged-ish)    (target grammar)
```

### Stage 1 — Extraction: capture the decision, not just the guard

**Files:** `prompts_graph.py`, `extract_graph.py`, `taxonomy_graph.json` (docs only).

Add two nullable free-text fields to each **trace step**, alongside the existing coded triple and
`guard`:

- **`decision_q`** — the question the engineer resolved at/before this step, **written in English
  regardless of note language**, ≤10 words. `null` when the step is not a decision point.
  e.g. `"is the device online and rebooted?"`
- **`decision_a`** — *this ticket's* answer to that question, English, ≤8 words. `null` when no
  decision. e.g. `"no, still offline"`

The coded `guard`/`action`/`object` stay — they are the **deterministic scaffold**: the common
decisions align with coded transitions, so the high-frequency core is counted near-exactly, while
`decision_q`/`decision_a` supply the discovered question wording and surface less-common-but-real
decisions. `guard_detail` is superseded by `decision_a` (kept extracted, harmless).

Concrete edits:
- `prompts_graph.py`: extend the "What to extract per ticket" section and the output schema
  (`prompts_graph.py:56-108`) to define `decision_q`/`decision_a`; add the **English-output** and
  **PII** rules to `_FREETEXT_RULE` neighbours; update `GRAPH_FEW_SHOT` (`prompts_graph.py:132`) so
  at least the password-reset example shows a real decision pair (e.g. a `self_service_failed` /
  `hybrid_account` step carries `decision_q: "did the self-service reset work?"`,
  `decision_a: "no, escalated to manual"`).
- `extract_graph.py`: extend `_clean_trace` to carry the two new fields through
  `parse_result_text`, word-capping them via the existing `_clean_text(..., max_words=…)` and
  keeping them nullable. `GRAPH_EMPTY` unchanged.

**Model:** extraction runs on the Batch API on **`claude-sonnet-4-6`** — already the
`DEFAULT_MODEL` in `extract_graph.py`, so no model-config change is needed. Confirm exact params
against the `claude-api` skill at code time; reuse the existing chunking (`BATCH_BYTES_BUDGET`,
`chunk_by_bytes`) so we never hit the 256 MB / request-size ceiling.

**PII invariant (unchanged):** `batch_runner.py` never persists raw `notes`; new free-text fields
are word-capped and carry the explicit "no names / emails / phone / credentials" rule.

### Stage 2 — Normalize: frequency-anchored LLM merge (NEW)

**File:** `normalize_decisions.py` (new). **Output:** `results/decisions.json`.

Per workflow:

1. **Collect & count (deterministic).** Walk deduped tickets; for each step with a `decision_q`,
   emit `(decision_q, decision_a, coded_context, ticket_id)` where `coded_context` is the coded
   transition it scaffolds (the step's `action:object` + `guard`). Canonicalize text cheaply
   (lowercase, strip punctuation, collapse whitespace) and **count distinct questions by ticket
   support**.
2. **Bucket by the coded scaffold (deterministic).** Group distinct questions by `coded_context`
   first — prevents merging similar-sounding questions that sit at different points in the process,
   and shrinks each clustering problem to a handful of candidates.
3. **Frequency-anchored merge (one LLM call per workflow).** Feed the distinct questions **in
   descending frequency order with counts + coded context**; instruct the model to treat the most
   frequent phrasings as **anchors**, merge rarer phrasings into them (translating as needed),
   write **one crisp canonical question** per cluster, and **leave genuinely distinct rare
   questions unclustered**. Output: `{raw_question → cluster_id}` + `canonical_q` per cluster.
4. **Branches — same procedure one level down.** For each canonical decision, pool the `decision_a`
   answers of its member tickets and merge into **canonical branches**, each with a `next_hint`
   (the most common coded step that followed that answer).
5. **Gate & rank (deterministic).** Keep a decision as a diamond only if
   `support ≥ min_decision_support`; keep a branch only if `support ≥ min_branch_support`; fold the
   rest into an "other" exit and into the workflow drift tally. Sort by support.

**Output shape (`results/decisions.json`):**
```json
"RMM Performance & Health Alert": {
  "n": 4472,
  "decisions": [
    { "id": "d0", "canonical_q": "Is the device online and rebooted?",
      "count": 3120, "support": 69.8, "coded_context": "restart:device_desktop",
      "branches": [
        { "canonical_a": "Yes, back online", "count": 2588, "support": 57.9, "next_hint": "verify:service" },
        { "canonical_a": "No, still offline", "count": 402, "support": 9.0, "next_hint": "await_response:user_account" }
      ],
      "members": ["device online?", "is het toestel weer online?", "rebooted and reachable?"] }
  ]
}
```

**Key properties:** every `support` is a plain ticket count from step 1 — the LLM only decides
grouping and wording, never the numbers. Cached in `results/decision_cache.json` keyed on a hash of
the candidate list, so re-runs are free and reproducible; ties break `(count desc, string asc)`.
**Model:** `claude-sonnet-4-6` (strong enough for the semantic clustering + cross-lingual +
canonical phrasing; ~73 calls total, cheap; keeps the whole pipeline on one model).

### Stage 3 — Aggregate: decision-aware graph

**File:** `aggregate_graph.py`.

Consume `results/decisions.json` and build the target topology:

- Build the **spine** from the most common canonical action sequence (reuse `canonical_sequence`,
  `aggregate_graph.py:50`).
- **Insert diamonds** at the steps where `decisions.json` has an above-threshold decision, instead
  of the current out-degree≥2 + guarded-out heuristic (`aggregate_graph.py:176-184`). Each diamond
  gets `cls="dec"`, `label=canonical_q`, and one out-edge per canonical branch, weighted by branch
  `support`, routed toward `next_hint` and onward to the outcome.
- **Wait-loops:** a branch whose `next_hint` is a block/await state renders as a `:::block` node
  that arrows **back to the diamond** (idiomatic in the target deck).
- **Recover branches from drift:** because decisions now carry their own gated branches, the
  wholesale `DRIFT` dump shrinks; keep the dashed out-of-scope branch for genuinely
  out-of-definition work, annotated with its `%`.
- Preserve the existing PII scrub (drop internal `_tickets` sets before serialization).

Emit the same `results/graph.json`, now with real multi-branch diamonds and branch labels.

### Stage 4 — Label: unchanged role, smaller job

**File:** `label_nodes.py`. The canonical question **is** the diamond label (from Stage 2), so
labeling only handles non-decision **action** nodes (its existing coverage-gated intent summary,
`label_nodes.py:91-128`). Keep the crash-proof JSON fallback and `results/label_cache.json`.

### Stage 5 — Render: the target grammar

**File:** `build_flowcharts.py`.

- **Question diamonds:** `dec` nodes render `{ "…?" }` using `canonical_q` (already diamond-shaped
  via `_shape`, `build_flowcharts.py:55-61`).
- **Branch labels:** `edge_label` (`build_flowcharts.py:78-89`) emits `"<pct>% · <canonical_a>"`.
- **FCR on the success edge:** pull FCR% / touches from `merged_data.json` metrics and annotate the
  edge into the resolved outcome, matching the deck's `"83 percent, one touch"`.
- **Wait-loops & drift:** render block loop-backs and the single dashed out-of-scope node as today.
- Keep the exact `CLASSDEFS` palette (`build_flowcharts.py:24-31`) and Mermaid init already copied
  from the deck.

## Config / thresholds (tunable, set on the pilot)

| knob | default | meaning |
|---|---|---|
| `min_decision_support` | 5% of workflow tickets | a `decision_q` cluster becomes a diamond |
| `min_branch_support` | 3% | a branch renders; rest → "other" exit |
| `max_branches` | 4 | branches per diamond (target deck is 2–3) |
| `merge_top_n` | covers ≥90% of tickets (cap ~200) | distinct questions fed to the merge call |

## Rollout

New branch **`decision-tree-v2`** off `route-b-decision-graphs`; commit each stage.

1. Stage 1 schema + prompt edits; update `tests/test_extract_graph.py`. **Run Corridor
   `analyzePlan` before writing code** (per global config).
2. **Pilot extract** — full extraction of these **4 workflows** via `--workflows` filter, on
   Sonnet 4.6, chosen to exercise different tree shapes:
   - **RMM Performance & Health Alert** (Infrastructure) — the diagnostic tree in the target image
   - **Password Reset** (Identity & Access) — a routing tree
   - the **EDR / Endpoint Security Alert** workflow — a triage/disposition tree
   - **Backup Monitoring, Failure Remediation & Restore** — the frequency-hub that currently has
     zero decisions and most needs recovered structure
   Small spend.
3. Build `normalize_decisions.py` + aggregate/render changes; run the pilot end-to-end.
4. **Eyeball the pilot trees against the RMM target image**; tune thresholds / prompt / few-shot.
5. **Full re-extract** (all 139k, Opus, chunked) once the pilot looks right.
6. Full normalize → aggregate → label → render; regenerate `workflow_flowcharts.html`.
7. Commit, push, offer PR.

## Verification

- **Offline unit tests** (extend `tests/`): `decision_q`/`decision_a` parse + word-cap
  (`test_extract_graph.py`); normalize gating + deterministic tie-break with a stubbed merge cache
  (`test_normalize_decisions.py`, new); aggregate diamond insertion + branch routing
  (`test_aggregate_graph.py`). All offline, no API.
- **Count invariants:** each branch `support ≤` its decision `support`; branch supports for a
  decision sum to ≤ decision support (remainder = "other"); every rendered `%` traces to a raw
  ticket count in `graph_raw`.
- **Visual check:** open the pilot `workflow_flowcharts.html`; confirm the RMM workflow shows
  question diamonds, ≥2 labeled branches, a wait-loop, a drift branch, and FCR on the success edge
  — i.e. it reads like the target image.
- **PII spot-check:** grep `results/decisions.json`, `results/graph.json`, and the HTML for any
  names/emails; confirm only aggregated counts + distilled English labels are present.

## Risks & mitigations

- **`decision_q` sparsity** (engineers don't always state the question) → the coded scaffold still
  yields the common decisions; frequency-gating means thin decisions simply don't render (honest,
  not fabricated).
- **Merge non-determinism** → cached by input hash; first computation is a model judgment but the
  numbers are not, and grouping only affects gated-in common decisions.
- **Batch cost** on 139k tickets → comparable to the prior ~$213 Sonnet 4.6 run (same model; the
  two added free-text fields add a little output, negligible).
- **Over-branching** → `max_branches` + `min_branch_support` keep diagrams as legible as the deck.
