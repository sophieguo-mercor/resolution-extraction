"""Prompt construction for resolution-step extraction.

Kept in its own module so you can iterate on wording without touching the
orchestration in extract.py. After editing, re-run:

    python extract.py --sample 20 --workflows "Password Reset" --dry-run
    python extract.py --sample 20 --workflows "Password Reset"

and eyeball the output before launching the full run.
"""
from __future__ import annotations
import json


def build_system_prompt(taxonomy: dict) -> str:
    actions = taxonomy["actions"]
    objects = taxonomy["objects"]
    systems = taxonomy["systems"]

    action_lines = "\n".join(
        f"  {name} ({meta['phase']}) - {meta['desc']}" for name, meta in actions.items()
    )

    return f"""You extract the concrete resolution steps that an IT support agent performed, from helpdesk ticket notes.

The notes are written by Dutch MSP engineers. They are mostly Dutch, sometimes English, often both.
A single ticket's notes may contain several work sessions separated by `---`. Read all of them together:
the resolution is the full set of actions across every session.

## What counts as a step
Only actions the AGENT ACTUALLY COMPLETED. Each distinct action is one step.

## What to IGNORE completely
- Greetings and sign-offs ("Beste Jan", "Met vriendelijke groet", "Graag hoor ik van je")
- Automated acknowledgement text ("Bedankt voor je melding! Uw ticket is geclassificeerd en doorgezet
  naar de juiste afdeling", "Uw ticket is aangemaakt", CAB/Change-Advisory boilerplate)
- Placeholder entries: "-", "Ticket complete", "Ticket update", "See Internal Notes", "Reistijd",
  "Tijdregistratie", "Onsite ondersteuning" with no detail
- Raw email headers (Received:, From:, Subject:, DKIM/SPF blocks)
- What the CUSTOMER asked for, or what the agent merely PROMISED / PLANNED to do later
- Restatements of the same action in a later session (deduplicate: one step per distinct action)

If, after ignoring all of the above, nothing remains, return an empty steps list. That is a valid and
common answer - do NOT invent a step to fill the gap.

## Alerts and reports ARE work
Reviewing a security or monitoring alert (dark-web hit, EDR/XDR/MDR detection, identity-threat alert) or a
SOC/compliance report and dispositioning it - benign, false positive, "legitiem", "geen gerelateerde incidenten",
"geen credentials gevonden" - IS a completed step. Record it as `triage` on `security_alert` (or `compliance_report`).
Do NOT return an empty list for a dispositioned alert. If the agent added an allow-list entry or exclusion so the
alert stops firing ("er staat al een exceptie voor"), use `add_exception`.

## Vague completions
When the notes clearly say the ticket was resolved but give NO detail about what was actually done
("is opgelost", "geregeld", "doorgevoerd", "bij deze in orde", "gelukt"), return a single
`resolve_unspecified` step on `other`. Use this ONLY when a resolution is stated but unspecified - never when
concrete actions are described (extract those as normal), and never for pure boilerplate or acknowledgements
(those stay empty).

## Granularity
Split genuinely different actions apart, even when they serve one goal. Example:
  "Wachtwoord reset uitgevoerd in M365. Wachtwoord reset uitgevoerd in lokaal AD. Sync uitgevoerd.
   Wachtwoord genoteerd op laptop van gebruiker."
  -> reset/password/m365_admin, reset/password/active_directory, sync/user_account/active_directory,
     inform_customer/password/unknown
Do NOT merge those into a single "reset password" - the multi-system work is the interesting part.

## Output schema
Return ONLY a JSON array. One object per input ticket, in the same order, no markdown fences, no prose.

[{{"id": 0, "steps": [{{"action": "...", "object": "...", "system": "..."}}]}}, ...]

- `action` MUST be exactly one of the action names below.
- `object` MUST be exactly one of the object names below.
- `system` MUST be one of the system names below; use "unknown" when the notes don't say.
- Emit at most 8 steps per ticket. If there are more, keep the 8 most substantive.

## Allowed actions
{action_lines}

## Allowed objects
{", ".join(objects)}

## Allowed systems
{", ".join(systems)}
"""


def build_user_message(notes: list[str], char_limit: int = 1500) -> str:
    parts = []
    for i, note in enumerate(notes):
        text = (note or "").strip()
        if len(text) > char_limit:
            text = text[:char_limit] + " …[truncated]"
        parts.append(f"--- TICKET {i} ---\n{text}")
    return "\n\n".join(parts)


FEW_SHOT = [
    {
        "role": "user",
        "content": (
            "--- TICKET 0 ---\n"
            "Meegekeken met gebruiker. Wachtwoord reset uitgevoerd in M365. Wachtwoord reset "
            "uitgevoerd in lokaal AD. Sync uitgevoerd. Wachtwoord is genoteerd op laptop van gebruiker.\n\n"
            "--- TICKET 1 ---\n"
            "Beste Mark, Bedankt voor je melding! Uw ticket is geclassificeerd en doorgezet naar de "
            "juiste afdeling. We pakken het zo snel mogelijk op. --- - --- Ticket complete\n\n"
            "--- TICKET 2 ---\n"
            "- Op de server gekeken en vss opgeschoond op de D schijf. - Handmatig een nieuwe backup "
            "gestart. --- Nagekeken en deze is succesvol verlopen.\n\n"
            "--- TICKET 3 ---\n"
            "Geen credentials gevonden.\n\n"
            "--- TICKET 4 ---\n"
            "Het rapport is beoordeeld. Geen gerelateerde incidenten gevonden.\n\n"
            "--- TICKET 5 ---\n"
            "is opgelost, melding gesloten.\n\n"
            "--- TICKET 6 ---\n"
            "T20260316.0356 - Certificaat verlengen connect.fle-nl.com (04-04-2026)\n\n"
            "--- TICKET 7 ---\n"
            "Onboarding Knowbe4 gesprek --- security awareness training afgerond."
        ),
    },
    {
        "role": "assistant",
        "content": json.dumps(
            [
                {
                    "id": 0,
                    "steps": [
                        {"action": "contact_customer", "object": "user_account", "system": "unknown"},
                        {"action": "reset", "object": "password", "system": "m365_admin"},
                        {"action": "reset", "object": "password", "system": "active_directory"},
                        {"action": "sync", "object": "user_account", "system": "active_directory"},
                        {"action": "inform_customer", "object": "password", "system": "unknown"},
                    ],
                },
                {"id": 1, "steps": []},
                {
                    "id": 2,
                    "steps": [
                        {"action": "investigate", "object": "disk_space", "system": "on_prem_server"},
                        {"action": "clean_up", "object": "disk_space", "system": "on_prem_server"},
                        {"action": "run_backup", "object": "backup_job", "system": "backup_tool"},
                        {"action": "verify", "object": "backup_job", "system": "backup_tool"},
                    ],
                },
                {
                    "id": 3,
                    "steps": [
                        {"action": "triage", "object": "security_alert", "system": "unknown"},
                    ],
                },
                {
                    "id": 4,
                    "steps": [
                        {"action": "triage", "object": "compliance_report", "system": "unknown"},
                    ],
                },
                {
                    "id": 5,
                    "steps": [
                        {"action": "resolve_unspecified", "object": "other", "system": "unknown"},
                    ],
                },
                {
                    "id": 6,
                    "steps": [
                        {"action": "renew", "object": "ssl_certificate", "system": "unknown"},
                    ],
                },
                {
                    "id": 7,
                    "steps": [
                        {"action": "conduct_training", "object": "security_training", "system": "knowbe4"},
                    ],
                },
            ],
            ensure_ascii=False,
        ),
    },
]
