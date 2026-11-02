"""Unit tests for app.detection (CONTRACT.md Detection semantics)."""

import hashlib

from app.detection import (
    CONTAINMENT_THRESHOLD,
    DENY_THRESHOLD,
    SHINGLE_K,
    SUSPEND_THRESHOLD,
    content_hash,
    extract_canaries,
    jaccard,
    make_fingerprint,
    normalize_content,
    scan_write,
    shingles,
)


def make_fp(fid, text, project="alpha", sensitivity=2, resource_id="alpha-roadmap"):
    fp = make_fingerprint(text)
    fp.update(
        {
            "id": fid,
            "source_resource_id": resource_id,
            "source_project": project,
            "source_sensitivity": sensitivity,
        }
    )
    return fp


def synthetic_fp(fid, shared_shingles, n_extra, project="omega", sensitivity=2):
    """Fingerprint with caller-controlled shingle overlap (deterministic Jaccard)."""
    extras = [f"pad{i} qzx wkv extra filler words {i}" for i in range(n_extra)]
    return {
        "id": fid,
        "source_resource_id": "omega-doc",
        "source_project": project,
        "source_sensitivity": sensitivity,
        "hash": "f" * 64,
        "canaries": [],
        "shingles": list(shared_shingles) + extras,
    }


# --- constants ---


def test_constants_match_contract():
    assert SHINGLE_K == 5
    assert DENY_THRESHOLD == 0.15
    assert SUSPEND_THRESHOLD == 0.30
    assert CONTAINMENT_THRESHOLD == 0.5


# --- normalize ---


def test_normalize_lowercase_whitespace_strip():
    assert normalize_content("  Hello   WORLD\n\tFoo  ") == "hello world foo"
    assert normalize_content("") == ""
    assert normalize_content("   \n\t  ") == ""


# --- hash ---


def test_content_hash_is_sha256_of_normalized():
    assert content_hash("Hello  World") == hashlib.sha256(b"hello world").hexdigest()


def test_content_hash_insensitive_to_case_and_spacing():
    assert content_hash("Hello   World") == content_hash("hello world")
    assert content_hash("\n Project Nova \t roadmap ") == content_hash("project nova roadmap")


def test_content_hash_differs_for_different_content():
    assert content_hash("alpha") != content_hash("beta")


# --- canaries ---


def test_extract_canaries_sorted_unique():
    text = "leak CANARY-BETA-123 and CANARY-ALPHA-NOVA-7F3K9 then CANARY-BETA-123 again"
    assert extract_canaries(text) == ["CANARY-ALPHA-NOVA-7F3K9", "CANARY-BETA-123"]


def test_extract_canaries_none():
    assert extract_canaries("nothing to see here") == []


def test_extract_canaries_case_sensitive_and_well_formed():
    assert extract_canaries("canary-abc-123 CANARY- CANARY") == []


def test_extract_canaries_multi_segment():
    assert extract_canaries("tag CANARY-A-B-C-1 end") == ["CANARY-A-B-C-1"]


# --- shingles ---


def test_shingles_empty_without_tokens():
    assert shingles("") == set()
    assert shingles("!!! ... ???") == set()


def test_shingles_short_content_single_shingle():
    assert shingles("hello world", k=5) == {"hello world"}


def test_shingles_sliding_window():
    assert shingles("a b c d e f", k=2) == {"a b", "b c", "c d", "d e", "e f"}


def test_shingles_default_k_is_five():
    tokens = "t0 t1 t2 t3 t4 t5 t6"
    assert shingles(tokens) == shingles(tokens, k=5)
    assert len(shingles(tokens)) == 3  # 7 tokens -> 3 five-grams


def test_shingles_lowercase_alphanumeric_tokens():
    assert shingles("Hello, WORLD!") == {"hello world"}


# --- jaccard ---


def test_jaccard_empty_is_zero():
    assert jaccard(set(), set()) == 0.0
    assert jaccard({"a"}, set()) == 0.0
    assert jaccard(set(), {"a"}) == 0.0


def test_jaccard_identical_is_one():
    assert jaccard({"a", "b"}, {"a", "b"}) == 1.0


def test_jaccard_partial_and_disjoint():
    assert jaccard({"a", "b"}, {"b", "c"}) == 1 / 3
    assert jaccard({"a", "b"}, {"c", "d"}) == 0.0


# --- fingerprint ---


def test_make_fingerprint_shape():
    fp = make_fingerprint("hello world CANARY-X-1")
    assert set(fp.keys()) == {"hash", "canaries", "shingles"}
    assert fp["hash"] == content_hash("hello world CANARY-X-1")
    assert fp["canaries"] == ["CANARY-X-1"]
    assert fp["shingles"] == sorted(fp["shingles"])


# --- scan_write ladder ---

GARDEN_BASE = (
    "the quick brown fox jumps over the lazy dog near the river bank at dawn "
    "while birds sing in the tall green trees and fish swim in the clear cool "
    "water under the bright morning sun"
)
GARDEN_EDIT = GARDEN_BASE.replace("fox", "wolf").replace("river", "stream")


def test_scan_write_exact_copy_suspends():
    text = GARDEN_BASE + " extra secret plans"
    fp = make_fp(7, text, project="alpha", sensitivity=2)
    result = scan_write(text, [fp], run_project="beta", target_sensitivity=0)
    assert result["action"] == "deny_and_suspend"
    assert result["policy_id"] == "detect-exact-copy"
    assert result["matched_fingerprint_id"] == 7
    assert result["matched_canary"] is None
    assert result["similarity"] == 1.0


def test_scan_write_ignores_same_project_at_or_below_target_sensitivity():
    text = "identical secret content here"
    fp = make_fp(3, text, project="alpha", sensitivity=1)
    result = scan_write(text, [fp], run_project="alpha", target_sensitivity=1)
    assert result["action"] == "allow"
    assert result["policy_id"] == "detect-clean"
    assert result["matched_fingerprint_id"] is None
    assert result["matched_canary"] is None


def test_scan_write_same_project_higher_sensitivity_is_considered():
    text = "identical secret content here"
    fp = make_fp(4, text, project="alpha", sensitivity=2)
    result = scan_write(text, [fp], run_project="alpha", target_sensitivity=0)
    assert result["action"] == "deny_and_suspend"
    assert result["policy_id"] == "detect-exact-copy"
    assert result["matched_fingerprint_id"] == 4


def test_scan_write_canary_leak_suspends():
    source = "project nova roadmap launch budget CANARY-ALPHA-NOVA-7F3K9 details here"
    fp = make_fp(11, source, project="alpha", sensitivity=2)
    leak = "completely different words about other topics CANARY-ALPHA-NOVA-7F3K9 reused"
    assert content_hash(leak) != fp["hash"]
    result = scan_write(leak, [fp], run_project="beta", target_sensitivity=0)
    assert result["action"] == "deny_and_suspend"
    assert result["policy_id"] == "detect-canary"
    assert result["matched_fingerprint_id"] == 11
    assert result["matched_canary"] == "CANARY-ALPHA-NOVA-7F3K9"
    # The canary value stays in the matched_canary field, but must not leak
    # into the human reason (which is exposed via the open events feed).
    assert "CANARY-ALPHA-NOVA-7F3K9" not in result["reason"]


def test_scan_write_taint_overlap_suspends():
    fp = make_fp(21, GARDEN_BASE, project="alpha", sensitivity=2)
    assert content_hash(GARDEN_EDIT) != fp["hash"]
    assert jaccard(shingles(GARDEN_EDIT), set(fp["shingles"])) >= 0.30
    result = scan_write(GARDEN_EDIT, [fp], run_project="beta", target_sensitivity=0)
    assert result["action"] == "deny_and_suspend"
    assert result["policy_id"] == "detect-taint-overlap"
    assert result["matched_fingerprint_id"] == 21
    assert result["matched_canary"] is None
    assert result["similarity"] >= 0.30


def test_scan_write_suspicious_overlap_denies_without_suspend():
    content = "a b c d e f g h i j k l m n o p q r s t"  # 20 tokens -> 16 shingles
    content_sh = shingles(content)
    assert len(content_sh) == 16
    fp = synthetic_fp(31, content_sh, n_extra=60)  # 16/76 ~= 0.2105
    expected = round(16 / 76, 4)
    assert DENY_THRESHOLD <= expected < SUSPEND_THRESHOLD
    assert content_hash(content) != fp["hash"]
    result = scan_write(content, [fp], run_project="beta", target_sensitivity=0)
    assert result["action"] == "deny"
    assert result["policy_id"] == "detect-suspicious-overlap"
    assert result["matched_fingerprint_id"] == 31
    assert result["matched_canary"] is None
    assert result["similarity"] == expected


def test_scan_write_max_similarity_wins_and_boundaries():
    content = "a b c d e f g"  # 7 tokens -> 3 shingles
    assert len(shingles(content)) == 3
    low = synthetic_fp(41, shingles(content), n_extra=17)  # 3/20 = 0.15
    high = synthetic_fp(42, shingles(content), n_extra=7)  # 3/10 = 0.30
    result = scan_write(content, [low, high], run_project="beta", target_sensitivity=0)
    assert result["action"] == "deny_and_suspend"
    assert result["policy_id"] == "detect-taint-overlap"
    assert result["matched_fingerprint_id"] == 42
    assert result["similarity"] == 0.3
    only_low = scan_write(content, [low], run_project="beta", target_sensitivity=0)
    assert only_low["action"] == "deny"
    assert only_low["policy_id"] == "detect-suspicious-overlap"
    assert only_low["similarity"] == 0.15


def test_scan_write_severity_exact_beats_canary():
    text = "private plans CANARY-Z-9 end"
    fp_exact = make_fp(51, text, project="alpha", sensitivity=2)
    fp_canary = make_fp(52, "other words CANARY-Z-9 here", project="gamma", sensitivity=2)
    result = scan_write(text, [fp_canary, fp_exact], run_project="beta", target_sensitivity=0)
    assert result["action"] == "deny_and_suspend"
    assert result["policy_id"] == "detect-exact-copy"


def test_scan_write_severity_canary_beats_overlap():
    source = GARDEN_BASE + " CANARY-Q-1"
    fp = make_fp(61, source, project="alpha", sensitivity=2)
    leak = GARDEN_EDIT + " CANARY-Q-1"
    assert content_hash(leak) != fp["hash"]
    assert jaccard(shingles(leak), set(fp["shingles"])) >= 0.30
    result = scan_write(leak, [fp], run_project="beta", target_sensitivity=0)
    assert result["action"] == "deny_and_suspend"
    assert result["policy_id"] == "detect-canary"
    assert result["matched_canary"] == "CANARY-Q-1"


def test_scan_write_accepts_set_shingles():
    fp = make_fp(81, GARDEN_BASE, project="alpha", sensitivity=2)
    fp["shingles"] = set(fp["shingles"])
    result = scan_write(GARDEN_EDIT, [fp], run_project="beta", target_sensitivity=0)
    assert result["policy_id"] == "detect-taint-overlap"


def test_scan_write_clean_allows():
    fp = make_fp(71, GARDEN_BASE, project="alpha", sensitivity=2)
    clean = "my own unrelated note about lunch plans and weekend chores"
    result = scan_write(clean, [fp], run_project="beta", target_sensitivity=0)
    assert result["action"] == "allow"
    assert result["policy_id"] == "detect-clean"
    assert result["matched_fingerprint_id"] is None
    assert result["matched_canary"] is None
    assert result["similarity"] < DENY_THRESHOLD


def test_scan_write_no_fingerprints_is_clean():
    result = scan_write("anything at all", [], run_project="beta", target_sensitivity=0)
    assert result["action"] == "allow"
    assert result["policy_id"] == "detect-clean"
    assert result["similarity"] == 0.0
    assert result["matched_fingerprint_id"] is None
    assert result["matched_canary"] is None


def test_scan_write_result_shape():
    result = scan_write("x", [], run_project="beta", target_sensitivity=0)
    assert set(result.keys()) == {
        "action",
        "policy_id",
        "reason",
        "similarity",
        "matched_fingerprint_id",
        "matched_canary",
    }
    assert isinstance(result["reason"], str) and result["reason"]


# --- sensitivity-0 (public) fingerprints never taint ---


def test_scan_write_exact_copy_of_sensitivity_zero_fingerprint_is_clean():
    text = "public announcement everyone may read and share this note freely"
    fp = make_fp(91, text, project="alpha", sensitivity=0, resource_id="public-doc")
    result = scan_write(text, [fp], run_project="beta", target_sensitivity=0)
    assert result["action"] == "allow"
    assert result["policy_id"] == "detect-clean"
    assert result["matched_fingerprint_id"] is None
    assert result["matched_canary"] is None
    assert result["similarity"] == 0.0


def test_scan_write_canary_in_sensitivity_zero_fingerprint_is_ignored():
    source = "public bulletin CANARY-PUBLIC-1 everyone can copy this announcement"
    fp = make_fp(92, source, project="alpha", sensitivity=0, resource_id="public-doc")
    assert fp["canaries"] == ["CANARY-PUBLIC-1"]
    leak = "different public wording mentioning CANARY-PUBLIC-1 for everyone"
    result = scan_write(leak, [fp], run_project="beta", target_sensitivity=0)
    assert result["action"] == "allow"
    assert result["policy_id"] == "detect-clean"
    assert result["matched_fingerprint_id"] is None
    assert result["matched_canary"] is None
    assert result["similarity"] == 0.0


def test_scan_write_sensitivity_one_shared_project_fingerprint_still_triggers():
    text = "internal briefing with launch checklist and staffing notes for the team"
    # A "shared"-project fingerprint above public sensitivity still taints a
    # write to a sensitivity-0 target.
    fp_shared = make_fp(93, text, project="shared", sensitivity=1, resource_id="shared-internal")
    result = scan_write(text, [fp_shared], run_project="beta", target_sensitivity=0)
    assert result["action"] == "deny_and_suspend"
    assert result["policy_id"] == "detect-exact-copy"
    assert result["matched_fingerprint_id"] == 93
    # Likewise, a same-project sensitivity-1 fingerprint still taints a
    # write down to a sensitivity-0 target (guard against over-filtering).
    fp_same = make_fp(94, text, project="beta", sensitivity=1, resource_id="beta-internal")
    result = scan_write(text, [fp_same], run_project="beta", target_sensitivity=0)
    assert result["action"] == "deny_and_suspend"
    assert result["policy_id"] == "detect-exact-copy"
    assert result["matched_fingerprint_id"] == 94


# --- containment rung (after both Jaccard rungs, before clean) ---


def _containment(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


_LONG_SOURCE = " ".join(f"sourceword{i}" for i in range(120))  # 116 shingles


def test_scan_write_containment_denies_short_verbatim_excerpt():
    fp = make_fp(101, _LONG_SOURCE, project="alpha", sensitivity=2)
    excerpt = " ".join(f"sourceword{i}" for i in range(40, 55))  # 15 words
    excerpt_shingles = shingles(excerpt)
    fp_shingles = set(fp["shingles"])
    assert content_hash(excerpt) != fp["hash"]
    # 11/116 ~= 0.0948: below the Jaccard deny rung, fully contained.
    assert round(jaccard(excerpt_shingles, fp_shingles), 4) == 0.0948
    assert jaccard(excerpt_shingles, fp_shingles) < DENY_THRESHOLD
    assert _containment(excerpt_shingles, fp_shingles) == 1.0
    result = scan_write(excerpt, [fp], run_project="beta", target_sensitivity=0)
    assert result["action"] == "deny"  # containment never suspends
    assert result["policy_id"] == "detect-suspicious-overlap"
    assert result["matched_fingerprint_id"] == 101
    assert result["matched_canary"] is None
    assert result["similarity"] == 0.0948  # similarity stays max Jaccard
    assert "containment" in result["reason"]
    assert "100.0%" in result["reason"]


def test_scan_write_containment_denies_each_short_chunk():
    fp = make_fp(102, _LONG_SOURCE, project="alpha", sensitivity=2)
    fp_shingles = set(fp["shingles"])
    chunks = [
        " ".join(f"sourceword{i}" for i in range(10, 18)),  # 8 words
        " ".join(f"sourceword{i}" for i in range(50, 59)),  # 9 words
        " ".join(f"sourceword{i}" for i in range(90, 100)),  # 10 words
    ]
    for chunk in chunks:
        chunk_shingles = shingles(chunk)
        assert jaccard(chunk_shingles, fp_shingles) < DENY_THRESHOLD, chunk
        assert _containment(chunk_shingles, fp_shingles) == 1.0, chunk
        result = scan_write(chunk, [fp], run_project="beta", target_sensitivity=0)
        assert result["action"] == "deny", chunk
        assert result["policy_id"] == "detect-suspicious-overlap", chunk
        assert result["matched_fingerprint_id"] == 102, chunk
        assert result["matched_canary"] is None, chunk


def test_scan_write_containment_denies_canary_stripped_padded_leak():
    source = "CANARY-ALPHA-NOVA-7F3K9 " + " ".join(
        f"sourceword{i}" for i in range(400)
    )  # 404 tokens -> 400 shingles
    fp = make_fp(103, source, project="alpha", sensitivity=2)
    assert fp["canaries"] == ["CANARY-ALPHA-NOVA-7F3K9"]
    excerpt = " ".join(f"sourceword{i}" for i in range(100, 160))  # 60 words
    padding = " ".join(f"padword{i}" for i in range(40))  # 40 diverse words
    leak = f"{excerpt} {padding}"  # 100 tokens -> 96 shingles, no canary
    leak_shingles = shingles(leak)
    fp_shingles = set(fp["shingles"])
    assert "CANARY-ALPHA-NOVA-7F3K9" not in leak
    assert extract_canaries(leak) == []
    assert content_hash(leak) != fp["hash"]
    # Intersection is the 56 excerpt shingles: 56/440 ~= 0.1273 Jaccard,
    # 56/min(96, 400) ~= 0.5833 containment.
    assert round(jaccard(leak_shingles, fp_shingles), 4) == 0.1273
    assert jaccard(leak_shingles, fp_shingles) < DENY_THRESHOLD
    assert round(_containment(leak_shingles, fp_shingles), 4) == 0.5833
    assert _containment(leak_shingles, fp_shingles) >= CONTAINMENT_THRESHOLD
    result = scan_write(leak, [fp], run_project="beta", target_sensitivity=0)
    assert result["action"] == "deny"
    assert result["policy_id"] == "detect-suspicious-overlap"
    assert result["matched_fingerprint_id"] == 103
    assert result["matched_canary"] is None
    assert result["similarity"] == 0.1273
    assert "58.3%" in result["reason"]


def test_scan_write_containment_allows_text_sharing_one_shingle():
    fp = make_fp(104, GARDEN_BASE, project="alpha", sensitivity=2)
    fp_shingles = set(fp["shingles"])
    clean = (
        "the quick brown fox jumps purple banana telescope umbrella "
        "sandwich guitar river"
    )  # 12 tokens -> 8 shingles, only the first is in the fingerprint
    clean_shingles = shingles(clean)
    assert len(clean_shingles & fp_shingles) == 1
    # 1/min(8, 32) = 0.125 containment; 1/39 ~= 0.0256 Jaccard.
    assert round(_containment(clean_shingles, fp_shingles), 4) == 0.125
    assert _containment(clean_shingles, fp_shingles) < CONTAINMENT_THRESHOLD
    assert round(jaccard(clean_shingles, fp_shingles), 4) == 0.0256
    result = scan_write(clean, [fp], run_project="beta", target_sensitivity=0)
    assert result["action"] == "allow"
    assert result["policy_id"] == "detect-clean"
    assert result["matched_fingerprint_id"] is None
    assert result["matched_canary"] is None
    assert result["similarity"] == 0.0256


def test_scan_write_containment_does_not_preempt_jaccard_suspicious_rung():
    # Existing synthetic fixture: content fully contained in the fingerprint
    # (containment 1.0) but Jaccard 16/76 ~= 0.2105 hits the earlier rung, so
    # the verdict must be unchanged (Jaccard reason, exact similarity).
    content = "a b c d e f g h i j k l m n o p q r s t"  # 20 tokens -> 16 shingles
    content_sh = shingles(content)
    fp = synthetic_fp(105, content_sh, n_extra=60)
    fp_shingles = set(fp["shingles"])
    assert _containment(content_sh, fp_shingles) == 1.0
    assert round(jaccard(content_sh, fp_shingles), 4) == 0.2105
    result = scan_write(content, [fp], run_project="beta", target_sensitivity=0)
    assert result["action"] == "deny"
    assert result["policy_id"] == "detect-suspicious-overlap"
    assert result["matched_fingerprint_id"] == 105
    assert result["matched_canary"] is None
    assert result["similarity"] == 0.2105
    assert "0.2105" in result["reason"]
    assert "containment" not in result["reason"]
