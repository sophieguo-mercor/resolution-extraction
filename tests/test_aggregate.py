"""Smoke test for the aggregation maths.

Builds four synthetic workflows with known properties and asserts the metrics
behave. Runs with pytest or standalone:

    python -m pytest tests/ -q
    python tests/test_aggregate.py
"""
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aggregate import analyse  # noqa: E402

TAXONOMY = json.loads((Path(__file__).resolve().parent.parent / "taxonomy.json").read_text())
PHASE_OF = {a: m["phase"] for a, m in TAXONOMY["actions"].items()}


def rec(steps):
    return {"steps": [{"action": a, "object": o, "system": "unknown"} for a, o in steps]}


def build():
    rng = random.Random(1)

    # 90/100 tickets share one 2-step pattern
    templated = [rec([("reset", "password"), ("inform_customer", "password")]) for _ in range(90)]
    templated += [
        rec([("reset", "password"), ("investigate", "user_account"), ("escalate_internal", "user_account")])
        for _ in range(10)
    ]

    # every ticket a different pattern
    acts = ["investigate", "configure", "install", "restart", "escalate_vendor", "migrate", "replace", "clean_up"]
    objs = ["server", "printer", "vpn", "application", "firewall_rule", "network_switch", "disk_space", "wifi_ap"]
    chaotic = []
    for i in range(100):
        steps = list({(rng.choice(acts), rng.choice(objs)) for _ in range(rng.randint(2, 5))})
        steps.append((acts[i % len(acts)], objs[(i * 3) % len(objs)]))
        chaotic.append(rec(steps))

    # two tight, mutually disjoint sub-populations
    bimodal = [rec([("reset", "mfa_method"), ("verify", "mfa_method")]) for _ in range(50)]
    bimodal += [
        rec([("replace", "device_laptop"), ("install", "application"),
             ("migrate", "user_account"), ("verify", "device_laptop")])
        for _ in range(50)
    ]

    # most tickets yielded no extractable action
    empty = [rec([]) for _ in range(60)] + [rec([("no_action", "other")]) for _ in range(40)]

    return {"templated": templated, "chaotic": chaotic, "bimodal": bimodal, "empty": empty}


def run():
    rng = random.Random(42)
    r = {k: analyse(v, PHASE_OF, rng) for k, v in build().items()}

    # Pattern concentration separates templated from chaotic
    assert r["templated"]["pattern_entropy"] < 0.2, r["templated"]["pattern_entropy"]
    assert r["chaotic"]["pattern_entropy"] > 0.9, r["chaotic"]["pattern_entropy"]
    assert r["templated"]["top3_pct"] == 100.0
    assert r["chaotic"]["top3_pct"] < 10.0

    # Jaccard: alike tickets score high, unlike score low
    assert r["templated"]["mean_jaccard"] > 0.8
    assert r["chaotic"]["mean_jaccard"] < 0.2

    # Bimodality must fire ONLY for the genuinely split workflow.
    # Regression guard: the original definition ("pairs at either extreme")
    # returned ~0.8-1.0 for all three, which was useless.
    assert r["bimodal"]["jaccard_bimodality"] > 0.8, r["bimodal"]["jaccard_bimodality"]
    assert r["templated"]["jaccard_bimodality"] < 0.2, r["templated"]["jaccard_bimodality"]
    assert r["chaotic"]["jaccard_bimodality"] < 0.2, r["chaotic"]["jaccard_bimodality"]

    # No-action tickets are reported, not silently treated as consistency
    assert r["empty"]["pct_no_action"] == 60.0
    assert r["empty"]["phase_mix"]["admin"] == 100.0

    # The empty "(no action extracted)" pattern must NOT be counted as a route.
    # The synthetic "empty" workflow is 60 no-step tickets + 40 on a single real
    # route. If the empty bucket were ranked (the old behaviour) top1_pct would be
    # 60.0; excluding it, the top route is the 40-ticket one and n_patterns is 1.
    assert r["empty"]["top1_pct"] == 40.0, r["empty"]["top1_pct"]
    assert r["empty"]["top3_pct"] == 40.0, r["empty"]["top3_pct"]
    assert r["empty"]["n_patterns"] == 1, r["empty"]["n_patterns"]

    # Phase mix reflects the action types used
    assert r["bimodal"]["phase_mix"]["change"] > 50
    assert r["templated"]["phase_mix"]["coordinate"] > 30

    print("all assertions passed")
    for k, v in r.items():
        print(
            f"  {k:10s} entropy={v['pattern_entropy']:<6} jaccard={v['mean_jaccard']:<6} "
            f"bimodality={v['jaccard_bimodality']:<6} no_action={v['pct_no_action']}%"
        )


def test_aggregate_metrics():
    run()


if __name__ == "__main__":
    run()
