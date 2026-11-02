"""Policy evaluation for FlowGate (backend/app/policy.py)."""

import yaml


def load_policies(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    rules = data.get("rules") or []
    return {"rules": rules, "default_action": data.get("default_action", "deny")}


def _project_relation(run: dict, resource: dict) -> str:
    # Tenant is a hard boundary: a non-shared resource in another
    # tenant is "different" even when the project names match.
    run_tenant = run.get("tenant") or run.get("project")
    resource_tenant = resource.get("tenant") or resource.get("project")
    if resource_tenant and resource_tenant != "shared" and run_tenant != resource_tenant:
        return "different"
    if resource.get("project") == "shared":
        return "shared"
    if run.get("project") == resource.get("project"):
        return "same"
    return "different"


def _clearance_vs_sensitivity(run: dict, resource: dict) -> str:
    if run.get("clearance", 0) < resource.get("sensitivity", 0):
        return "lt"
    return "gte"


def _when_matches(when, operation, project_relation, clearance_vs_sensitivity) -> bool:
    if not when:
        return True
    for key, expected in when.items():
        if key == "operation":
            if isinstance(expected, list):
                if operation not in expected:
                    return False
            elif operation != expected:
                return False
        elif key == "project_relation":
            if project_relation != expected:
                return False
        elif key == "clearance_vs_sensitivity":
            if clearance_vs_sensitivity != expected:
                return False
        else:
            return False
    return True


def _normalise_label(label) -> dict:
    """Normalise a label-ish dict to {classification, compartments}."""
    if not label or not isinstance(label, dict):
        return {"classification": 0, "compartments": []}
    classification = label.get(
        "classification",
        label.get("effective_level", label.get("sensitivity", 0)),
    )
    try:
        classification = int(classification)
    except (TypeError, ValueError):
        classification = 0
    compartments = label.get(
        "compartments", label.get("effective_compartments", [])
    )
    if compartments is None:
        compartments = []
    elif isinstance(compartments, str):
        compartments = [compartments] if compartments else []
    else:
        try:
            compartments = list(compartments)
        except TypeError:
            compartments = []
    return {
        "classification": classification,
        "compartments": sorted({str(c) for c in compartments if c is not None and str(c) != ""}),
    }


def resource_label(resource: dict) -> dict:
    """Label of a resource: classification = sensitivity.

    Compartments are ``[project]`` except for public/shared resources,
    which carry no compartments.
    """
    if not resource or not isinstance(resource, dict):
        return {"classification": 0, "compartments": []}
    try:
        sensitivity = int(resource.get("sensitivity", 0) or 0)
    except (TypeError, ValueError):
        sensitivity = 0
    project = resource.get("project")
    if sensitivity == 0 or project == "shared" or not project:
        compartments = []
    elif resource.get("compartments"):
        compartments = _normalise_label(
            {"classification": sensitivity, "compartments": resource["compartments"]}
        )["compartments"]
    else:
        compartments = [str(project)]
    return {"classification": sensitivity, "compartments": sorted(compartments)}


def run_effective_label(run: dict) -> dict:
    """Effective (taint) label of a run, tolerating missing fields."""
    if not run or not isinstance(run, dict):
        return {"classification": 0, "compartments": []}
    if "effective_level" in run or "effective_compartments" in run:
        return _normalise_label(run)
    if "classification" in run or "compartments" in run:
        return _normalise_label(run)
    return {"classification": 0, "compartments": []}


def join_labels(a: dict, b: dict) -> dict:
    """Lattice join: max classification, union of compartments."""
    left = _normalise_label(a)
    right = _normalise_label(b)
    return {
        "classification": max(left["classification"], right["classification"]),
        "compartments": sorted(
            set(left["compartments"]) | set(right["compartments"])
        ),
    }


def can_flow(source: dict, dest: dict) -> bool:
    """True when data labelled ``source`` may flow to ``dest``."""
    src = _normalise_label(source)
    dst = _normalise_label(dest)
    return src["classification"] <= dst["classification"] and set(
        src["compartments"]
    ).issubset(set(dst["compartments"]))


def _format_label(label: dict) -> str:
    return "classification={} compartments={}".format(
        label.get("classification", 0), label.get("compartments", [])
    )


def evaluate_flow(run: dict, resource: dict) -> dict:
    """Flow check for a write: run effective label -> resource label.

    Allow only when the effective label can flow to the destination
    label; otherwise deny as a write-down (without suspension).
    """
    effective = run_effective_label(run)
    if isinstance(resource, dict) and "classification" in resource and "sensitivity" not in resource:
        destination = _normalise_label(resource)
    else:
        destination = resource_label(resource)
    if can_flow(effective, destination):
        return {
            "action": "allow",
            "policy_id": "flow-allowed",
            "reason": "Effective label {} can flow to destination label {}".format(
                _format_label(effective), _format_label(destination)
            ),
            "effective_label": effective,
            "destination_label": destination,
        }
    return {
        "action": "deny",
        "policy_id": "deny-write-down",
        "reason": (
            "Write-down denied: effective label {} cannot flow to "
            "destination label {}".format(
                _format_label(effective), _format_label(destination)
            )
        ),
        "effective_label": effective,
        "destination_label": destination,
    }


def evaluate_policy(run: dict, resource: dict, operation: str, policies: list) -> dict:
    project_relation = _project_relation(run, resource)
    clearance_vs_sensitivity = _clearance_vs_sensitivity(run, resource)
    for rule in policies or []:
        if _when_matches(
            rule.get("when"), operation, project_relation, clearance_vs_sensitivity
        ):
            return {
                "action": rule.get("action", "deny"),
                "policy_id": rule.get("id", "unknown"),
                "reason": rule.get("reason", ""),
            }
    return {
        "action": "deny",
        "policy_id": "default-deny",
        "reason": "No policy rule matched; default deny",
    }
