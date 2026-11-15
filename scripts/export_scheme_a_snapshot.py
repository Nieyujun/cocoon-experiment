"""导出方案A已核验签发快照（只读输入，只写 ai_assisted_review_20261110/）。

用法（项目根目录）：
    python scripts/export_scheme_a_snapshot.py
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cocoon import ai_assisted_review as ar  # noqa: E402
from cocoon.runtime import utc_now, write_json_new, canonical_hash  # noqa: E402

SOURCE_ROOT = ROOT / "experiment/evidence_candidates/v1_20261108"
BUILD = SOURCE_ROOT / "build_20261108T074108_f38ec5"
TEMPLATE = ROOT / "experiment/review/cross_topic_20261108/human_review_checklist.json"
SEMANTIC = ROOT / "experiment/review/live_semantic_20261108.json"
OUT_DIR = ROOT / "experiment/review/ai_assisted_review_20261110"
OUT_PATH = OUT_DIR / "scheme_a_merged_verified.json"


def main():
    import hashlib
    template_sha = hashlib.sha256(TEMPLATE.read_bytes()).hexdigest()
    merge = ar.run_scheme_a_merge_and_verify(
        TEMPLATE, evidence_build=BUILD, live_semantic_path=SEMANTIC)
    gate_locked = ar.evaluate_ai_assisted_release_gate(SOURCE_ROOT)
    gate_unlocked = ar.evaluate_ai_assisted_release_gate(
        SOURCE_ROOT, allow_ai_assisted_release=True)
    document = {
        "snapshot_contract": "scheme_a_signed_snapshot_v1",
        "saved_at_utc": utc_now(),
        "review_origin": ar.REVIEW_ORIGIN,
        "real_human_ca_signature_verified": False,
        "identity_authentication_scope":
            "local_format_independence_only_not_real_human_authentication",
        "source_template_path":
            "experiment/review/cross_topic_20261108/human_review_checklist.json",
        "source_template_sha256": template_sha,
        "source_template_unchanged": hashlib.sha256(TEMPLATE.read_bytes()).hexdigest()
                                     == template_sha,
        "merged_verified": merge,
        "release_gate_default": gate_locked,
        "release_gate_authorized": gate_unlocked,
    }
    document["snapshot_sha256"] = canonical_hash(document)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if OUT_PATH.exists():
        OUT_PATH.unlink()
    write_json_new(OUT_PATH, document)
    print({
        "path": str(OUT_PATH),
        "bytes": OUT_PATH.stat().st_size,
        "snapshot_sha256": document["snapshot_sha256"],
        "merged_sha256": merge["merged_sha256"],
        "human_slots": merge["human_slots_completed"],
        "items": merge["items_total"],
        "disagreements": merge["disagreement_count"],
        "unlocked": gate_unlocked["pilot_release_unlocked"],
    })


if __name__ == "__main__":
    main()
