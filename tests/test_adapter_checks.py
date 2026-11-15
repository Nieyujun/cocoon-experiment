from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from cocoon.assistant import AssistantAdapter, adapter_fingerprint
from cocoon.assistant_checks import (FixtureTransport, FIXTURE_SETTINGS, FIXTURE_VIEW, SCENARIOS,
                                    _verify_case_trace, fixture_packet)
from cocoon.prompts import load_prompt_bundle
from cocoon.reporting import render_adapter_report
from cocoon.runtime import EventStore, canonical_hash, verify_events, write_json_new

ROOT = Path(__file__).resolve().parents[1]
BUNDLE_PATH = ROOT / "configs/assistant_prompts_v1.json"


class AdapterTraceTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="adapter-trace-", dir=ROOT / "tests"))
        self.packet = fixture_packet()
        self.bundle = load_prompt_bundle(BUNDLE_PATH)
        self.fingerprint = adapter_fingerprint(self.bundle)

    def store(self, root=None):
        return EventStore(root or self.root, {}, canonical_hash(self.packet), True, "trace_check")

    def actual_run(self, scenario):
        store = self.store()
        transport = FixtureTransport(scenario[1], scenario[2])
        adapter = AssistantAdapter(store, BUNDLE_PATH, self.bundle["bundle_sha256"], FIXTURE_SETTINGS,
                                   transport, execution_mode="offline_fixture")
        result = adapter.run(self.packet, "trace-task", include_mirror=scenario[3],
                             user_view=FIXTURE_VIEW if scenario[3] else None)
        self.assertTrue(verify_events(self.root)["chain_valid"])
        return result

    def test_actual_five_stage_trace_passes(self):
        spec = SCENARIOS[0]
        result = self.actual_run(spec)
        _verify_case_trace(self.root, result, spec, self.fingerprint)

    def test_actual_failure_trace_passes(self):
        spec = next(s for s in SCENARIOS if s[0] == "integrate_unknown_citation")
        result = self.actual_run(spec)
        _verify_case_trace(self.root, result, spec, self.fingerprint)

    def test_terminal_claim_without_stage_execution_rejected(self):
        store = self.store()
        fabricated = {"status": "succeeded", "fixture_calls": 5, "task_id": "invented"}
        store.append("assistant_task_completed", task_id="invented", attempt_id=1,
                     payload=fabricated, status="success")
        self.assertTrue(verify_events(self.root)["chain_valid"])
        with self.assertRaises(ValueError):
            _verify_case_trace(self.root, fabricated, SCENARIOS[0], self.fingerprint)

    def test_result_edit_does_not_change_saved_original(self):
        result = self.actual_run(SCENARIOS[0])
        result["stage_results"]["search"]["raw_text"] = "different"
        with self.assertRaises(ValueError):
            _verify_case_trace(self.root, result, SCENARIOS[0], self.fingerprint)

    def test_wrong_fingerprint_rejected(self):
        result = self.actual_run(SCENARIOS[0])
        wrong = deepcopy(self.fingerprint)
        wrong["adapter_version"] = "other"
        with self.assertRaises(ValueError):
            _verify_case_trace(self.root, result, SCENARIOS[0], wrong)

    def test_failed_status_report_not_published_as_success(self):
        with self.assertRaises(ValueError):
            render_adapter_report(self.root / "should-not-exist.html", {"offline_verified": False})
        self.assertFalse((self.root / "should-not-exist.html").exists())


if __name__ == "__main__":
    unittest.main()
