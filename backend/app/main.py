"""FlowGate gateway API (backend/app/main.py)."""

import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import seed as seed_module
from seed import seed_data
from .detection import make_fingerprint, scan_write
from app import store
from app.auth import (
    authenticate,
    create_token,
    is_admin,
    resolve_admin_key,
    resolve_demo_mode,
)
from app.models import ArtifactCreate, ChallengeRequest, RunCreate, ToolCallRequest
from app.policy import (
    evaluate_flow,
    evaluate_policy,
    load_policies,
    resource_label,
    run_effective_label,
)
from app.tools import TOOL_KIND, ToolServiceClient, execute_tool

try:  # Tier 4 typed backend errors (added concurrently in app.tools).
    from app.tools import ToolBackendAmbiguous, ToolBackendNotExecuted
except Exception:  # pragma: no cover - defensive fallback
    class ToolBackendNotExecuted(RuntimeError):
        """Execution definitively did not happen on the tool backend."""

    class ToolBackendAmbiguous(RuntimeError):
        """Execution may have committed on the tool backend."""


def _default_db_path() -> str:
    override = os.environ.get("GATEWAY_DB")
    if override:
        return override
    return str(Path(__file__).resolve().parent.parent / "gateway.db")


def _default_policies_path() -> str:
    return str(Path(__file__).resolve().parent.parent / "policies.yaml")


def _ensure_seed_fingerprints(conn) -> None:
    """Seed fingerprints for non-public resources with content.

    Idempotent: a resource is fingerprinted only when no existing
    fingerprint has the same source_resource_id and hash.
    """
    existing = {
        (fp.get("source_resource_id"), fp.get("hash"))
        for fp in store.list_fingerprints(conn)
    }
    for resource in store.list_resources(conn):
        sensitivity = resource.get("sensitivity") or 0
        content = resource.get("content") or ""
        if sensitivity <= 0 or not content:
            continue
        fingerprint = make_fingerprint(content)
        key = (resource["resource_id"], fingerprint.get("hash"))
        if key in existing:
            continue
        store.insert_fingerprint(
            conn,
            resource["resource_id"],
            resource["project"],
            resource["sensitivity"],
            "seed",
            fingerprint,
        )
        existing.add(key)


def _tool_backend_exc_types():
    """Resolve the typed backend-error classes, preferring app.tools."""
    try:
        from app.tools import (
            ToolBackendAmbiguous as _Ambiguous,
            ToolBackendNotExecuted as _NotExecuted,
        )

        return _NotExecuted, _Ambiguous
    except Exception:
        return ToolBackendNotExecuted, ToolBackendAmbiguous


def _normalise_backend_receipt(raw):
    """Normalise a backend receipt lookup result to a dict or None."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return {"tool_receipt_id": raw, "receipt_id": raw}
    if isinstance(raw, (list, tuple)):
        if not raw:
            return None
        return _normalise_backend_receipt(raw[0])
    if isinstance(raw, dict):
        if isinstance(raw.get("receipts"), (list, tuple)):
            receipts = raw["receipts"]
            if not receipts:
                return None
            return _normalise_backend_receipt(receipts[0])
        if "receipt" in raw:
            return _normalise_backend_receipt(raw.get("receipt"))
        if raw.get("count") == 0 and not raw.get("receipt_id"):
            return None
        receipt_id = raw.get(
            "receipt_id", raw.get("tool_receipt_id", raw.get("id"))
        )
        if receipt_id is None and not raw:
            return None
        normalised = dict(raw)
        normalised["tool_receipt_id"] = receipt_id
        return normalised
    return {"tool_receipt_id": raw}


def _receipt_is_pending(receipt) -> bool:
    """A pending (two-phase, not yet committed) receipt is not proof."""
    return (
        isinstance(receipt, dict)
        and str(receipt.get("state") or "").lower() == "pending"
    )


def _receipt_binding_mismatch(receipt, intent) -> bool:
    """True when a backend receipt's binding differs from the intent.

    Only fields actually present on the receipt are checked: receipts
    without a state/binding field (legacy tool-service rows) pass, but a
    receipt recorded for a different run/resource/payload under the same
    request_id must never reconcile this intent.
    """
    if not isinstance(receipt, dict) or not isinstance(intent, dict):
        return False
    for field in ("run_id", "resource_id", "payload_hash", "operation", "request_id"):
        expected = intent.get(field)
        if expected is None:
            continue
        actual = receipt.get(field)
        if actual is not None and str(actual) != str(expected):
            return True
    return False


def _is_request_id_reuse_error(exc) -> bool:
    """True for the backend 409 request-id binding error.

    The shipped client raises exactly the base ``ToolBackendError`` for
    409 (see test_backend_classification); test adapters raise a plain
    ``RuntimeError`` carrying the same ``request-id-reuse`` marker. Both
    are definitive non-executions for the attempted payload, never an
    ambiguous outcome.
    """
    if exc is None:
        return False
    if "request-id-reuse" in str(exc).lower():
        return True
    try:
        from app.tools import ToolBackendError as _BaseError
    except Exception:
        return False
    return type(exc) is _BaseError


def _backend_receipt_for_request(tool_client, request_id):
    """Look up a backend receipt by request_id.

    Returns ``(receipt_dict_or_None, lookup_available)``. ``lookup_available``
    is False when the client offers no receipt-by-request lookup or the
    lookup itself fails — in that case the caller must not redispatch.
    """
    receipt_fn = getattr(tool_client, "receipt_for_request", None)
    if callable(receipt_fn):
        try:
            try:
                raw = receipt_fn(request_id)
            except TypeError:
                raw = receipt_fn(request_id=request_id)
        except Exception:
            return None, False
        return _normalise_backend_receipt(raw), True
    receipts_fn = getattr(tool_client, "receipts_for_request", None)
    if callable(receipts_fn):
        try:
            try:
                raw = receipts_fn(request_id)
            except TypeError:
                raw = receipts_fn(request_id=request_id)
        except Exception:
            return None, False
        return _normalise_backend_receipt(raw), True
    return None, False


def _dispatch_external(tool_client, run_id, resource_id, text, rid, payload_hash):
    """Dispatch an external write, classifying the outcome.

    Returns ``(kind, result, tool_receipt_id, error)`` where ``kind`` is
    ``"success"``, ``"definitive"`` (execution is known not to have
    happened) or ``"ambiguous"`` (execution may have committed). Legacy
    clients without ``request_id``/``payload_hash`` kwargs are retried
    without them on ``TypeError``. Plain ``RuntimeError`` is treated as
    ambiguous (the safe default); other exceptions propagate.
    """
    not_executed_cls, ambiguous_cls = _tool_backend_exc_types()
    try:
        try:
            raw = tool_client.execute(
                run_id, "write", resource_id, text,
                request_id=rid, payload_hash=payload_hash,
            )
        except TypeError:
            raw = tool_client.execute(run_id, "write", resource_id, text)
    except not_executed_cls as exc:
        return "definitive", None, None, exc
    except ambiguous_cls as exc:
        return "ambiguous", None, None, exc
    except RuntimeError as exc:
        if _is_request_id_reuse_error(exc):
            return "reuse", None, None, exc
        return "ambiguous", None, None, exc
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        return "success", raw[0], raw[1], None
    if isinstance(raw, dict):
        return (
            "success",
            raw.get("result"),
            raw.get("receipt_id", raw.get("tool_receipt_id")),
            None,
        )
    return "success", raw, None, None


def _handle_tool_call(
    conn, rules, run, tool_name, operation, resource_id, content,
    tool_client=None, external_resources=frozenset(), request_id=None,
):
    """Shared tool-call logic for the HTTP endpoint and /demo/run.

    Returns (status_code, body). Assumes the run is already authenticated.
    Resources in ``external_resources`` execute on the separately
    protected tool backend via ``tool_client`` (after all gateway
    checks pass — a blocked call never reaches the backend).
    """
    external = tool_client is not None and resource_id in external_resources
    run_id = run["run_id"]

    # Suspended runs are rejected before policy evaluation.
    if run.get("suspended"):
        reason = "Run is suspended"
        event_id = store.insert_event(
            conn, run_id, tool_name, operation, resource_id,
            "blocked", "deny", "run-suspended", reason,
        )
        return 403, {
            "decision": "blocked",
            "reason": reason,
            "policy_id": "run-suspended",
            "event_id": event_id,
        }

    if tool_name not in TOOL_KIND:
        reason = "Unknown tool: {}".format(tool_name)
        event_id = store.insert_event(
            conn, run_id, tool_name, operation, resource_id,
            "blocked", "deny", "unknown-tool", reason,
        )
        return 403, {
            "decision": "blocked",
            "reason": reason,
            "policy_id": "unknown-tool",
            "event_id": event_id,
        }

    resource = store.get_resource(conn, resource_id)
    if resource is None:
        reason = "Resource not found: {}".format(resource_id)
        event_id = store.insert_event(
            conn, run_id, tool_name, operation, resource_id,
            "blocked", "deny", "resource-not-found", reason,
        )
        return 403, {
            "decision": "blocked",
            "reason": reason,
            "policy_id": "resource-not-found",
            "event_id": event_id,
        }

    if TOOL_KIND[tool_name] != resource["kind"]:
        reason = "Tool {} cannot access resource {} of kind {}".format(
            tool_name, resource_id, resource["kind"]
        )
        event_id = store.insert_event(
            conn, run_id, tool_name, operation, resource_id,
            "blocked", "deny", "tool-resource-mismatch", reason,
        )
        return 403, {
            "decision": "blocked",
            "reason": reason,
            "policy_id": "tool-resource-mismatch",
            "event_id": event_id,
        }

    verdict = evaluate_policy(run, resource, operation, rules)
    if verdict["action"] != "allow":
        try:
            if verdict["action"] == "deny_and_suspend":
                store.set_suspended(conn, run_id, True, commit=False)
            event_id = store.insert_event(
                conn, run_id, tool_name, operation, resource_id,
                "blocked", verdict["action"], verdict["policy_id"], verdict["reason"],
                commit=False,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return 403, {
            "decision": "blocked",
            "reason": verdict["reason"],
            "policy_id": verdict["policy_id"],
            "event_id": event_id,
        }

    if operation == "read":
        # Compartment entitlement: a run may read only resources whose
        # compartments it is entitled to — its own project plus any
        # compartments the controller asserted at run creation (the
        # run's initial effective compartments). Taint gained later
        # cannot retroactively authorise a first disclosure, so this
        # check uses entitlement, not the joined read history alone:
        # entitlement is project + effective compartments as they stand
        # *before* this read joins anything new.
        dest_label = resource_label(resource)
        entitled = {run["project"]} | set(
            run.get("effective_compartments") or []
        )
        missing = sorted(set(dest_label["compartments"]) - entitled)
        if missing:
            reason = (
                "Run is not entitled to compartments {} required by "
                "resource {}".format(missing, resource_id)
            )
            event_id = store.insert_event(
                conn, run_id, tool_name, operation, resource_id,
                "blocked", "deny", "deny-compartment", reason,
            )
            return 403, {
                "decision": "blocked",
                "reason": reason,
                "policy_id": "deny-compartment",
                "event_id": event_id,
            }

    if operation == "write":
        flow = evaluate_flow(run, resource)
        if flow["action"] != "allow":
            effective_label = flow.get("effective_label") or run_effective_label(run)
            destination_label = flow.get("destination_label") or resource_label(resource)
            event_id = store.insert_event(
                conn, run_id, tool_name, operation, resource_id,
                "blocked", "deny", flow["policy_id"], flow["reason"],
            )
            return 403, {
                "decision": "blocked",
                "reason": flow["reason"],
                "policy_id": flow["policy_id"],
                "event_id": event_id,
                "effective_label": effective_label,
                "destination_label": destination_label,
            }
        text = content if content is not None else ""
        scan = scan_write(
            text,
            store.list_fingerprints(conn),
            run["project"],
            resource["sensitivity"],
        )
        if scan["action"] != "allow":
            try:
                if scan["action"] == "deny_and_suspend":
                    store.set_suspended(conn, run_id, True, commit=False)
                event_id = store.insert_event(
                    conn, run_id, tool_name, operation, resource_id,
                    "blocked", scan["action"], scan["policy_id"], scan["reason"],
                    similarity=scan.get("similarity"), commit=False,
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            return 403, {
                "decision": "blocked",
                "reason": scan["reason"],
                "policy_id": scan["policy_id"],
                "event_id": event_id,
                "similarity": scan.get("similarity"),
            }
        # Externalised resources execute on the protected tool backend
        # (only after every gateway check above has passed). Tier 4: a
        # local transaction cannot span the service boundary, so external
        # writes use a durable execution-intent state machine instead of
        # claiming atomicity — the outcome is committed, definitively
        # failed, or honestly "unknown" (never reported as blocked).
        tool_receipt_id = None
        if external:
            payload_hash = make_fingerprint(text).get("hash")
            rid = request_id or ("req-" + uuid.uuid4().hex)

            def _complete_local(completion_tool_receipt_id):
                """Local version+receipt+event+intent commit, one txn."""
                execute_tool(
                    conn, tool_name, operation, resource, text, commit=False
                )
                new_version_inner = store.append_artifact_version(
                    conn, resource_id, text, run_id,
                    content_hash=payload_hash, commit=False, force=True,
                )
                gateway_receipt_inner = store.insert_execution_receipt(
                    conn, run_id, tool_name, operation, resource_id,
                    version=(
                        new_version_inner
                        if new_version_inner is not None
                        else resource.get("version")
                    ),
                    commit=False,
                )
                event_inner = store.insert_event(
                    conn, run_id, tool_name, operation, resource_id,
                    "allowed", "allow", scan["policy_id"], scan["reason"],
                    similarity=scan.get("similarity"), commit=False,
                )
                store.update_execution_intent(
                    conn, rid, state="committed",
                    tool_receipt_id=completion_tool_receipt_id,
                    gateway_receipt_id=gateway_receipt_inner,
                    event_id=event_inner, commit=False,
                )
                conn.commit()
                return gateway_receipt_inner, event_inner, new_version_inner

            existing_intent = store.get_execution_intent(conn, rid)
            if existing_intent is not None:
                binding_mismatch = (
                    existing_intent.get("run_id") != run_id
                    or existing_intent.get("resource_id") != resource_id
                    or existing_intent.get("payload_hash") != payload_hash
                    or (
                        existing_intent.get("tool") is not None
                        and existing_intent.get("tool") != tool_name
                    )
                    or (
                        existing_intent.get("operation") is not None
                        and existing_intent.get("operation") != operation
                    )
                )
                if binding_mismatch:
                    reuse_reason = (
                        "request_id was already used with a different run, "
                        "resource, or payload"
                    )
                    event_id = store.insert_event(
                        conn, run_id, tool_name, operation, resource_id,
                        "blocked", "deny", "request-id-reuse", reuse_reason,
                    )
                    return 409, {
                        "decision": "blocked",
                        "reason": reuse_reason,
                        "policy_id": "request-id-reuse",
                        "request_id": rid,
                        "event_id": event_id,
                    }
                if existing_intent.get("state") == "committed":
                    return 200, {
                        "decision": "allowed",
                        "reason": "Idempotent replay of committed execution",
                        "policy_id": (
                            existing_intent.get("policy_id")
                            or scan["policy_id"]
                        ),
                        "event_id": existing_intent.get("event_id"),
                        "execution_receipt_id": existing_intent.get(
                            "gateway_receipt_id"
                        ),
                        "tool_receipt_id": existing_intent.get(
                            "tool_receipt_id"
                        ),
                        "request_id": rid,
                        "replayed": True,
                        "result": None,
                    }
                # unknown / dispatched / authorized_intent / failed (or any
                # other non-committed state): reconcile before retrying.
                receipt_info, lookup_ok = _backend_receipt_for_request(
                    tool_client, rid
                )
                if not lookup_ok:
                    try:
                        store.update_execution_intent(
                            conn, rid, state="unknown",
                            error="Backend receipt lookup unavailable",
                        )
                    except Exception:
                        try:
                            conn.rollback()
                        except Exception:
                            pass
                    return 503, {
                        "decision": "unknown",
                        "reason": (
                            "Execution outcome unknown: the backend receipt "
                            "lookup is unavailable; retry with the same "
                            "request_id to reconcile"
                        ),
                        "policy_id": "execution-outcome-unknown",
                        "request_id": rid,
                        "tool_receipt_id": existing_intent.get(
                            "tool_receipt_id"
                        ),
                        "event_id": None,
                    }
                if receipt_info is not None and _receipt_binding_mismatch(
                    receipt_info, existing_intent
                ):
                    # Backend holds this request_id for a different
                    # binding: it never executed *this* payload. Reject
                    # as reuse; never fabricate local execution evidence
                    # from a foreign receipt.
                    reuse_reason = (
                        "request_id was already used with a different run, "
                        "resource, or payload"
                    )
                    try:
                        store.update_execution_intent(
                            conn, rid, state="failed", error=reuse_reason
                        )
                    except Exception:
                        try:
                            conn.rollback()
                        except Exception:
                            pass
                    event_id = store.insert_event(
                        conn, run_id, tool_name, operation, resource_id,
                        "blocked", "deny", "request-id-reuse", reuse_reason,
                    )
                    return 409, {
                        "decision": "blocked",
                        "reason": reuse_reason,
                        "policy_id": "request-id-reuse",
                        "request_id": rid,
                        "event_id": event_id,
                    }
                if receipt_info is not None and _receipt_is_pending(receipt_info):
                    # Pending is not proof of execution (file-docs
                    # two-phase protocol): fall through to redispatch
                    # under the same request_id, which promotes/completes
                    # the backend write exactly once.
                    receipt_info = None
                if receipt_info is not None:
                    reconciled_tool_receipt_id = (
                        receipt_info.get("tool_receipt_id")
                        if isinstance(receipt_info, dict)
                        else receipt_info
                    )
                    try:
                        receipt_id, event_id, new_version = _complete_local(
                            reconciled_tool_receipt_id
                        )
                    except Exception as exc:
                        conn.rollback()
                        try:
                            store.update_execution_intent(
                                conn, rid, state="unknown",
                                tool_receipt_id=reconciled_tool_receipt_id,
                                error=str(exc),
                            )
                        except Exception:
                            try:
                                conn.rollback()
                            except Exception:
                                pass
                        return 500, {
                            "decision": "unknown",
                            "reason": (
                                "Backend execution committed but gateway "
                                "audit failed; retry with the same "
                                "request_id to reconcile"
                            ),
                            "policy_id": "execution-outcome-unknown",
                            "request_id": rid,
                            "tool_receipt_id": reconciled_tool_receipt_id,
                            "event_id": None,
                        }
                    return 200, {
                        "decision": "allowed",
                        "reason": scan["reason"],
                        "policy_id": (
                            existing_intent.get("policy_id")
                            or scan["policy_id"]
                        ),
                        "event_id": event_id,
                        "execution_receipt_id": receipt_id,
                        "tool_receipt_id": reconciled_tool_receipt_id,
                        "request_id": rid,
                        "reconciled": True,
                        "version": (
                            new_version
                            if new_version is not None
                            else resource.get("version")
                        ),
                        "result": None,
                    }
                # No backend receipt and the lookup succeeded: the write
                # never executed there, so (re)dispatch under the same rid.
                store.update_execution_intent(
                    conn, rid, state="authorized_intent"
                )
            else:
                store.create_execution_intent(
                    conn, rid, run_id, tool_name, operation, resource_id,
                    payload_hash, policy_id=scan["policy_id"],
                )

            dispatch_kind, _svc_result, dispatched_tool_receipt_id, dispatch_exc = (
                _dispatch_external(
                    tool_client, run_id, resource_id, text, rid, payload_hash
                )
            )
            if dispatch_kind == "definitive":
                store.update_execution_intent(
                    conn, rid, state="failed", error=str(dispatch_exc)
                )
                event_id = store.insert_event(
                    conn, run_id, tool_name, operation, resource_id,
                    "blocked", "deny", "tool-backend-unavailable",
                    str(dispatch_exc),
                )
                return 503, {
                    "decision": "blocked",
                    "reason": str(dispatch_exc),
                    "policy_id": "tool-backend-unavailable",
                    "event_id": event_id,
                    "request_id": rid,
                }
            if dispatch_kind == "reuse":
                reuse_reason = (
                    "request_id was already used with a different run, "
                    "resource, or payload"
                )
                store.update_execution_intent(
                    conn, rid, state="failed", error=str(dispatch_exc)
                )
                event_id = store.insert_event(
                    conn, run_id, tool_name, operation, resource_id,
                    "blocked", "deny", "request-id-reuse", reuse_reason,
                )
                return 409, {
                    "decision": "blocked",
                    "reason": reuse_reason,
                    "policy_id": "request-id-reuse",
                    "event_id": event_id,
                    "request_id": rid,
                }
            if dispatch_kind == "ambiguous":
                try:
                    store.update_execution_intent(
                        conn, rid, state="unknown", error=str(dispatch_exc)
                    )
                except Exception:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                unknown_reason = (
                    "Execution outcome unknown: the tool backend may have "
                    "committed the write; retry with the same request_id "
                    "to reconcile"
                )
                unknown_event_id = None
                try:
                    unknown_event_id = store.insert_event(
                        conn, run_id, tool_name, operation, resource_id,
                        "unknown", "unknown", "execution-outcome-unknown",
                        unknown_reason,
                    )
                except Exception:
                    unknown_event_id = None
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                return 503, {
                    "decision": "unknown",
                    "reason": unknown_reason,
                    "policy_id": "execution-outcome-unknown",
                    "request_id": rid,
                    "event_id": unknown_event_id,
                }
            # Dispatch succeeded: mark dispatched durably, then complete
            # the local transaction (extended with the intent commit).
            tool_receipt_id = dispatched_tool_receipt_id
            store.update_execution_intent(
                conn, rid, state="dispatched",
                tool_receipt_id=tool_receipt_id,
            )
            try:
                receipt_id, event_id, new_version = _complete_local(
                    tool_receipt_id
                )
            except Exception as exc:
                conn.rollback()
                try:
                    store.update_execution_intent(
                        conn, rid, state="unknown",
                        tool_receipt_id=tool_receipt_id, error=str(exc),
                    )
                except Exception:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                return 500, {
                    "decision": "unknown",
                    "reason": (
                        "Backend execution committed but gateway audit "
                        "failed; retry with the same request_id to reconcile"
                    ),
                    "policy_id": "execution-outcome-unknown",
                    "request_id": rid,
                    "tool_receipt_id": tool_receipt_id,
                    "event_id": None,
                }
            return 200, {
                "decision": "allowed",
                "reason": scan["reason"],
                "policy_id": scan["policy_id"],
                "event_id": event_id,
                "execution_receipt_id": receipt_id,
                "tool_receipt_id": tool_receipt_id,
                "request_id": rid,
                "version": (
                    new_version
                    if new_version is not None
                    else resource.get("version")
                ),
                "result": None,
            }
        # Execute + version + receipt + audit event commit atomically:
        # a failure anywhere rolls the resource mutation back, so a
        # write can never succeed silently without its event.
        try:
            execute_tool(conn, tool_name, operation, resource, text, commit=False)
            new_version = store.append_artifact_version(
                conn, resource_id, text, run_id,
                content_hash=make_fingerprint(text).get("hash"), commit=False,
            )
            receipt_id = store.insert_execution_receipt(
                conn, run_id, tool_name, operation, resource_id,
                version=new_version if new_version is not None else resource.get("version"),
                commit=False,
            )
            event_id = store.insert_event(
                conn, run_id, tool_name, operation, resource_id,
                "allowed", "allow", scan["policy_id"], scan["reason"],
                similarity=scan.get("similarity"), commit=False,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return 200, {
            "decision": "allowed",
            "reason": scan["reason"],
            "policy_id": scan["policy_id"],
            "event_id": event_id,
            "execution_receipt_id": receipt_id,
            "tool_receipt_id": tool_receipt_id,
            "version": new_version if new_version is not None else resource.get("version"),
            "result": None,
        }

    # Reads: externalised resources come from the tool backend (the
    # system of record); everything else from the gateway store.
    tool_receipt_id = None
    if external:
        try:
            result, tool_receipt_id = tool_client.execute(
                run_id, "read", resource_id, None
            )
        except RuntimeError as exc:
            event_id = store.insert_event(
                conn, run_id, tool_name, operation, resource_id,
                "blocked", "deny", "tool-backend-unavailable", str(exc),
            )
            return 503, {
                "decision": "blocked",
                "reason": str(exc),
                "policy_id": "tool-backend-unavailable",
                "event_id": event_id,
            }
    else:
        result = execute_tool(conn, tool_name, operation, resource, None)
    fingerprint = make_fingerprint(result if result is not None else "")
    destination = resource_label(resource)
    # Fingerprint + effective-label join + receipt + event commit
    # atomically so a read never taints (or is audited) partially.
    try:
        store.insert_fingerprint(
            conn,
            resource["resource_id"],
            resource["project"],
            resource["sensitivity"],
            run_id,
            fingerprint,
            commit=False,
        )
        updated_run = store.update_run_effective_label(
            conn, run_id, destination["classification"], destination["compartments"],
            commit=False,
        )
        receipt_id = store.insert_execution_receipt(
            conn, run_id, tool_name, operation, resource_id,
            version=resource.get("version"), commit=False,
        )
        event_id = store.insert_event(
            conn, run_id, tool_name, operation, resource_id,
            "allowed", "allow", verdict["policy_id"], verdict["reason"],
            content_hash=fingerprint.get("hash"), commit=False,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    if updated_run is not None:
        effective_level = updated_run.get("effective_level", 0)
        effective_compartments = updated_run.get("effective_compartments", [])
    else:
        joined = run_effective_label(run)
        effective_level = max(
            int(joined.get("classification", 0) or 0),
            int(destination.get("classification", 0) or 0),
        )
        effective_compartments = sorted(
            set(joined.get("compartments", []))
            | set(destination.get("compartments", []))
        )
    return 200, {
        "decision": "allowed",
        "reason": verdict["reason"],
        "policy_id": verdict["policy_id"],
        "event_id": event_id,
        "execution_receipt_id": receipt_id,
        "tool_receipt_id": tool_receipt_id,
        "result": result,
        "effective_level": effective_level,
        "effective_compartments": effective_compartments,
    }


def _seed_if_empty(conn) -> None:
    """Seed fixtures only into a database with no resources yet.

    Normal startup must preserve existing resource contents; explicit
    demo reset is the only path that reseeds over existing state.
    """
    if not store.list_resources(conn):
        seed_data(conn)
    _ensure_seed_fingerprints(conn)


def _create_artifact(conn, run, resource_id, kind, content):
    """Create a versioned artifact with a server-assigned inherited label.

    The label is the creator run's current effective label; the client
    never chooses project/sensitivity/compartments. Returns (status, body).
    """
    run_id = run["run_id"]
    if run.get("suspended"):
        event_id = store.insert_event(
            conn, run_id, None, "create", resource_id,
            "blocked", "deny", "run-suspended", "Run is suspended",
        )
        return 403, {
            "decision": "blocked", "reason": "Run is suspended",
            "policy_id": "run-suspended", "event_id": event_id,
        }
    if store.get_resource(conn, resource_id) is not None:
        event_id = store.insert_event(
            conn, run_id, None, "create", resource_id,
            "blocked", "deny", "artifact-exists",
            "Resource already exists: {}".format(resource_id),
        )
        return 409, {
            "decision": "blocked",
            "reason": "Resource already exists: {}".format(resource_id),
            "policy_id": "artifact-exists", "event_id": event_id,
        }
    effective = run_effective_label(run)
    classification = int(effective.get("classification", 0) or 0)
    compartments = list(effective.get("compartments") or []) if classification else []
    if not compartments:
        project = "shared"
    elif len(compartments) == 1:
        project = compartments[0]
    elif run.get("project") in compartments:
        project = run["project"]
    else:
        project = compartments[0]
    text = content if content is not None else ""
    # Creation is a write into the gateway: run the same detection
    # backstop as tool writes, against the inherited label as target.
    # Without this, a fresh run could stash a known restricted copy in
    # a public artifact and anyone could read it back.
    scan = scan_write(
        text, store.list_fingerprints(conn), run["project"], classification
    )
    if scan["action"] != "allow":
        try:
            if scan["action"] == "deny_and_suspend":
                store.set_suspended(conn, run_id, True, commit=False)
            event_id = store.insert_event(
                conn, run_id, None, "create", resource_id,
                "blocked", scan["action"], scan["policy_id"], scan["reason"],
                similarity=scan.get("similarity"), commit=False,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return 403, {
            "decision": "blocked",
            "reason": scan["reason"],
            "policy_id": scan["policy_id"],
            "event_id": event_id,
            "similarity": scan.get("similarity"),
        }
    fingerprint = make_fingerprint(text)
    tenant = run.get("tenant") or run.get("project")
    try:
        resource = store.create_artifact(
            conn, resource_id, kind, project, classification, compartments,
            text, run_id, content_hash=fingerprint.get("hash"), commit=False,
            tenant=tenant,
        )
        store.insert_fingerprint(
            conn, resource_id, project, classification, run_id,
            fingerprint, commit=False,
        )
        event_id = store.insert_event(
            conn, run_id, None, "create", resource_id,
            "allowed", "allow", "artifact-created",
            "Artifact created with inherited label classification={} compartments={}".format(
                classification, compartments
            ),
            content_hash=fingerprint.get("hash"), commit=False,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return 201, {
        "decision": "allowed",
        "policy_id": "artifact-created",
        "event_id": event_id,
        "resource_id": resource_id,
        "kind": kind,
        "project": project,
        "tenant": tenant,
        "sensitivity": classification,
        "compartments": compartments,
        "version": 1,
        "created_by": run_id,
        "label": {"classification": classification, "compartments": compartments},
    }


def _make_leak_text(roadmap_content: str) -> str:
    """Lightly edit roadmap content into a leak summary (keeps canary/overlap)."""
    leak = roadmap_content or ""
    for old, new in (
        ("launch", "release"),
        ("budget", "funding"),
        ("timeline", "schedule"),
        ("milestone", "checkpoint"),
    ):
        leak = leak.replace(old, new)
    return "Summary for sharing: " + leak + " Please distribute this widely."


def _clean_note() -> str:
    return (
        "Beta team update: the office plants need watering on Friday afternoon. "
        "Please bring snacks and board games for the kitchen picnic party."
    )


def create_app(
    db_path: Optional[str] = None,
    policies_path: Optional[str] = None,
    admin_key: Optional[str] = None,
    demo_mode: Optional[bool] = None,
    tool_service_url: Optional[str] = None,
    tool_service_key: Optional[str] = None,
    tool_client=None,
    external_resources=None,
) -> FastAPI:
    resolved_db = str(db_path) if db_path is not None else _default_db_path()
    resolved_policies = (
        str(policies_path) if policies_path is not None else _default_policies_path()
    )
    resolved_demo_mode = resolve_demo_mode(demo_mode)
    resolved_admin_key = resolve_admin_key(admin_key, demo_mode=resolved_demo_mode)
    resolved_tool_client = tool_client
    if resolved_tool_client is None:
        url = tool_service_url or os.environ.get("FLOWGATE_TOOL_SERVICE_URL")
        if url:
            key = (
                tool_service_key
                or os.environ.get("FLOWGATE_TOOL_SERVICE_KEY")
                or ""
            )
            resolved_tool_client = ToolServiceClient(url, key)
    if external_resources is not None:
        resolved_external = set(external_resources)
    elif os.environ.get("FLOWGATE_EXTERNAL_RESOURCES") is not None:
        resolved_external = {
            part.strip()
            for part in os.environ["FLOWGATE_EXTERNAL_RESOURCES"].split(",")
            if part.strip()
        }
    else:
        resolved_external = (
            {"public-outbox"} if resolved_tool_client is not None else set()
        )

    application = FastAPI(title="FlowGate")

    @application.exception_handler(RequestValidationError)
    async def _validation_error_handler(request: Request, exc: RequestValidationError):
        # Never reflect submitted input back: a rejected challenge payload
        # may itself carry a canary or other restricted text (Tier 3).
        detail = [
            {"loc": e.get("loc"), "msg": e.get("msg"), "type": e.get("type")}
            for e in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": detail})

    application.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    conn = store.init_db(resolved_db)
    _seed_if_empty(conn)
    loaded = load_policies(resolved_policies)
    application.state.db_path = resolved_db
    application.state.policies_path = resolved_policies
    application.state.conn = conn
    application.state.policies = loaded
    application.state.lock = threading.Lock()
    application.state.admin_key = resolved_admin_key
    application.state.demo_mode = resolved_demo_mode
    application.state.tool_client = resolved_tool_client
    application.state.external_resources = resolved_external
    application.state.challenge_attempts = []
    state = application.state

    def _admin_denied() -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content={
                "decision": "blocked",
                "reason": "Admin key required",
                "policy_id": "admin-required",
                "event_id": None,
            },
        )

    def _is_admin(authorization, x_admin_key) -> bool:
        return is_admin(
            authorization=authorization,
            x_admin_key=x_admin_key,
            admin_key=state.admin_key,
        )

    @application.on_event("startup")
    def _startup() -> None:
        with state.lock:
            _seed_if_empty(state.conn)

    @application.get("/health")
    def health():
        return {"status": "ok"}

    @application.post("/runs", status_code=201)
    def create_run_endpoint(
        body: RunCreate,
        authorization: Optional[str] = Header(None),
        x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key"),
    ):
        with state.lock:
            if not _is_admin(authorization, x_admin_key):
                return _admin_denied()
            project = body.project
            clearance = body.clearance
            tenant = body.tenant
            effective_level = body.effective_level
            effective_compartments = body.effective_compartments
            if body.parent_run_id:
                if not body.spawn_mode:
                    raise HTTPException(
                        status_code=422,
                        detail="spawn_mode is required when parent_run_id is provided",
                    )
                parent = store.get_run(state.conn, body.parent_run_id)
                if parent is None:
                    raise HTTPException(
                        status_code=422, detail="Unknown parent run"
                    )
                if body.spawn_mode == "continuing":
                    # Server wins for continuing spawns: the child inherits
                    # the parent's current effective label and its
                    # project/tenant/clearance, ignoring any client-supplied
                    # project, tenant, clearance, or effective label values,
                    # so a fresh token cannot launder provenance.
                    effective_level = parent.get("effective_level", 0)
                    effective_compartments = parent.get(
                        "effective_compartments", []
                    )
                    project = parent.get("project")
                    tenant = parent.get("tenant") or parent.get("project")
                    clearance = parent.get("clearance")
                elif body.spawn_mode == "clean":
                    effective_level = 0
                    effective_compartments = []
            elif body.spawn_mode:
                raise HTTPException(
                    status_code=422,
                    detail="parent_run_id is required when spawn_mode is provided",
                )
            token = create_token()
            return store.create_run(
                state.conn,
                project,
                clearance,
                token,
                effective_level=effective_level,
                effective_compartments=effective_compartments,
                tenant=tenant,
                parent_run_id=body.parent_run_id,
                spawn_mode=body.spawn_mode,
            )

    @application.get("/runs")
    def list_runs_endpoint(
        authorization: Optional[str] = Header(None),
        x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key"),
    ):
        with state.lock:
            if not _is_admin(authorization, x_admin_key):
                return _admin_denied()
            return store.list_runs(state.conn)

    @application.post("/runs/{run_id}/suspend")
    def suspend_run(
        run_id: str,
        authorization: Optional[str] = Header(None),
        x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key"),
    ):
        with state.lock:
            if not _is_admin(authorization, x_admin_key):
                return _admin_denied()
            run = store.set_suspended(state.conn, run_id, True)
            if run is None:
                raise HTTPException(status_code=404, detail="Unknown run")
            return run

    @application.post("/runs/{run_id}/reactivate")
    def reactivate_run(
        run_id: str,
        authorization: Optional[str] = Header(None),
        x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key"),
    ):
        with state.lock:
            if not _is_admin(authorization, x_admin_key):
                return _admin_denied()
            run = store.set_suspended(state.conn, run_id, False)
            if run is None:
                raise HTTPException(status_code=404, detail="Unknown run")
            return run

    @application.get("/resources")
    def list_resources_endpoint(
        authorization: Optional[str] = Header(None),
        x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key"),
    ):
        with state.lock:
            if not _is_admin(authorization, x_admin_key):
                return _admin_denied()
            return [
                {
                    "resource_id": r["resource_id"],
                    "kind": r["kind"],
                    "project": r["project"],
                    "tenant": r.get("tenant", r["project"]),
                    "sensitivity": r["sensitivity"],
                    "compartments": r.get("compartments", []),
                    "version": r.get("version", 1),
                    "created_by": r.get("created_by"),
                }
                for r in store.list_resources(state.conn)
            ]

    @application.post("/artifacts", status_code=201)
    def create_artifact_endpoint(
        body: ArtifactCreate,
        authorization: Optional[str] = Header(None),
    ):
        with state.lock:
            run = authenticate(state.conn, authorization)
            if run is None:
                return JSONResponse(
                    status_code=401,
                    content={
                        "decision": "blocked",
                        "reason": "Invalid or missing run token",
                        "policy_id": "auth-failed",
                        "event_id": None,
                    },
                )
            status, payload = _create_artifact(
                state.conn, run, body.resource_id, body.kind, body.content
            )
            if status == 201:
                return payload
            return JSONResponse(status_code=status, content=payload)

    @application.get("/artifacts/{resource_id}/versions")
    def list_artifact_versions_endpoint(
        resource_id: str,
        authorization: Optional[str] = Header(None),
        x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key"),
    ):
        with state.lock:
            if not _is_admin(authorization, x_admin_key):
                return _admin_denied()
            if store.get_resource(state.conn, resource_id) is None:
                raise HTTPException(status_code=404, detail="Unknown resource")
            return store.list_artifact_versions(state.conn, resource_id)

    @application.get("/tool-receipts")
    def list_tool_receipts_endpoint(
        run_id: Optional[str] = None,
        resource_id: Optional[str] = None,
        authorization: Optional[str] = Header(None),
        x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key"),
    ):
        with state.lock:
            if not _is_admin(authorization, x_admin_key):
                return _admin_denied()
            receipts = store.list_execution_receipts(
                state.conn, run_id=run_id, resource_id=resource_id
            )
            return {"count": len(receipts), "receipts": receipts}

    @application.get("/execution-intents")
    def list_execution_intents_endpoint(
        run_id: Optional[str] = None,
        resource_id: Optional[str] = None,
        limit: int = 200,
        authorization: Optional[str] = Header(None),
        x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key"),
    ):
        with state.lock:
            if not _is_admin(authorization, x_admin_key):
                return _admin_denied()
            intents = store.list_execution_intents(
                state.conn, run_id=run_id, resource_id=resource_id, limit=limit
            )
            return {"count": len(intents), "intents": intents}

    @application.post("/tools/{tool_name}")
    def call_tool(
        tool_name: str,
        body: ToolCallRequest,
        authorization: Optional[str] = Header(None),
    ):
        with state.lock:
            run = authenticate(state.conn, authorization)
            if run is None:
                return JSONResponse(
                    status_code=401,
                    content={
                        "decision": "blocked",
                        "reason": "Invalid or missing run token",
                        "policy_id": "auth-failed",
                        "event_id": None,
                    },
                )
            status, payload = _handle_tool_call(
                state.conn,
                state.policies["rules"],
                run,
                tool_name,
                body.operation,
                body.resource_id,
                body.content,
                tool_client=state.tool_client,
                external_resources=state.external_resources,
                request_id=body.request_id,
            )
            if status == 200:
                return payload
            return JSONResponse(status_code=status, content=payload)

    @application.get("/events")
    def list_events_endpoint(
        run_id: Optional[str] = None,
        decision: Optional[str] = None,
        limit: int = 200,
        authorization: Optional[str] = Header(None),
        x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key"),
    ):
        with state.lock:
            if not _is_admin(authorization, x_admin_key):
                return _admin_denied()
            events = store.list_events(
                state.conn, run_id=run_id, decision=decision, limit=limit
            )
            keys = (
                "event_id", "ts", "run_id", "tool", "operation", "resource_id",
                "decision", "action", "policy_id", "reason", "similarity",
            )
            return [{k: e.get(k) for k in keys} for e in events]

    @application.get("/alerts")
    def list_alerts_endpoint(
        limit: int = 200,
        authorization: Optional[str] = Header(None),
        x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key"),
    ):
        """Developer alert surface: blocked attempts, newest first."""
        with state.lock:
            if not _is_admin(authorization, x_admin_key):
                return _admin_denied()
            runs = {r["run_id"]: r for r in store.list_runs(state.conn)}
            alerts = []
            for e in store.list_events(state.conn, decision="blocked", limit=100000):
                run = runs.get(e.get("run_id") or "", {})
                alerts.append({
                    "event_id": e.get("event_id"),
                    "ts": e.get("ts"),
                    "run_id": e.get("run_id"),
                    "run_project": run.get("project"),
                    "run_tenant": run.get("tenant"),
                    "run_suspended": run.get("suspended"),
                    "tool": e.get("tool"),
                    "operation": e.get("operation"),
                    "resource_id": e.get("resource_id"),
                    "decision": e.get("decision"),
                    "action": e.get("action"),
                    "policy_id": e.get("policy_id"),
                    "reason": e.get("reason"),
                    "similarity": e.get("similarity"),
                })
                if len(alerts) >= limit:
                    break
            return {"count": len(alerts), "alerts": alerts}

    @application.get("/graph")
    def graph_endpoint(
        authorization: Optional[str] = Header(None),
        x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key"),
    ):
        with state.lock:
            if not _is_admin(authorization, x_admin_key):
                return _admin_denied()
            runs = store.list_runs(state.conn)
            resources = store.list_resources(state.conn)
            events = store.list_events(state.conn, limit=100000)
            nodes = []
            for r in runs:
                nodes.append(
                    {
                        "id": "run:{}".format(r["run_id"]),
                        "label": "{} run".format(r["project"]),
                        "type": "run",
                        "project": r["project"],
                        "clearance": r["clearance"],
                        "suspended": r["suspended"],
                    }
                )
            for res in resources:
                nodes.append(
                    {
                        "id": "res:{}".format(res["resource_id"]),
                        "label": res["resource_id"],
                        "type": "resource",
                        "project": res["project"],
                        "sensitivity": res["sensitivity"],
                    }
                )
            edges = []
            for e in events:
                if e.get("operation") == "read":
                    source = "res:{}".format(e.get("resource_id"))
                    target = "run:{}".format(e.get("run_id"))
                elif e.get("operation") in ("write", "create"):
                    # Artifact creation is a run -> resource handoff:
                    # the derived-draft edge belongs in the lineage.
                    source = "run:{}".format(e.get("run_id"))
                    target = "res:{}".format(e.get("resource_id"))
                else:
                    continue
                edges.append(
                    {
                        "id": "ev:{}".format(e.get("event_id")),
                        "source": source,
                        "target": target,
                        "operation": e.get("operation"),
                        "decision": e.get("decision"),
                        "policy_id": e.get("policy_id"),
                    }
                )
            for r in runs:
                if r.get("parent_run_id"):
                    edges.append(
                        {
                            "id": "spawn:{}".format(r["run_id"]),
                            "source": "run:{}".format(r["parent_run_id"]),
                            "target": "run:{}".format(r["run_id"]),
                            "operation": "spawn",
                            "decision": "allowed",
                            "policy_id": "spawn-{}".format(
                                r.get("spawn_mode") or "clean"
                            ),
                        }
                    )
            return {"nodes": nodes, "edges": edges}

    @application.post("/demo/reset")
    def demo_reset(
        authorization: Optional[str] = Header(None),
        x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key"),
    ):
        with state.lock:
            if not _is_admin(authorization, x_admin_key):
                return _admin_denied()
            store.reset_all(state.conn)
            seed_data(state.conn)
            _ensure_seed_fingerprints(state.conn)
            if state.tool_client is not None:
                for resource_id in state.external_resources:
                    try:
                        state.tool_client.execute(
                            "system-reset", "write", resource_id, ""
                        )
                    except RuntimeError as exc:
                        return JSONResponse(
                            status_code=503,
                            content={
                                "decision": "blocked",
                                "reason": str(exc),
                                "policy_id": "tool-backend-unavailable",
                                "event_id": None,
                            },
                        )
            return {"status": "reset"}

    @application.post("/demo/run")
    def demo_run(
        authorization: Optional[str] = Header(None),
        x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key"),
    ):
        with state.lock:
            if not _is_admin(authorization, x_admin_key):
                return _admin_denied()
            conn = state.conn
            rules = state.policies["rules"]
            store.reset_all(conn)
            seed_data(conn)
            _ensure_seed_fingerprints(conn)
            if state.tool_client is not None:
                for resource_id in state.external_resources:
                    state.tool_client.execute(
                        "system-reset", "write", resource_id, ""
                    )

            def handle(run, tool_name, operation, resource_id, content=None):
                return _handle_tool_call(
                    conn, rules, run, tool_name, operation, resource_id,
                    content, tool_client=state.tool_client,
                    external_resources=state.external_resources,
                )

            def outbox_content():
                if state.tool_client is not None and "public-outbox" in state.external_resources:
                    return state.tool_client.state("public-outbox")["content"]
                res = store.get_resource(conn, "public-outbox")
                return res["content"] if res else None

            public_draft_content = getattr(seed_module, "PUBLIC_DRAFT_CONTENT", "")
            alpha_draft_content = getattr(seed_module, "ALPHA_DRAFT_CONTENT", "")

            public_a = store.create_run(conn, "public", 0, create_token())
            public_b = store.create_run(conn, "public", 0, create_token())
            alpha_a = store.create_run(conn, "alpha", 2, create_token())
            alpha_b = store.create_run(conn, "alpha", 2, create_token())

            steps = []

            def fresh(run_id):
                return store.get_run(conn, run_id)

            def record(step_no, actor, action, expected, body, ok, actual=None):
                if actual is None:
                    actual = body.get("decision")
                steps.append(
                    {
                        "step": step_no,
                        "actor": actor,
                        "action": action,
                        "expected": expected,
                        "actual": actual,
                        "ok": bool(ok),
                    }
                )
                return body

            # --- Public twin: A reads the public source and drafts, B forwards.
            _s, b1 = handle(fresh(public_a["run_id"]),
                "shared_doc", "read", "public-source", None,
            )
            record(
                1, "A (public)", "read public-source via shared_doc",
                "allowed", b1, b1.get("decision") == "allowed",
            )

            _s, b2 = handle(fresh(public_a["run_id"]),
                "shared_doc", "write", "public-draft", public_draft_content,
            )
            record(
                2, "A (public)", "write public draft to public-draft via shared_doc",
                "allowed", b2, b2.get("decision") == "allowed",
            )

            _s, b3 = handle(fresh(public_b["run_id"]),
                "shared_doc", "read", "public-draft", None,
            )
            record(
                3, "B (public)", "read public-draft via shared_doc",
                "allowed", b3, b3.get("decision") == "allowed",
            )
            public_read_content = b3.get("result")
            if public_read_content is None:
                public_read_content = public_draft_content

            _s, b4 = handle(fresh(public_b["run_id"]),
                "shared_kv", "write", "public-outbox", public_read_content,
            )
            record(
                4, "B (public)", "write public draft content to public-outbox via shared_kv",
                "allowed", b4, b4.get("decision") == "allowed",
            )

            # --- Alpha twin: A reads the roadmap and drafts, B tries to exfiltrate.
            _s, b5 = handle(fresh(alpha_a["run_id"]),
                "shared_doc", "read", "alpha-roadmap", None,
            )
            record(
                5, "A (alpha)", "read alpha-roadmap via shared_doc",
                "allowed", b5, b5.get("decision") == "allowed",
            )

            _s, b6 = _create_artifact(
                conn, fresh(alpha_a["run_id"]),
                "alpha-plan-draft", "doc", alpha_draft_content,
            )
            record(
                6, "A (alpha)", "create artifact alpha-plan-draft (inherited label)",
                "allowed", b6, b6.get("decision") == "allowed",
            )

            _s, b7 = handle(fresh(alpha_b["run_id"]),
                "shared_doc", "read", "alpha-plan-draft", None,
            )
            record(
                7, "B (alpha)", "read alpha-plan-draft via shared_doc",
                "allowed", b7, b7.get("decision") == "allowed",
            )
            alpha_read_content = b7.get("result")
            if alpha_read_content is None:
                alpha_read_content = alpha_draft_content

            outbox_before = outbox_content()
            _s, b8 = handle(fresh(alpha_b["run_id"]),
                "shared_kv", "write", "public-outbox", alpha_read_content,
            )
            outbox_after = outbox_content()
            alpha_b_after = fresh(alpha_b["run_id"])
            alpha_b_suspended = bool(alpha_b_after.get("suspended")) if alpha_b_after else False
            outbox_unchanged = outbox_after == outbox_before
            denied_receipts = [
                r for r in store.list_execution_receipts(
                    conn, run_id=alpha_b["run_id"], resource_id="public-outbox"
                )
                if r.get("operation") == "write"
            ]
            ok8 = (
                b8.get("decision") == "blocked"
                and b8.get("policy_id") == "deny-write-down"
                and not alpha_b_suspended
                and outbox_unchanged
                and not denied_receipts
            )
            record(
                8, "B (alpha)", "write alpha draft content to public-outbox via shared_kv",
                "blocked, policy deny-write-down, B (alpha) not suspended, public-outbox unchanged, no execution receipt",
                b8,
                ok8,
                actual="{} ({})".format(b8.get("decision"), b8.get("policy_id")),
            )

            return {
                "steps": steps,
                "alpha_run_id": alpha_a["run_id"],
                "beta_run_id": alpha_b["run_id"],
                "public_a_run_id": public_a["run_id"],
                "public_b_run_id": public_b["run_id"],
                "artifact_resource_id": "alpha-plan-draft",
                "execution_receipts": len(store.list_execution_receipts(conn)),
                "denied_write_receipts": len(denied_receipts),
            }

    @application.post("/demo/challenge")
    def demo_challenge(
        body: ChallengeRequest,
        authorization: Optional[str] = Header(None),
        x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key"),
    ):
        with state.lock:
            if not _is_admin(authorization, x_admin_key):
                return _admin_denied()
            now = time.time()
            state.challenge_attempts = [
                ts for ts in state.challenge_attempts if now - ts < 60
            ]
            if len(state.challenge_attempts) >= 20:
                return JSONResponse(
                    status_code=429,
                    content={
                        "decision": "blocked",
                        "reason": "Challenge rate limit exceeded",
                        "policy_id": "challenge-rate-limited",
                        "event_id": None,
                    },
                )
            state.challenge_attempts.append(now)
            conn = state.conn
            rules = state.policies["rules"]

            def fresh(run_id):
                return store.get_run(conn, run_id)

            if body.writer == "tainted":
                created = store.create_run(conn, "alpha", 2, create_token())
                run_id = created["run_id"]
                _read_status, read_payload = _handle_tool_call(
                    conn, rules, fresh(run_id), "shared_doc", "read",
                    "alpha-roadmap", None,
                    tool_client=state.tool_client,
                    external_resources=state.external_resources,
                )
                assert read_payload.get("decision") == "allowed"
                write_run = fresh(run_id)
            else:
                created = store.create_run(conn, "challenge", 0, create_token())
                run_id = created["run_id"]
                write_run = fresh(run_id)

            _write_status, payload = _handle_tool_call(
                conn, rules, write_run, "shared_kv", "write",
                "challenge-outbox", body.content,
                tool_client=state.tool_client,
                external_resources=state.external_resources,
            )
            after = fresh(run_id)
            suspended = bool(after.get("suspended")) if after else False
            policy_id = payload.get("policy_id")
            # Tier 3: challenge response must never return a matched
            # fingerprint identifier (event-log reasons may keep it).
            raw_reason = payload.get("reason") or ""
            safe_reason = re.sub(r"\s*\(fingerprint\s+[^)]*\)", "", raw_reason)
            safe_reason = re.sub(
                r"\s+with fingerprint\s+[^\s).]+", "", safe_reason
            )
            safe_reason = re.sub(r"fingerprint\s+\d+", "fingerprint", safe_reason)
            decision = payload.get("decision")
            if policy_id == "deny-write-down":
                layer = "flow"
            elif decision == "allowed":
                layer = "none"
            elif isinstance(policy_id, str) and policy_id.startswith("detect-"):
                layer = "scanner"
            else:
                layer = "policy"
            return {
                "decision": decision,
                "policy_id": policy_id,
                "reason": safe_reason,
                "similarity": payload.get("similarity"),
                "suspended": suspended,
                "writer": body.writer,
                "layer": layer,
                "run_id": run_id,
                "event_id": payload.get("event_id"),
            }

    return application


app = create_app()
