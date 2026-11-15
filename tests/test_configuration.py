import copy
import math
from pathlib import Path
import unittest

from cocoon.configuration import call_plan, load_config, readiness, validate_config
from cocoon.metrics import exposure_metrics

ROOT = Path(__file__).resolve().parents[1]


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(ROOT / "configs/core.json")

    def test_core_call_count(self):
        plan = call_plan(self.config)
        self.assertEqual(plan["base_calls"], 25344)
        self.assertEqual(plan["exposure_worlds"], 72)
        self.assertEqual(plan["intervention_worlds"], 108)
        self.assertEqual(plan["blocks"], 18)
        self.assertEqual(plan["actual_calls"], 0)

    def test_pilot_count_separate(self):
        c = copy.deepcopy(self.config)
        c.update(topics=["SYNTHETIC"], blocks_per_topic=2, agents_per_world=8)
        self.assertEqual(call_plan(c)["base_calls"], 1408)
        self.assertEqual(call_plan(self.config)["base_calls"], 25344)

    def test_banned_shortcuts(self):
        for key in ("demo_fallback_allowed", "measurement_writes_to_memory", "generated_replies_reenter_primary_feed"):
            c = copy.deepcopy(self.config)
            c[key] = True
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_config(c)

    def test_formal_always_closed_at_bootstrap(self):
        c = copy.deepcopy(self.config)
        c["formal_launch_enabled"] = True
        state = readiness(c, ROOT)
        self.assertFalse(state["ready_for_formal"])
        self.assertFalse(state["ready_for_paid_pilot"])

    def test_budget_is_not_fabricated(self):
        state = readiness(self.config, ROOT)
        self.assertIsNone(state["plan"]["estimated_cost"])
        self.assertIn("pilot_budget", [x["code"] for x in state["blockers"]])

    def test_main_endpoint_cannot_change(self):
        c = copy.deepcopy(self.config)
        c["primary_endpoint"] = "BS_T2"
        with self.assertRaises(ValueError):
            validate_config(c)

    def test_exposure_diagnostic_excludes_neutral(self):
        self.assertAlmostEqual(exposure_metrics([1, 1, -1, -1, 0, 0], 1)["X"], 100 / 3)
        self.assertAlmostEqual(exposure_metrics([1, 1, 1, 1, -1, 0], 1)["X"], 100 / 6)


if __name__ == "__main__":
    unittest.main()
