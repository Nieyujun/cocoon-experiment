"""指标的手算与边界测试；合成记录仅用于单元测试，不是正式分析。"""

import copy
import math
import random
import unittest
from unittest.mock import patch

from cocoon.metrics import (
    coverage_score,
    exposure_contrasts,
    exposure_metrics,
    holm,
    interaction_rate,
    paired_summary,
    score_answers,
    understanding_score,
)


class AnswerScoreTests(unittest.TestCase):
    def test_hand_calculated_complete_scores(self):
        gold = {"a": 1, "b": 1, "c": 0, "d": 0}
        answers = [
            {"item_id": "a", "p_true": 0.8, "choice": False},
            {"item_id": "b", "p_true": 0.2},
            {"item_id": "c", "p_true": 0.5, "choice": False},
            {"item_id": "d", "p_true": 0.9, "choice": 1},
        ]
        result = score_answers(answers, gold)
        self.assertTrue(result["complete"])
        self.assertEqual((result["expected_n"], result["observed_n"]), (4, 4))
        self.assertEqual(result["missing_ids"], [])
        self.assertAlmostEqual(result["BS"], 0.435)
        self.assertEqual(result["Acc"], 25)
        self.assertEqual(result["S"], 50)
        self.assertEqual(result["uncertain_count"], 1)
        self.assertEqual(result["choice_inconsistencies"], 2)
        self.assertEqual(result["BS_lower"], result["BS"])
        self.assertEqual(result["BS_upper"], result["BS"])

    def test_half_probability_always_predicts_true(self):
        result = score_answers([
            {"item_id": "true", "p_true": 0.5},
            {"item_id": "false", "p_true": 0.5, "choice": False},
        ], {"true": 1, "false": 0})
        self.assertEqual(result["Acc"], 50)
        self.assertEqual(result["BS"], 0.25)
        self.assertEqual(result["S"], 0)
        self.assertEqual(result["uncertain_count"], 2)
        self.assertEqual(result["choice_inconsistencies"], 1)

    def test_confidence_threshold_is_inclusive_on_both_sides(self):
        result = score_answers([
            {"item_id": "a", "p_true": 0.8},
            {"item_id": "b", "p_true": 0.2},
            {"item_id": "c", "p_true": 0.799},
            {"item_id": "d", "p_true": 0.201},
        ], {"a": 0, "b": 1, "c": 0, "d": 1})
        self.assertEqual(result["Acc"], 0)
        self.assertEqual(result["S"], 50)

    def test_probability_endpoints(self):
        for truth, probability, bs, accuracy, overconfidence in (
            (0, 0, 0, 100, 0), (1, 1, 0, 100, 0),
            (0, 1, 1, 0, 100), (1, 0, 1, 0, 100),
        ):
            with self.subTest(truth=truth, probability=probability):
                result = score_answers([{"item_id": "q", "p_true": probability}], {"q": truth})
                self.assertEqual((result["BS"], result["Acc"], result["S"]),
                                 (bs, accuracy, overconfidence))

    def test_missing_bounds_keep_fixed_denominator(self):
        gold = {"a": 1, "b": 0, "c": 1, "d": 0}
        answers = [{"item_id": "a", "p_true": 0.5}, {"item_id": "d", "p_true": 1}]
        before = copy.deepcopy((answers, gold))
        result = score_answers(answers, gold)
        self.assertFalse(result["complete"])
        self.assertEqual((result["expected_n"], result["observed_n"]), (4, 2))
        self.assertEqual(result["missing_ids"], ["b", "c"])
        for key in ("BS", "Acc", "S"):
            self.assertIsNone(result[key])
        self.assertEqual(result["BS_lower"], 1.25 / 4)
        self.assertEqual(result["BS_upper"], 3.25 / 4)
        self.assertEqual((answers, gold), before)

    def test_all_answers_missing(self):
        result = score_answers([], {"a": 0, "b": 1})
        self.assertEqual((result["BS_lower"], result["BS_upper"]), (0, 1))
        self.assertEqual(result["observed_n"], 0)
        self.assertFalse(result["complete"])
        self.assertTrue(all(result[key] is None for key in ("BS", "Acc", "S")))

    def test_duplicate_unknown_and_malformed_ids_rejected(self):
        answer = {"item_id": "q", "p_true": 0.7}
        for answers in ([answer, answer], [{"item_id": "unknown", "p_true": 0.7}],
                        [{"item_id": [], "p_true": 0.7}], [{"p_true": 0.7}], [None]):
            with self.subTest(answers=answers), self.assertRaises(ValueError):
                score_answers(answers, {"q": 1})

    def test_invalid_probabilities_rejected(self):
        for value in (float("nan"), float("inf"), -float("inf"), True, False,
                      -0.001, 1.001, None, "0.5", [], 10 ** 1000):
            with self.subTest(value=value), self.assertRaises(ValueError):
                score_answers([{"item_id": "q", "p_true": value}], {"q": 1})
        with self.assertRaises(ValueError):
            score_answers([{"item_id": "q"}], {"q": 1})

    def test_invalid_choice_and_gold_rejected(self):
        for choice in (None, "true", 2, 0.0, [], float("nan")):
            with self.subTest(choice=choice), self.assertRaises(ValueError):
                score_answers([{"item_id": "q", "p_true": 0.7, "choice": choice}], {"q": 1})
        for gold in ({}, {"q": True}, {"q": 2}, {"q": 1.0}, {"q": None}, {1: 1}, {"": 0}):
            with self.subTest(gold=gold), self.assertRaises(ValueError):
                score_answers([], gold)
        with self.assertRaises(ValueError):
            score_answers((), {"q": 1})


class DimensionScoreTests(unittest.TestCase):
    def test_coverage_union_excludes_mastered_from_recognized_count(self):
        result = coverage_score([
            {"mastered": True, "recognized_gap": True},
            {"mastered": True, "recognized_gap": False},
            {"mastered": False, "recognized_gap": True},
            {"mastered": False, "recognized_gap": True},
            {"mastered": False, "recognized_gap": False},
            {"mastered": False, "recognized_gap": False},
        ])
        for value in result.values():
            self.assertAlmostEqual(value, 100 / 3)
        self.assertAlmostEqual(sum(result.values()), 100)

    def test_coverage_extremes(self):
        for mastered, gap, expected in (
            (True, True, (0, 100, 0)),
            (False, True, (0, 0, 100)),
            (False, False, (100, 0, 0)),
        ):
            with self.subTest(mastered=mastered, gap=gap):
                result = coverage_score([{"mastered": mastered, "recognized_gap": gap}] * 6)
                self.assertEqual(tuple(result[key] for key in
                                       ("O", "mastered_rate", "recognized_gap_rate")), expected)

    def test_coverage_requires_six_boolean_dimensions(self):
        good = {"mastered": False, "recognized_gap": False}
        bad_dimensions = [[], [good] * 5, [good] * 7, tuple([good] * 6),
                          [good] * 5 + [None], [good] * 5 + [{"mastered": False}]]
        for key in good:
            for value in (0, 1, None, "false"):
                bad_dimensions.append([good] * 5 + [{**good, key: value}])
        for dimensions in bad_dimensions:
            with self.subTest(dimensions=dimensions), self.assertRaises(ValueError):
                coverage_score(dimensions)

    def test_understanding_hand_calculation_and_limits(self):
        self.assertEqual(understanding_score([2, 1, 0]), 50.0)
        self.assertEqual(understanding_score([0, 0, 0]), 0.0)
        self.assertEqual(understanding_score([2, 2, 2]), 100.0)
        self.assertIsInstance(understanding_score([2, 1, 0]), float)

    def test_understanding_rejects_invalid_parts(self):
        for parts in ([], [0, 1], [0] * 4, (0, 1, 2), [True, 0, 0],
                      [False, 0, 0], [2.0, 0, 0], [-1, 0, 0], [3, 0, 0], [None, 0, 0]):
            with self.subTest(parts=parts), self.assertRaises(ValueError):
                understanding_score(parts)


class ExposureTests(unittest.TestCase):
    def test_entropy_uniform_three_categories(self):
        result = exposure_metrics([-1, 0, 1, -1, 0, 1], -1)
        self.assertAlmostEqual(result["Hn"], 1)
        self.assertAlmostEqual(result["X"], 100 / 3)

    def test_balanced_exposure_counts_only_opposite_stance(self):
        for initial_stance in (-1, 1):
            with self.subTest(initial_stance=initial_stance):
                result = exposure_metrics([1, 1, -1, -1, 0, 0], initial_stance)
                self.assertAlmostEqual(result["X"], 100 / 3)
                self.assertAlmostEqual(result["Hn"], 1)

    def test_homogeneous_exposure_excludes_neutral_from_opposition(self):
        for stances, initial_stance in (([1, 1, 1, 1, -1, 0], 1),
                                        ([-1, -1, -1, -1, 1, 0], -1)):
            with self.subTest(initial_stance=initial_stance):
                result = exposure_metrics(stances, initial_stance)
                self.assertAlmostEqual(result["X"], 100 / 6)

    def test_all_neutral_exposure_has_zero_opposition(self):
        for initial_stance in (-1, 1):
            with self.subTest(initial_stance=initial_stance):
                self.assertEqual(exposure_metrics([0] * 6, initial_stance), {"Hn": 0, "X": 0})

    def test_entropy_uses_three_categories_when_only_two_observed(self):
        result = exposure_metrics([-1, 1], -1)
        self.assertAlmostEqual(result["Hn"], 1 / math.log2(3))
        self.assertEqual(result["X"], 50)

    def test_one_category_and_no_exposure(self):
        self.assertEqual(exposure_metrics([1, 1], 1), {"Hn": 0, "X": 0})
        self.assertEqual(exposure_metrics([1, 1], -1), {"Hn": 0, "X": 100})
        for initial_stance in (-1, 1):
            self.assertEqual(exposure_metrics([], initial_stance), {"Hn": None, "X": None})

    def test_invalid_stances_rejected(self):
        for stance in (True, False, 1.0, 2, -2, None, float("nan")):
            with self.subTest(stance=stance):
                with self.assertRaises(ValueError):
                    exposure_metrics([stance], 1)
                with self.assertRaises(ValueError):
                    exposure_metrics([], stance)
        with self.assertRaises(ValueError):
            exposure_metrics((0, 1), 1)

    def test_neutral_initial_stance_rejected_even_without_exposure(self):
        for stances in ([], [0, 1, -1]):
            with self.subTest(stances=stances), self.assertRaises(ValueError):
                exposure_metrics(stances, 0)

    def test_interaction_counts_events_without_deduplication(self):
        stances = {"a": -1, "b": -1, "c": 1}
        edges = [("a", "b"), ("a", "c"), ("a", "c"), ("c", "c")]
        self.assertEqual(interaction_rate(edges, stances), 50)
        self.assertIsNone(interaction_rate([], stances))
        self.assertIsNone(interaction_rate([], {}))

    def test_invalid_interactions_rejected(self):
        for edges in ([("a", "unknown")], [("a",)], [["a", "b"]], [("a", [])], None):
            with self.subTest(edges=edges), self.assertRaises(ValueError):
                interaction_rate(edges, {"a": -1, "b": 1})
        for stance in (True, False, 1.0, 2, -2, None, float("nan")):
            with self.subTest(stance=stance), self.assertRaises(ValueError):
                interaction_rate([], {"a": stance})

    def test_neutral_interaction_group_rejected_even_without_events(self):
        for edges in ([], [("a", "b")]):
            with self.subTest(edges=edges), self.assertRaises(ValueError):
                interaction_rate(edges, {"a": 1, "b": 0})

    def test_factorial_contrasts_hand_calculation(self):
        changes = {"A": 1, "B": 3, "C": 5, "D": 9}
        self.assertEqual(exposure_contrasts(changes),
                         {"homogeneous": 5.0, "misinformation": 3.0, "interaction": 2.0})
        self.assertEqual(exposure_contrasts({key: 4 for key in "ABCD"}),
                         {"homogeneous": 0.0, "misinformation": 0.0, "interaction": 0.0})

    def test_contrasts_reject_missing_extra_and_invalid_values(self):
        for changes in ({}, {"A": 0, "B": 0, "C": 0}, dict.fromkeys("ABCDE", 0)):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                exposure_contrasts(changes)
        for value in (True, float("nan"), float("inf"), "1", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                exposure_contrasts({"A": value, "B": 0, "C": 0, "D": 0})


class HolmTests(unittest.TestCase):
    def test_hand_calculation_is_monotone_not_naive_multiplication(self):
        result = holm({"third": 0.04, "first": 0.01, "second": 0.03})
        self.assertEqual(list(result), ["third", "first", "second"])
        self.assertAlmostEqual(result["first"], 0.03)
        self.assertAlmostEqual(result["second"], 0.06)
        self.assertAlmostEqual(result["third"], 0.06)

    def test_ties_endpoints_clamping_and_empty_family(self):
        self.assertEqual(holm({}), {})
        self.assertEqual(holm({"one": 0.2}), {"one": 0.2})
        self.assertEqual(holm({"a": 0.5, "b": 0.5, "c": 0.9}),
                         {"a": 1.0, "b": 1.0, "c": 1.0})
        self.assertEqual(holm({"a": 0.0, "b": 1.0}), {"a": 0.0, "b": 1.0})
        self.assertEqual(holm({"a": 0.01, "b": 0.01}), {"a": 0.02, "b": 0.02})

    def test_invalid_p_values_rejected(self):
        for value in (True, False, float("nan"), float("inf"), -float("inf"),
                      -0.1, 1.1, None, "0.05"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                holm({"comparison": value})
        for values in ([], {1: 0.5}, {"": 0.5}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                holm(values)


class PairedSummaryTests(unittest.TestCase):
    topics = ["t1", "t2", "t3"]

    @staticmethod
    def row(topic, block, arm, t1, t3, valid=True, **extras):
        return {"topic_id": topic, "block_id": block, "arm": arm,
                "T1": t1, "T3": t3, "valid": valid, **extras}

    def constant_records(self):
        records = []
        for topic, n, effect in zip(self.topics, (1, 2, 4), (0.125, 0.25, 0.375)):
            for block in range(n):
                records.extend([
                    self.row(topic, block, "I0", 0.5, 0.5),
                    self.row(topic, block, "I4", 0.5, 0.5 - effect),
                    self.row(topic, block, "I5", 0.5, 0.375),
                ])
        return records

    def test_equal_topic_weight_despite_unequal_pair_counts(self):
        result = paired_summary(self.constant_records(), self.topics)
        self.assertEqual(result["bootstrap_reps"], 10000)
        self.assertEqual(result["target_topics"], self.topics)
        comparison = result["comparisons"]["I4_vs_I0"]
        self.assertTrue(comparison["estimable"])
        self.assertAlmostEqual(comparison["effect"], 0.25)
        self.assertNotAlmostEqual(comparison["effect"], (0.125 + 2 * 0.25 + 4 * 0.375) / 7)
        self.assertEqual(comparison["n"], 7)
        self.assertEqual(comparison["count_missing"], 0)
        self.assertEqual(comparison["coverage"], 1)
        self.assertEqual([comparison["per_topic"][topic]["n"] for topic in self.topics], [1, 2, 4])
        for endpoint in comparison["ci_95"]:
            self.assertAlmostEqual(endpoint, 0.25)
        self.assertAlmostEqual(comparison["control"]["T1"], 0.5)
        self.assertAlmostEqual(comparison["treatment"]["T3"], 0.25)
        other = result["comparisons"]["I4_vs_I5"]
        self.assertAlmostEqual(other["effect"], 0.125)
        for endpoint in other["ci_95"]:
            self.assertAlmostEqual(endpoint, 0.125)

    def test_effect_direction_with_shared_baseline(self):
        records = []
        for topic in self.topics:
            records.extend([
                self.row(topic, "b", "I0", 0.5, 0.375),
                self.row(topic, "b", "I4", 0.5, 0.125),
                self.row(topic, "b", "I5", 0.5, 0.25),
            ])
        result = paired_summary(records, self.topics, bootstrap_reps=20)
        self.assertAlmostEqual(result["comparisons"]["I4_vs_I0"]["effect"], 0.25)
        self.assertAlmostEqual(result["comparisons"]["I4_vs_I5"]["effect"], 0.125)

    def test_mismatched_baselines_rejected_for_either_control_or_i4(self):
        for arm in ("I0", "I4", "I5"):
            for t1 in (0.375, 0.625, 0.5 - 2e-12, 0.5 + 2e-12):
                records = self.constant_records()
                for row in records:
                    if row["topic_id"] == "t1" and row["arm"] == arm:
                        row["T1"] = t1
                with self.subTest(arm=arm, t1=t1), self.assertRaisesRegex(ValueError, "共享 T1"):
                    paired_summary(records, self.topics, bootstrap_reps=1)

    def test_shared_t1_absolute_tolerance_is_inclusive(self):
        for control in ("I0", "I5"):
            for control_t1, treatment_t1 in ((0, 1e-12), (1e-12, 0), (0.5, 0.5 + 5e-13)):
                records = []
                for topic in self.topics:
                    for arm in ("I0", "I4", "I5"):
                        t1 = control_t1 if arm == control else treatment_t1
                        records.append(self.row(topic, "b", arm, t1, 0))
                with self.subTest(control=control, control_t1=control_t1,
                                  treatment_t1=treatment_t1):
                    result = paired_summary(records, self.topics, bootstrap_reps=1)
                    comparison = result["comparisons"][f"I4_vs_{control}"]
                    self.assertTrue(comparison["estimable"])
                    self.assertEqual(comparison["n"], 3)
                    self.assertAlmostEqual(comparison["effect"], treatment_t1 - control_t1,
                                           delta=1e-16)

    def test_shared_t1_rejects_next_float_above_tolerance(self):
        above_tolerance = math.nextafter(1e-12, math.inf)
        for control in ("I0", "I5"):
            for control_t1, treatment_t1 in ((0, above_tolerance), (above_tolerance, 0)):
                records = [self.row("t1", "b", control, control_t1, 0),
                           self.row("t1", "b", "I4", treatment_t1, 0)]
                with self.subTest(control=control, control_t1=control_t1,
                                  treatment_t1=treatment_t1), self.assertRaisesRegex(ValueError, "共享 T1"):
                    paired_summary(records, self.topics, bootstrap_reps=1)

    def test_bs_measurements_require_finite_nonbool_values_in_unit_interval(self):
        for timepoint in ("T1", "T3"):
            for value in (-0.001, 1.001, math.nextafter(0, -math.inf), math.nextafter(1, math.inf),
                          float("nan"), float("inf"), -float("inf"), True, False):
                for valid in (True, False):
                    record = self.row("t1", "b", "I4", 0.5, 0.25, valid=valid)
                    record[timepoint] = value
                    with self.subTest(timepoint=timepoint, value=value, valid=valid), self.assertRaisesRegex(
                            ValueError, f"BS {timepoint}"):
                        paired_summary([record], self.topics, bootstrap_reps=1)

    def test_bs_zero_and_one_endpoints_are_valid(self):
        for t1 in (0, 1):
            records = []
            for topic in self.topics:
                records.extend([self.row(topic, "b", "I0", t1, 1),
                                self.row(topic, "b", "I4", t1, 0),
                                self.row(topic, "b", "I5", t1, 1)])
            with self.subTest(t1=t1):
                result = paired_summary(records, self.topics, bootstrap_reps=1)
                for comparison in result["comparisons"].values():
                    self.assertTrue(comparison["estimable"])
                    self.assertAlmostEqual(comparison["effect"], 1)

    def test_invalid_and_missing_measurements_stay_in_coverage_denominator(self):
        records = self.constant_records() + [
            self.row("t1", "invalid", "I0", 0.5, 0.5),
            self.row("t1", "invalid", "I4", 0.5, 0.0, valid=False),
            self.row("t1", "null", "I0", 0.5, 0.5),
            self.row("t1", "null", "I4", 0.5, None),
            self.row("t1", "absent", "I0", 0.5, 0.5),
        ]
        comparison = paired_summary(records, self.topics, 20)["comparisons"]["I4_vs_I0"]
        topic = comparison["per_topic"]["t1"]
        self.assertEqual((topic["n"], topic["expected_n"], topic["count_missing"]), (1, 4, 3))
        self.assertEqual(topic["coverage"], 0.25)
        self.assertEqual(comparison["n"], 7)
        self.assertEqual(comparison["count_missing"], 3)
        self.assertEqual(comparison["coverage"], 0.7)
        self.assertAlmostEqual(comparison["effect"], 0.25)

    def test_missing_timepoint_key_counts_as_missing_not_neutral(self):
        records = self.constant_records()
        del records[1]["T3"]
        result = paired_summary(records, self.topics, 20)
        for comparison in result["comparisons"].values():
            self.assertFalse(comparison["estimable"])
            self.assertEqual(comparison["per_topic"]["t1"]["count_missing"], 1)
            self.assertIsNone(comparison["effect"])

    def test_completely_absent_target_never_shrinks_topic_set(self):
        records = [row for row in self.constant_records() if row["topic_id"] != "t3"]
        result = paired_summary(records, self.topics, 20)
        for comparison in result["comparisons"].values():
            self.assertFalse(comparison["estimable"])
            self.assertEqual(comparison["missing_topics"], ["t3"])
            self.assertEqual(list(comparison["per_topic"]), self.topics)
            self.assertEqual(comparison["per_topic"]["t3"]["n"], 0)
            self.assertIsNone(comparison["per_topic"]["t3"]["coverage"])
            for key in ("effect", "ci_95", "control", "treatment", "p_raw", "p_adjusted"):
                self.assertIsNone(comparison[key])

    def test_all_invalid_topic_blocks_estimation(self):
        records = self.constant_records()
        for row in records:
            if row["topic_id"] == "t2" and row["arm"] == "I4":
                row["valid"] = False
        result = paired_summary(records, self.topics, 20)
        for comparison in result["comparisons"].values():
            self.assertFalse(comparison["estimable"])
            topic = comparison["per_topic"]["t2"]
            self.assertEqual((topic["n"], topic["count_missing"], topic["coverage"]), (0, 2, 0))
            self.assertIsNone(comparison["effect"])

    def test_controls_from_different_blocks_must_not_be_paired(self):
        records = []
        for topic in self.topics:
            records.extend([self.row(topic, "a", "I0", 0.5, 0.5),
                            self.row(topic, "b", "I4", 0.5, 0.25)])
        comparison = paired_summary(records, self.topics, 20)["comparisons"]["I4_vs_I0"]
        self.assertFalse(comparison["estimable"])
        self.assertEqual(comparison["n"], 0)
        self.assertEqual(comparison["count_missing"], 6)

    def test_comparisons_have_independent_completeness(self):
        records = [row for row in self.constant_records()
                   if not (row["topic_id"] == "t3" and row["arm"] == "I5")]
        result = paired_summary(records, self.topics, 20)
        self.assertTrue(result["comparisons"]["I4_vs_I0"]["estimable"])
        self.assertFalse(result["comparisons"]["I4_vs_I5"]["estimable"])
        self.assertEqual(result["comparisons"]["I4_vs_I5"]["per_topic"]["t3"]["count_missing"], 4)

    def test_empty_records_not_estimable(self):
        result = paired_summary([], self.topics, 1)
        for comparison in result["comparisons"].values():
            self.assertFalse(comparison["estimable"])
            self.assertEqual(comparison["missing_topics"], self.topics)
            self.assertEqual(comparison["n"], 0)
            self.assertIsNone(comparison["coverage"])

    def test_bootstrap_preserves_network_pairs(self):
        records = []
        for topic in self.topics:
            for block, control_change in enumerate((-0.25, 0, 0.25)):
                records.extend([
                    self.row(topic, block, "I0", 0.5, 0.5 + control_change),
                    self.row(topic, block, "I4", 0.5, 0.375 + control_change),
                    self.row(topic, block, "I5", 0.5, 0.5 + control_change),
                ])
        result = paired_summary(records, self.topics, 200)
        for comparison in result["comparisons"].values():
            self.assertAlmostEqual(comparison["effect"], 0.125)
            for endpoint in comparison["ci_95"]:
                self.assertAlmostEqual(endpoint, 0.125)

    def test_bootstrap_percentile_interpolation_and_fixed_stratum_sizes(self):
        records = []
        for topic, effects in zip(self.topics, ([0, 1], [0], [0])):
            for block, effect in enumerate(effects):
                records.extend([
                    self.row(topic, block, "I0", 0, effect),
                    self.row(topic, block, "I4", 0, 0),
                    self.row(topic, block, "I5", 0, effect),
                ])
        # 两次抽样分别只取各层首值/末值，得到总体效应0与1/3。
        with patch("cocoon.metrics.random.Random") as random_class:
            selections = iter([0] * 4 + [-1] * 4 + [0] * 4 + [-1] * 4)
            random_class.return_value.choice.side_effect = lambda values: values[next(selections)]
            result = paired_summary(records, self.topics, bootstrap_reps=2)
            calls = random_class.return_value.choice.call_args_list
            self.assertEqual([len(call.args[0]) for call in calls], [2, 2, 1, 1] * 4)
        for comparison in result["comparisons"].values():
            self.assertAlmostEqual(comparison["effect"], 1 / 6)
            self.assertAlmostEqual(comparison["ci_95"][0], 0.025 / 3)
            self.assertAlmostEqual(comparison["ci_95"][1], 0.975 / 3)

    def test_seed_reproducibility_input_order_and_no_mutation(self):
        records = self.constant_records()
        records[-2]["T3"] = 0.0
        before = copy.deepcopy(records)
        rng_state = random.getstate()
        first = paired_summary(records, self.topics, 100, seed=17)
        second = paired_summary(list(reversed(records)), self.topics, 100, seed=17)
        self.assertEqual(first, second)
        self.assertEqual(records, before)
        self.assertEqual(random.getstate(), rng_state)
        for comparison in first["comparisons"].values():
            self.assertLess(comparison["ci_95"][0], comparison["ci_95"][1])

    def test_p_values_absent_without_actual_allocation_and_fixed_topic_note(self):
        result = paired_summary(self.constant_records(), self.topics, 20)
        self.assertIn("allocation", result["p_value_note"])
        self.assertIn("sign-flip", result["p_value_note"])
        self.assertIn("三个议题是固定集合", result["note"])
        self.assertIn("完全未提交的区组无法从这些记录推知", result["note"])
        self.assertIn("不能将该覆盖率正式解读为全实验网络覆盖率", result["note"])
        for comparison in result["comparisons"].values():
            self.assertIsNone(comparison["p_raw"])
            self.assertIsNone(comparison["p_adjusted"])

    def test_demo_record_anywhere_rejected_even_outside_targets(self):
        for topic, valid in (("t1", True), ("t1", False), ("outside", False)):
            records = self.constant_records() + [self.row(topic, "demo", "I1", None, None,
                                                        valid=valid, is_demo=True)]
            with self.subTest(topic=topic, valid=valid), self.assertRaisesRegex(ValueError, "is_demo"):
                paired_summary(records, self.topics, 20)

    def test_non_target_records_excluded_and_other_arms_retained_for_coverage(self):
        records = self.constant_records() + [
            self.row("outside", "extra", "I4", 0, 1),
            self.row("t1", "extra", "I1", None, None, valid=False),
        ]
        comparison = paired_summary(records, self.topics, 20)["comparisons"]["I4_vs_I0"]
        self.assertAlmostEqual(comparison["effect"], 0.25)
        self.assertEqual(comparison["per_topic"]["t1"]["coverage"], 0.5)
        self.assertEqual(comparison["count_missing"], 1)

    def test_duplicate_network_record_rejected(self):
        records = self.constant_records()
        for valid in (True, False):
            with self.subTest(valid=valid), self.assertRaisesRegex(ValueError, "重复"):
                paired_summary(records + [{**records[0], "valid": valid}], self.topics, 20)

    def test_invalid_record_fields_rejected(self):
        good = self.row("t1", 0, "I0", 0.5, 0.25)
        for key, values in (
            ("valid", (1, 0, None, "true")),
            ("is_demo", (1, "false", None)),
            ("T1", (True, float("nan"), "0.5")),
            ("T3", (False, float("inf"), -float("inf"))),
            ("block_id", (True, [], None, "")),
            ("topic_id", (None, [], "")),
            ("arm", (None, "I6", [])),
        ):
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    paired_summary([{**good, key: value}], self.topics, 20)
        with self.assertRaises(ValueError):
            paired_summary([None], self.topics, 20)
        with self.assertRaises(ValueError):
            paired_summary(tuple(), self.topics, 20)

    def test_invalid_topics_repetitions_and_seed_rejected(self):
        for topics in ([], ["t1", "t1"], [None], [True], [""], ("t1",)):
            with self.subTest(topics=topics), self.assertRaises(ValueError):
                paired_summary([], topics, 20)
        for repetitions in (0, -1, True, False, 1.5, None):
            with self.subTest(repetitions=repetitions), self.assertRaises(ValueError):
                paired_summary([], self.topics, repetitions)
        for seed in (True, False, 1.5, "0", None):
            with self.subTest(seed=seed), self.assertRaises(ValueError):
                paired_summary([], self.topics, 20, seed)


if __name__ == "__main__":
    unittest.main()
