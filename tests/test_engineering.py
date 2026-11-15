import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cocoon.configuration import load_config
from cocoon.engineering import dry_run
from cocoon.runtime import make_intervention_pair, verify_events
from scripts.intake_averitec import count_and_select, make_candidate, topic_checks

ROOT = Path(__file__).resolve().parents[1]


class EngineeringTests(unittest.TestCase):
    def test_fixture_length_regression(self):
        packet = {"packet_id": "fixture", "evidence": [{"evidence_id": "E1", "text": "【合成夹具】纸环沙盒每班四个座位、两班共八个座位；此为虚构规则。", "source_url": "synthetic://fixture/rules", "source_family_id": "fixture_rules"}]}
        unit = "【仅用于等长契约测试，不是助手输出】请根据已列出的虚构规则核对文字，不引入外部事实。"
        self.assertFalse(make_intervention_pair(packet, unit * 12, unit * 12)["checks"]["dose_within_range"])
        pair = make_intervention_pair(packet, unit * 14, unit * 14)
        self.assertEqual(pair["I4"]["char_count"], 677)
        self.assertTrue(pair["checks"]["dose_within_range"])
        self.assertEqual(pair["checks"]["semantic_review"], "pending")

    def test_failure_preserved_no_demo_fallback(self):
        root = Path(tempfile.mkdtemp(prefix="engineering-test-", dir=ROOT / "tests"))
        (root / "experiment").mkdir()
        c = load_config(ROOT / "configs/core.json")
        with patch("cocoon.engineering.make_population", side_effect=ValueError("injected_failure")):
            with self.assertRaisesRegex(ValueError, "injected_failure"):
                dry_run(c, root)
        run = next((root / "experiment/demo").iterdir())
        failure = json.loads((run / "failure.json").read_text(encoding="utf-8"))
        self.assertEqual(failure["status"], "failed")
        self.assertEqual(failure["actual_model_calls"], 0)
        self.assertFalse((run / "summary.json").exists())
        self.assertEqual(verify_events(run)["event_count"], 2)
        events = [json.loads(x) for x in (run / "events/events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(events[-1]["event_type"], "engineering_run_failed")


class IntakeOfflineTests(unittest.TestCase):
    def test_only_designated_candidate(self):
        count, record = count_and_select(b'[{"claim":"actor letter refusing advertisement"},{"unused":true}]', 0)
        self.assertEqual(count, 2)
        self.assertTrue(all(topic_checks(record["claim"]).values()))
        with self.assertRaises(ValueError):
            count_and_select(b'[{},{}]', 1)

    def test_candidate_never_auto_approved(self):
        record = {"claim": "test", "label": "private", "justification": "private", "questions": []}
        c = make_candidate(record, {"url": "https://example.invalid/data", "sha256": "0" * 64, "retrieved_at_utc": "fixture"})
        self.assertFalse(c["approved_for_experiment"])
        self.assertNotIn("label", c)
        self.assertEqual(c["review_status"], "pending_dual_human_review")

    def test_malformed_array_rejected(self):
        for data in (b'[]', b'[{},]', b'[{', b'[{}]trailing'):
            with self.subTest(data=data), self.assertRaises(ValueError):
                count_and_select(data, 0)


if __name__ == "__main__":
    unittest.main()
