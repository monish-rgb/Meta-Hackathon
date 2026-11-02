"""FlowGate write-path leak detection.

Pure functions only: no I/O, no app imports. Implements the Detection
semantics from CONTRACT.md (frozen).
"""

import hashlib
import re

SHINGLE_K = 5
DENY_THRESHOLD = 0.15
SUSPEND_THRESHOLD = 0.30
CONTAINMENT_THRESHOLD = 0.5

_CANARY_RE = re.compile(r"CANARY-[A-Z0-9]+(?:-[A-Z0-9]+)*")
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def normalize_content(content: str) -> str:
    """Lowercase, collapse all whitespace runs to a single space, strip."""
    return re.sub(r"\s+", " ", content.lower()).strip()


def content_hash(content: str) -> str:
    """SHA-256 hex digest of the normalized content."""
    return hashlib.sha256(normalize_content(content).encode("utf-8")).hexdigest()


def extract_canaries(content: str) -> list[str]:
    """All unique canary markers in content, sorted."""
    return sorted(set(_CANARY_RE.findall(content)))


def shingles(content: str, k: int = 5) -> set[str]:
    """Word 5-shingles over lowercase alphanumeric tokens."""
    tokens = _TOKEN_RE.findall(content.lower())
    if not tokens:
        return set()
    if len(tokens) < k:
        return {" ".join(tokens)}
    return {" ".join(tokens[i : i + k]) for i in range(len(tokens) - k + 1)}


def jaccard(a: set, b: set) -> float:
    """Jaccard similarity; 0.0 if either set is empty."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def make_fingerprint(content: str) -> dict:
    """Fingerprint of content for lineage tracking (JSON-serializable)."""
    return {
        "hash": content_hash(content),
        "canaries": extract_canaries(content),
        "shingles": sorted(shingles(content)),
    }


def _fingerprint_id(fp: dict):
    # Contract names the key "id"; tolerate the store-style alias too.
    return fp.get("id", fp.get("fingerprint_id"))


def _containment(a: set, b: set) -> float:
    """Overlap relative to the smaller set; 0.0 if either set is empty."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def scan_write(
    content: str,
    fingerprints: list[dict],
    run_project: str,
    target_sensitivity: int,
) -> dict:
    """Scan a write against lineage fingerprints, severity ladder in order.

    Sensitivity-0 (public) fingerprints never taint and are ignored
    entirely. Any other fingerprint is considered only if its sensitivity
    is higher than the target's OR it comes from a different project.
    Checks, in severity order: exact hash match, canary intersection,
    max Jaccard >= 0.30, max Jaccard >= 0.15, max containment >= 0.5.
    """
    considered = [
        fp
        for fp in (fingerprints or [])
        if (fp.get("source_sensitivity") or 0) > target_sensitivity
        or (
            fp.get("source_project") != run_project
            and (fp.get("source_sensitivity") or 0) > 0
        )
    ]

    digest = content_hash(content)
    content_canaries = set(extract_canaries(content))
    content_shingles = shingles(content)

    best_id = None
    best_sim = 0.0
    best_containment_id = None
    best_containment = 0.0
    for fp in considered:
        fp_shingles = set(fp.get("shingles") or [])
        sim = jaccard(content_shingles, fp_shingles)
        if sim > best_sim:
            best_sim = sim
            best_id = _fingerprint_id(fp)
        containment = _containment(content_shingles, fp_shingles)
        if containment > best_containment:
            best_containment = containment
            best_containment_id = _fingerprint_id(fp)
    similarity = round(best_sim, 4)

    for fp in considered:
        if digest == fp.get("hash"):
            fid = _fingerprint_id(fp)
            return {
                "action": "deny_and_suspend",
                "policy_id": "detect-exact-copy",
                "reason": f"Exact copy of protected content detected (fingerprint {fid}).",
                "similarity": similarity,
                "matched_fingerprint_id": fid,
                "matched_canary": None,
            }

    for fp in considered:
        hit = content_canaries & set(fp.get("canaries") or [])
        if hit:
            canary = sorted(hit)[0]
            fid = _fingerprint_id(fp)
            return {
                "action": "deny_and_suspend",
                "policy_id": "detect-canary",
                "reason": f"Canary token leaked (fingerprint {fid}).",
                "similarity": similarity,
                "matched_fingerprint_id": fid,
                "matched_canary": canary,
            }

    if best_sim >= SUSPEND_THRESHOLD:
        return {
            "action": "deny_and_suspend",
            "policy_id": "detect-taint-overlap",
            "reason": f"Taint overlap detected (similarity {similarity:.4f} with fingerprint {best_id}).",
            "similarity": similarity,
            "matched_fingerprint_id": best_id,
            "matched_canary": None,
        }
    if best_sim >= DENY_THRESHOLD:
        return {
            "action": "deny",
            "policy_id": "detect-suspicious-overlap",
            "reason": f"Suspicious overlap detected (similarity {similarity:.4f} with fingerprint {best_id}).",
            "similarity": similarity,
            "matched_fingerprint_id": best_id,
            "matched_canary": None,
        }
    if best_containment >= CONTAINMENT_THRESHOLD:
        return {
            "action": "deny",
            "policy_id": "detect-suspicious-overlap",
            "reason": (
                f"Suspicious overlap detected (containment "
                f"{best_containment * 100:.1f}% with fingerprint {best_containment_id})."
            ),
            "similarity": similarity,
            "matched_fingerprint_id": best_containment_id,
            "matched_canary": None,
        }
    return {
        "action": "allow",
        "policy_id": "detect-clean",
        "reason": "No protected content detected.",
        "similarity": similarity,
        "matched_fingerprint_id": None,
        "matched_canary": None,
    }
