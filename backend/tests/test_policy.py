"""Policy evaluator tests (CONTRACT.md Policy semantics).

Written from the contract; app.policy is built in parallel by another agent.
Only test_detection.py is executed by the detection builder.
"""

import textwrap

from app.policy import evaluate_policy, load_policies

STANDARD_RULES = [
    {
        "id": "deny-cross-project",
        "action": "deny",
        "reason": "Cross-project access is not allowed",
        "when": {"project_relation": "different"},
    },
    {
        "id": "deny-insufficient-clearance",
        "action": "deny",
        "reason": "Run clearance is below resource sensitivity",
        "when": {"clearance_vs_sensitivity": "lt"},
    },
    {
        "id": "allow-label-match",
        "action": "allow",
        "reason": "Labels allow this access",
        "when": {},
    },
]


def make_run(project="alpha", clearance=2):
    return {
        "run_id": "run-1",
        "project": project,
        "clearance": clearance,
        "suspended": False,
        "created_at": "2026-01-01T00:00:00",
    }


def make_resource(project="alpha", sensitivity=2, resource_id="res-1"):
    return {
        "resource_id": resource_id,
        "kind": "doc",
        "project": project,
        "sensitivity": sensitivity,
        "content": "content",
    }


# --- project_relation / clearance cases ---


def test_allow_when_labels_match():
    out = evaluate_policy(make_run("alpha", 2), make_resource("alpha", 2), "read", STANDARD_RULES)
    assert out == {
        "action": "allow",
        "policy_id": "allow-label-match",
        "reason": "Labels allow this access",
    }


def test_deny_cross_project_even_with_clearance():
    out = evaluate_policy(make_run("beta", 2), make_resource("alpha", 1), "read", STANDARD_RULES)
    assert out["action"] == "deny"
    assert out["policy_id"] == "deny-cross-project"
    assert out["reason"] == "Cross-project access is not allowed"


def test_deny_insufficient_clearance_same_project():
    out = evaluate_policy(make_run("alpha", 1), make_resource("alpha", 2), "read", STANDARD_RULES)
    assert out["action"] == "deny"
    assert out["policy_id"] == "deny-insufficient-clearance"


def test_clearance_equal_to_sensitivity_allows():
    out = evaluate_policy(make_run("alpha", 1), make_resource("alpha", 1), "read", STANDARD_RULES)
    assert out["action"] == "allow"
    assert out["policy_id"] == "allow-label-match"


def test_shared_resource_allowed_with_clearance():
    out = evaluate_policy(make_run("beta", 1), make_resource("shared", 0), "write", STANDARD_RULES)
    assert out["action"] == "allow"
    assert out["policy_id"] == "allow-label-match"


def test_shared_resource_denied_without_clearance():
    out = evaluate_policy(make_run("beta", 0), make_resource("shared", 1), "read", STANDARD_RULES)
    assert out["action"] == "deny"
    assert out["policy_id"] == "deny-insufficient-clearance"


# --- condition matching ---


def test_operation_condition_string():
    rules = [
        {"id": "deny-write", "action": "deny", "reason": "No writes", "when": {"operation": "write"}},
        {"id": "allow-all", "action": "allow", "reason": "ok", "when": {}},
    ]
    run = make_run("alpha", 2)
    res = make_resource("alpha", 0)
    assert evaluate_policy(run, res, "write", rules)["policy_id"] == "deny-write"
    assert evaluate_policy(run, res, "read", rules)["policy_id"] == "allow-all"


def test_operation_condition_list():
    rules = [
        {
            "id": "deny-rw",
            "action": "deny",
            "reason": "No rw",
            "when": {"operation": ["read", "write"]},
        },
        {"id": "allow-all", "action": "allow", "reason": "ok", "when": {}},
    ]
    run = make_run("alpha", 2)
    res = make_resource("alpha", 0)
    assert evaluate_policy(run, res, "read", rules)["policy_id"] == "deny-rw"
    assert evaluate_policy(run, res, "write", rules)["policy_id"] == "deny-rw"


def test_all_present_keys_must_match():
    rules = [
        {
            "id": "narrow",
            "action": "deny",
            "reason": "narrow",
            "when": {"operation": "write", "project_relation": "different"},
        },
        {"id": "allow-all", "action": "allow", "reason": "ok", "when": {}},
    ]
    # Same project write: operation matches but relation does not -> falls through.
    out = evaluate_policy(make_run("alpha", 2), make_resource("alpha", 0), "write", rules)
    assert out["policy_id"] == "allow-all"
    # Different project write: both match.
    out = evaluate_policy(make_run("beta", 2), make_resource("alpha", 0), "write", rules)
    assert out["policy_id"] == "narrow"
    # Different project read: operation does not match -> falls through.
    out = evaluate_policy(make_run("beta", 2), make_resource("alpha", 0), "read", rules)
    assert out["policy_id"] == "allow-all"


def test_empty_when_matches_everything():
    rules = [{"id": "catch-all", "action": "allow", "reason": "yes", "when": {}}]
    out = evaluate_policy(make_run("beta", 0), make_resource("alpha", 2), "write", rules)
    assert out["policy_id"] == "catch-all"


# --- first match wins / default ---


def test_first_match_wins_over_later_rules():
    rules = [
        {"id": "first", "action": "allow", "reason": "first wins", "when": {}},
        {"id": "second", "action": "deny", "reason": "second loses", "when": {}},
    ]
    out = evaluate_policy(make_run("beta", 0), make_resource("alpha", 2), "read", rules)
    assert out["action"] == "allow"
    assert out["policy_id"] == "first"


def test_rule_order_changes_outcome():
    reordered = [STANDARD_RULES[2], STANDARD_RULES[0], STANDARD_RULES[1]]
    out = evaluate_policy(make_run("beta", 2), make_resource("alpha", 1), "read", reordered)
    assert out["action"] == "allow"
    assert out["policy_id"] == "allow-label-match"


def test_no_match_uses_default_deny():
    out = evaluate_policy(make_run("alpha", 2), make_resource("alpha", 0), "read", [])
    assert out["action"] == "deny"
    assert out["policy_id"] == "default-deny"
    assert isinstance(out["reason"], str)


def test_non_matching_rules_fall_to_default_deny():
    rules = [
        {"id": "write-only", "action": "allow", "reason": "writes", "when": {"operation": "write"}}
    ]
    out = evaluate_policy(make_run("alpha", 2), make_resource("alpha", 0), "read", rules)
    assert out["action"] == "deny"
    assert out["policy_id"] == "default-deny"


def test_deny_and_suspend_action_passes_through():
    rules = [
        {"id": "suspend-all", "action": "deny_and_suspend", "reason": "stop", "when": {}}
    ]
    out = evaluate_policy(make_run("alpha", 2), make_resource("alpha", 0), "read", rules)
    assert out == {"action": "deny_and_suspend", "policy_id": "suspend-all", "reason": "stop"}


# --- load_policies ---


def test_load_policies_reads_yaml(tmp_path):
    path = tmp_path / "policies.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            default_action: deny
            rules:
              - id: deny-cross-project
                action: deny
                reason: "Cross-project access is not allowed"
                when: { project_relation: different }
              - id: deny-insufficient-clearance
                action: deny
                reason: "Run clearance is below resource sensitivity"
                when: { clearance_vs_sensitivity: lt }
              - id: allow-label-match
                action: allow
                reason: "Labels allow this access"
                when: {}
            """
        )
    )
    loaded = load_policies(str(path))
    assert loaded["default_action"] == "deny"
    assert [r["id"] for r in loaded["rules"]] == [
        "deny-cross-project",
        "deny-insufficient-clearance",
        "allow-label-match",
    ]
    out = evaluate_policy(make_run("alpha", 2), make_resource("alpha", 2), "read", loaded["rules"])
    assert out["action"] == "allow"
    assert out["policy_id"] == "allow-label-match"


# --- Tier 1 flow labels (helpers built in parallel in app.policy) ---
#
# Imported inside each test so the evaluator tests above stay collectable
# even before the parallel helpers land.


def make_effective_run(level=None, compartments=None, project="alpha", clearance=2):
    run = make_run(project, clearance)
    if level is not None:
        run["effective_level"] = level
    if compartments is not None:
        run["effective_compartments"] = compartments
    return run


def test_resource_label_uses_sensitivity_and_project_compartment():
    from app.policy import resource_label

    assert resource_label(make_resource("alpha", 2)) == {
        "classification": 2,
        "compartments": ["alpha"],
    }
    assert resource_label(make_resource("beta", 1)) == {
        "classification": 1,
        "compartments": ["beta"],
    }


def test_resource_label_public_has_no_compartments():
    from app.policy import resource_label

    assert resource_label(make_resource("alpha", 0)) == {
        "classification": 0,
        "compartments": [],
    }
    assert resource_label(make_resource("shared", 0)) == {
        "classification": 0,
        "compartments": [],
    }


def test_resource_label_shared_has_no_compartments_even_when_sensitive():
    from app.policy import resource_label

    assert resource_label(make_resource("shared", 2)) == {
        "classification": 2,
        "compartments": [],
    }
    assert resource_label(make_resource("shared", 1)) == {
        "classification": 1,
        "compartments": [],
    }


def test_run_effective_label_reads_effective_fields():
    from app.policy import run_effective_label

    run = make_effective_run(level=2, compartments=["alpha"])
    assert run_effective_label(run) == {"classification": 2, "compartments": ["alpha"]}
    run = make_effective_run(level=1, compartments=["beta"])
    assert run_effective_label(run) == {"classification": 1, "compartments": ["beta"]}


def test_run_effective_label_missing_fields_default_to_public():
    from app.policy import run_effective_label

    # Missing effective_* fields default to 0/[] regardless of the run's
    # static project/clearance.
    assert run_effective_label(make_run("alpha", 2)) == {
        "classification": 0,
        "compartments": [],
    }
    assert run_effective_label(make_effective_run(level=1)) == {
        "classification": 1,
        "compartments": [],
    }
    assert run_effective_label(make_effective_run(compartments=["alpha"])) == {
        "classification": 0,
        "compartments": ["alpha"],
    }


def test_join_labels_takes_max_and_sorted_union():
    from app.policy import join_labels

    assert join_labels(
        {"classification": 1, "compartments": ["beta"]},
        {"classification": 2, "compartments": ["alpha", "beta"]},
    ) == {"classification": 2, "compartments": ["alpha", "beta"]}
    assert join_labels(
        {"classification": 2, "compartments": ["beta"]},
        {"classification": 2, "compartments": ["alpha"]},
    ) == {"classification": 2, "compartments": ["alpha", "beta"]}


def test_join_labels_with_public_and_empty():
    from app.policy import join_labels

    assert join_labels(
        {"classification": 0, "compartments": []},
        {"classification": 0, "compartments": []},
    ) == {"classification": 0, "compartments": []}
    assert join_labels(
        {"classification": 0, "compartments": []},
        {"classification": 1, "compartments": ["alpha"]},
    ) == {"classification": 1, "compartments": ["alpha"]}


def test_can_flow_allows_flow_up_and_equal_with_matching_compartments():
    from app.policy import can_flow

    assert can_flow(
        {"classification": 0, "compartments": []},
        {"classification": 0, "compartments": []},
    )
    assert can_flow(
        {"classification": 0, "compartments": []},
        {"classification": 2, "compartments": ["alpha"]},
    )
    assert can_flow(
        {"classification": 1, "compartments": ["alpha"]},
        {"classification": 2, "compartments": ["alpha"]},
    )
    assert can_flow(
        {"classification": 1, "compartments": ["alpha"]},
        {"classification": 1, "compartments": ["alpha", "beta"]},
    )


def test_can_flow_denies_write_down_and_foreign_compartments():
    from app.policy import can_flow

    # Classification would flow down.
    assert not can_flow(
        {"classification": 2, "compartments": ["alpha"]},
        {"classification": 0, "compartments": []},
    )
    # Compartment not present in the destination.
    assert not can_flow(
        {"classification": 2, "compartments": ["alpha"]},
        {"classification": 2, "compartments": []},
    )
    assert not can_flow(
        {"classification": 1, "compartments": ["beta"]},
        {"classification": 2, "compartments": ["alpha"]},
    )


def test_evaluate_flow_denies_write_down_to_public():
    from app.policy import evaluate_flow

    run = make_effective_run(level=2, compartments=["alpha"])
    out = evaluate_flow(run, make_resource("alpha", 0))
    assert out["action"] == "deny"
    assert out["policy_id"] == "deny-write-down"


def test_evaluate_flow_allows_secret_run_to_matching_secret_resource():
    from app.policy import evaluate_flow

    run = make_effective_run(level=2, compartments=["alpha"])
    out = evaluate_flow(run, make_resource("alpha", 2))
    assert out["action"] == "allow"


def test_evaluate_flow_allows_public_run_to_public_resource():
    from app.policy import evaluate_flow

    run = make_effective_run(level=0, compartments=[])
    out = evaluate_flow(run, make_resource("shared", 0))
    assert out["action"] == "allow"


def test_evaluate_flow_denies_foreign_compartment_despite_classification():
    from app.policy import evaluate_flow

    # Classification 1 <= 2 is fine; the beta compartment may not flow
    # into an alpha compartment.
    run = make_effective_run(level=1, compartments=["beta"], project="beta", clearance=1)
    out = evaluate_flow(run, make_resource("alpha", 2))
    assert out["action"] == "deny"
    assert out["policy_id"] == "deny-write-down"
