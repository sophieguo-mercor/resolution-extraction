"""Prompt construction for the hybrid (coded + free-text) resolution-graph extractor.

Route B needs more than the ordered action:object:system steps that prompts.py
produces. Per ticket it also captures:
  - a coded `trigger` (presenting problem) + a short free-text `trigger_intent`
  - an ordered `trace` where each step carries the coded triple PLUS a coded
    `guard` (the condition that gated it, or null) and two short free-text fields
    (`guard_detail`, `intent`) for the workflow-specific colour
  - a coded `outcome` (terminal state) + a short free-text `outcome_intent`

The coded axes are what aggregate_graph.py counts (so branch percentages stay
population-true); the free-text axes are decorative — they feed the LLM node
labeller downstream and never touch a count.

Iterate on wording here, then eyeball a small sample:
    python extract_graph.py --sample 20 --workflows "Password Reset" --dry-run
    python extract_graph.py --sample 20 --workflows "Password Reset"
"""
from __future__ import annotations

import json

from prompts import FEW_SHOT, build_user_message  # reuse note-packing + few-shot input


# The free-text rule is repeated near every field on purpose: keep it distilled,
# short, and free of anything that could re-introduce raw PII.
_FREETEXT_RULE = (
    "a SHORT phrase (<=10 words) naming the workflow-specific detail — NOT a "
    "restatement of the coded value. Use null when there is nothing to add. "
    "NEVER include personal names, email addresses, phone numbers, or credentials."
)


def build_graph_system_prompt(taxonomy: dict, taxonomy_graph: dict) -> str:
    actions = taxonomy["actions"]
    objects = taxonomy["objects"]
    systems = taxonomy["systems"]
    triggers = taxonomy_graph["triggers"]
    guards = taxonomy_graph["guards"]
    outcomes = taxonomy_graph["outcomes"]

    action_lines = "\n".join(
        f"  {name} ({meta['phase']}) - {meta['desc']}" for name, meta in actions.items()
    )
    trigger_lines = "\n".join(f"  {name} - {desc}" for name, desc in triggers.items())
    guard_lines = "\n".join(f"  {name} - {desc}" for name, desc in guards.items())
    outcome_lines = "\n".join(f"  {name} - {desc}" for name, desc in outcomes.items())

    return f"""You reconstruct HOW an IT support agent resolved a ticket, as a structured trace, from helpdesk ticket notes.

The notes are written by Dutch MSP engineers. They are mostly Dutch, sometimes English, often both.
A single ticket's notes may contain several work sessions separated by `---`. Read all of them together:
the resolution is the full set of actions across every session.

## What to extract per ticket
1. `trigger` - the presenting problem that started the ticket (one coded value below).
2. `trace` - the ordered list of steps the AGENT ACTUALLY COMPLETED, each with:
     - `action`, `object`, `system` - exactly as in the coded vocabulary below.
     - `guard` - the condition that made the agent take THIS step next (one coded value
       below), or null when no condition is stated. Guards are how branches are recovered,
       so set one whenever the notes explain WHY a path was taken (e.g. self-service failed,
       account is hybrid, the first fix still failed).
     - `guard_detail` - {_FREETEXT_RULE}
     - `intent` - {_FREETEXT_RULE}
3. `outcome` - the terminal state of the ticket (one coded value below).
4. `trigger_intent`, `outcome_intent` - each {_FREETEXT_RULE}

## What counts as a step
Only actions the AGENT ACTUALLY COMPLETED. Each distinct action is one step, in order.

## What to IGNORE completely
- Greetings and sign-offs, automated acknowledgement text, CAB/Change-Advisory boilerplate
- Placeholder entries ("-", "Ticket complete", "See Internal Notes", "Reistijd", "Onsite ondersteuning")
- Raw email headers (Received:, From:, Subject:, DKIM/SPF blocks)
- What the CUSTOMER asked for, or what the agent merely PROMISED / PLANNED to do later
- Restatements of the same action in a later session

If nothing remains after ignoring the above, return an empty `trace`, `outcome` = "no_action",
and null intents. Do NOT invent steps to fill the gap.

## Alerts and reports ARE work
Reviewing a security/monitoring alert or a SOC/compliance report and dispositioning it - benign,
false positive, "geen gerelateerde incidenten", "geen credentials gevonden" - IS a completed step:
`triage` on `security_alert` (or `compliance_report`). If the agent added an allow-list entry so the
alert stops firing, use `add_exception`.

## Vague completions
When the notes say the ticket was resolved but give NO detail ("is opgelost", "geregeld",
"doorgevoerd", "gelukt"), return a single trace step with action `resolve_unspecified`,
object `other`, system `unknown`, and `outcome` = "resolved_unspecified". Use this ONLY when a
resolution is stated but unspecified.

## Granularity
Split genuinely different actions apart, even when they serve one goal - the multi-system work is
the interesting part. Example: reset in M365 AND reset in local AD AND a sync are three steps, not one.

## Output schema
Return ONLY a JSON array. One object per input ticket, in the same order, no markdown fences, no prose.

[{{"id": 0, "trigger": "...", "trigger_intent": null,
   "trace": [{{"guard": null, "guard_detail": null, "action": "...", "object": "...", "system": "...", "intent": null}}],
   "outcome": "...", "outcome_intent": null}}, ...]

- `action` / `object` / `system` MUST be exactly one of the vocab values below ("unknown" system when unstated).
- `trigger` MUST be one of the trigger values; `outcome` MUST be one of the outcome values.
- `guard` MUST be one of the guard values OR null.
- Emit at most 8 trace steps per ticket. If there are more, keep the 8 most substantive.

## Allowed actions
{action_lines}

## Allowed objects
{", ".join(objects)}

## Allowed systems
{", ".join(systems)}

## Allowed triggers
{trigger_lines}

## Allowed guards
{guard_lines}

## Allowed outcomes
{outcome_lines}
"""


# Reuse the exact few-shot INPUT from prompts.py (same 8 tickets) and pair it with a
# hybrid-schema answer, so the model sees the new shape on familiar examples.
GRAPH_FEW_SHOT = [
    FEW_SHOT[0],
    {
        "role": "assistant",
        "content": json.dumps(
            [
                {
                    "id": 0,
                    "trigger": "password_forgotten",
                    "trigger_intent": None,
                    "trace": [
                        {"guard": None, "guard_detail": None,
                         "action": "contact_customer", "object": "user_account", "system": "unknown",
                         "intent": "remote session with the user"},
                        {"guard": None, "guard_detail": None,
                         "action": "reset", "object": "password", "system": "m365_admin", "intent": None},
                        {"guard": "hybrid_account", "guard_detail": "synced account needs both directories",
                         "action": "reset", "object": "password", "system": "active_directory", "intent": None},
                        {"guard": None, "guard_detail": None,
                         "action": "sync", "object": "user_account", "system": "active_directory",
                         "intent": "forced directory sync"},
                        {"guard": None, "guard_detail": None,
                         "action": "inform_customer", "object": "password", "system": "unknown",
                         "intent": "left new password on the laptop"},
                    ],
                    "outcome": "resolved_first_contact",
                    "outcome_intent": None,
                },
                {"id": 1, "trigger": "other", "trigger_intent": None,
                 "trace": [], "outcome": "no_action", "outcome_intent": None},
                {
                    "id": 2,
                    "trigger": "other",
                    "trigger_intent": "backup failing, disk full on D:",
                    "trace": [
                        {"guard": None, "guard_detail": None,
                         "action": "investigate", "object": "disk_space", "system": "on_prem_server", "intent": None},
                        {"guard": None, "guard_detail": None,
                         "action": "clean_up", "object": "disk_space", "system": "on_prem_server",
                         "intent": "cleared VSS shadow copies on D:"},
                        {"guard": None, "guard_detail": None,
                         "action": "run_backup", "object": "backup_job", "system": "backup_tool",
                         "intent": "manually restarted the job"},
                        {"guard": "resolved_after_action", "guard_detail": None,
                         "action": "verify", "object": "backup_job", "system": "backup_tool",
                         "intent": "confirmed the backup succeeded"},
                    ],
                    "outcome": "resolved_first_contact",
                    "outcome_intent": None,
                },
                {
                    "id": 3,
                    "trigger": "other", "trigger_intent": "dark-web credential alert",
                    "trace": [
                        {"guard": None, "guard_detail": None,
                         "action": "triage", "object": "security_alert", "system": "unknown",
                         "intent": "no credentials found"},
                    ],
                    "outcome": "resolved_first_contact", "outcome_intent": None,
                },
                {
                    "id": 4,
                    "trigger": "other", "trigger_intent": "compliance report review",
                    "trace": [
                        {"guard": None, "guard_detail": None,
                         "action": "triage", "object": "compliance_report", "system": "unknown",
                         "intent": "no related incidents"},
                    ],
                    "outcome": "resolved_first_contact", "outcome_intent": None,
                },
                {
                    "id": 5,
                    "trigger": "other", "trigger_intent": None,
                    "trace": [
                        {"guard": None, "guard_detail": None,
                         "action": "resolve_unspecified", "object": "other", "system": "unknown", "intent": None},
                    ],
                    "outcome": "resolved_unspecified", "outcome_intent": None,
                },
                {
                    "id": 6,
                    "trigger": "other", "trigger_intent": "certificate nearing expiry",
                    "trace": [
                        {"guard": None, "guard_detail": None,
                         "action": "renew", "object": "ssl_certificate", "system": "unknown", "intent": None},
                    ],
                    "outcome": "resolved_first_contact", "outcome_intent": None,
                },
                {
                    "id": 7,
                    "trigger": "other", "trigger_intent": "security-awareness onboarding",
                    "trace": [
                        {"guard": None, "guard_detail": None,
                         "action": "conduct_training", "object": "security_training", "system": "knowbe4", "intent": None},
                    ],
                    "outcome": "resolved_first_contact", "outcome_intent": None,
                },
            ],
            ensure_ascii=False,
        ),
    },
]
