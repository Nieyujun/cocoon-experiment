"""纯标准库工程测试；合成夹具和流程断言均不是研究数据或效果结果。"""

from collections import Counter, defaultdict
from copy import deepcopy
import inspect
import random
import re
import unittest
from unittest.mock import patch

from cocoon.scheduling import (
    allocate,
    make_fixture_cards,
    make_network,
    make_population,
    public_card,
    schedule_cards,
)


PUBLIC_KEYS = {
    "card_id", "topic_id", "text", "source_family_id", "source_url",
    "transformation_type",
}


def assert_schedule(test, rows, initial_stance, environment, rounds, max_repeats=2):
    test.assertEqual(len(rows), rounds)
    quotas = (2, 2, 2) if environment in ("A", "B") else (4, 1, 1)
    expected = dict(zip((initial_stance, -initial_stance, 0), quotas))
    uses = Counter()
    for row in rows:
        test.assertEqual(len(row), 6)
        test.assertEqual(Counter(card["stance"] for card in row), expected)
        test.assertEqual(len({card["card_id"] for card in row}), 6)
        test.assertEqual(
            sum(not card["is_accurate"] for card in row),
            2 if environment in ("B", "D") else 0,
        )
        uses.update(card["card_id"] for card in row)
    test.assertTrue(all(count <= max_repeats for count in uses.values()))


class TestPopulation(unittest.TestCase):
    def test_balanced_population_and_preferences(self):
        for n in (8, 12, 16, 24):
            with self.subTest(n=n):
                population = make_population(n, 37)
                self.assertEqual([p["agent_id"] for p in population], [f"S{i:02d}" for i in range(1, n + 1)])
                self.assertEqual(Counter(p["initial_stance"] for p in population), {1: n // 2, -1: n // 2})
                groups = {
                    stance: [p["preferences"] for p in population if p["initial_stance"] == stance]
                    for stance in (1, -1)
                }
                self.assertEqual(
                    Counter(tuple(sorted(p.items())) for p in groups[1]),
                    Counter(tuple(sorted(p.items())) for p in groups[-1]),
                )
                for group in groups.values():
                    for key in group[0]:
                        self.assertEqual(sorted(Counter(p[key] for p in group).values()), [n // 4, n // 4])
                for agent in population:
                    self.assertEqual(set(agent), {"agent_id", "initial_stance", "preferences"})
                    self.assertEqual(set(agent["preferences"]), {
                        "selection_preference", "source_sensitivity", "memory_span", "background",
                    })

    def test_reproducible_and_seed_changes(self):
        self.assertEqual(make_population(16, 11), make_population(16, 11))
        self.assertNotEqual(make_population(16, 11), make_population(16, 12))

    def test_profile_copies_are_independent(self):
        population = make_population(8, 2)
        original = deepcopy(population)
        self.assertEqual(len({id(p["preferences"]) for p in population}), 8)
        population[0]["preferences"]["background"] = "仅修改本副本"
        self.assertEqual(population[1:], original[1:])
        self.assertEqual(make_population(8, 2), original)

    def test_invalid_population_sizes(self):
        for n in (0, 4, 7, 9, 10, -8, 8.0, True, "8"):
            with self.subTest(n=n), self.assertRaises(ValueError):
                make_population(n, 1)


class TestNetwork(unittest.TestCase):
    def test_two_same_two_opposite_no_self_or_duplicate_edges(self):
        for n in (8, 16):
            for seed in range(12):
                with self.subTest(n=n, seed=seed):
                    agents = make_population(n, seed)
                    original = deepcopy(agents)
                    stances = {p["agent_id"]: p["initial_stance"] for p in agents}
                    edges = make_network(agents, seed)
                    self.assertEqual(len(edges), 4 * n)
                    self.assertEqual(len({tuple(e.values()) for e in edges}), 4 * n)
                    targets = defaultdict(list)
                    for edge in edges:
                        self.assertEqual(set(edge), {"follower_id", "followee_id"})
                        follower, followee = edge["follower_id"], edge["followee_id"]
                        self.assertNotEqual(follower, followee)
                        self.assertIn(followee, stances)
                        targets[follower].append(followee)
                    self.assertEqual(set(targets), set(stances))
                    for follower, followees in targets.items():
                        self.assertEqual(len(followees), 4)
                        self.assertEqual(sum(stances[t] == stances[follower] for t in followees), 2)
                    self.assertEqual(agents, original)

    def test_network_seed_and_copy_isolation(self):
        agents = make_population(8, 17)
        baseline = make_network(agents, 31)
        self.assertEqual(baseline, make_network(agents, 31))
        self.assertNotEqual(baseline, make_network(agents, 32))
        modified = make_network(agents, 31)
        modified[0]["followee_id"] = "仅修改副本"
        self.assertEqual(baseline, make_network(agents, 31))

    def test_invalid_network_inputs(self):
        agents = make_population(8, 3)
        duplicate = deepcopy(agents)
        duplicate[0]["agent_id"] = duplicate[1]["agent_id"]
        imbalanced = deepcopy(agents)
        next(p for p in imbalanced if p["initial_stance"] == -1)["initial_stance"] = 1
        invalid_stance = deepcopy(agents)
        invalid_stance[0]["initial_stance"] = True
        missing_id = deepcopy(agents)
        del missing_id[0]["agent_id"]
        for value in (None, [], agents[:7], duplicate, imbalanced, invalid_stance, missing_id, [None] * 8):
            with self.subTest(value=value), self.assertRaises(ValueError):
                make_network(value, 1)


class TestAllocation(unittest.TestCase):
    def test_uniform_shuffle_slots_and_distinct_response_seeds(self):
        arms = [f"I{i}" for i in range(6)]
        original = list(arms)
        allocations = allocate(arms, 83)
        self.assertEqual([a["slot"] for a in allocations], list(range(6)))
        self.assertEqual(Counter(a["arm"] for a in allocations), Counter(arms))
        self.assertEqual(len({a["response_seed"] for a in allocations}), 6)
        for entry in allocations:
            self.assertEqual(set(entry), {"arm", "slot", "response_seed"})
            self.assertIs(type(entry["response_seed"]), int)
            self.assertTrue(0 <= entry["response_seed"] < 2**32)
        self.assertEqual(arms, original)
        self.assertEqual(allocations, allocate(arms, 83))
        self.assertNotEqual(allocations, allocate(arms, 84))
        reversed_arms = allocate(list(reversed(arms)), 83)
        self.assertEqual([a["response_seed"] for a in allocations], [a["response_seed"] for a in reversed_arms])

    def test_no_arm_is_bound_to_a_slot(self):
        arms = [f"I{i}" for i in range(6)]
        counts = Counter()
        for seed in range(600):
            counts.update((entry["arm"], entry["slot"]) for entry in allocate(arms, seed))
        self.assertEqual(len(counts), 36)
        # 固定种子的宽容分布检查；均匀性由标准库 shuffle 实现，而非实测效果。
        self.assertTrue(all(50 <= count <= 150 for count in counts.values()))

    def test_allocation_copy_and_invalid_arms(self):
        baseline = allocate(["I0", "I1"], 1)
        changed = allocate(["I0", "I1"], 1)
        changed[0]["arm"] = "仅修改副本"
        self.assertEqual(baseline, allocate(["I0", "I1"], 1))
        for arms in ([], None, "I0", ["I0", "I0"], [""], [1]):
            with self.subTest(arms=arms), self.assertRaises(ValueError):
                allocate(arms, 1)
        self.assertEqual(allocate(["I0"], 1)[0]["slot"], 0)


class TestSyntheticFixtures(unittest.TestCase):
    def test_counts_schema_and_chinese_length(self):
        for phase, accurate, inaccurate in (("exposure", 16, 8), ("retention", 8, 4)):
            with self.subTest(phase=phase):
                cards = make_fixture_cards(phase=phase)
                self.assertEqual(len(cards), 3 * (accurate + inaccurate))
                self.assertEqual(Counter((c["stance"], c["is_accurate"]) for c in cards), {
                    (stance, label): accurate if label else inaccurate
                    for stance in (-1, 0, 1) for label in (True, False)
                })
                self.assertEqual(len({c["card_id"] for c in cards}), len(cards))
                for card in cards:
                    self.assertEqual(set(card), PUBLIC_KEYS | {"phase", "stance", "is_accurate", "is_demo"})
                    self.assertIs(type(card["stance"]), int)
                    self.assertIs(type(card["is_accurate"]), bool)
                    self.assertIs(card["is_demo"], True)
                    self.assertEqual(card["topic_id"], "SYNTHETIC")
                    self.assertEqual(card["phase"], phase)
                    self.assertEqual(card["transformation_type"], "synthetic")
                    self.assertTrue(card["source_url"].startswith("synthetic://fixture/"))
                    self.assertRegex(card["card_id"], r"^C[0-9a-f]{24}$")
                    self.assertRegex(card["source_family_id"], r"^F[0-9a-f]{24}$")
                    text = card["text"]
                    self.assertTrue(180 <= len(text) <= 220, (len(text), text))
                    chinese_count = sum("\u4e00" <= ch <= "\u9fff" for ch in text)
                    self.assertTrue(180 <= chinese_count <= 220, chinese_count)
                    self.assertTrue(text.startswith("【合成工程夹具，内容不真实】"))
                    self.assertIn("封闭虚构", text)
                    self.assertIn("不是研究数据", text)
                    self.assertNotRegex(text, r"https?://|正确|错误|答案|is_accurate")

    def test_labels_match_only_closed_fictional_arithmetic(self):
        digits = "零一二三四五六七八九"
        numbers = {ch: n for n, ch in enumerate(digits)}
        for n in range(10, 100):
            tens, units = divmod(n, 10)
            word = (digits[tens] if tens > 1 else "") + "十" + (digits[units] if units else "")
            numbers[word] = n
        for phase in ("exposure", "retention"):
            for card in make_fixture_cards(phase=phase):
                match = re.search(r"每班有(.*?)个座位，每个观察周期发车(.*?)班", card["text"])
                total = re.search(r"合计提供(.*?)个座位", card["text"])
                self.assertIsNotNone(match)
                self.assertIsNotNone(total)
                expected = numbers[match[1]] * numbers[match[2]]
                claimed = numbers[total[1]]
                self.assertEqual(claimed == expected, card["is_accurate"])

    def test_shared_families_and_disjoint_phases_and_topics(self):
        exposure = make_fixture_cards()
        retention = make_fixture_cards(phase="retention")
        other_topic = make_fixture_cards(topic_id="另一虚构主题")
        for key in ("card_id", "source_family_id", "source_url"):
            self.assertTrue({c[key] for c in exposure}.isdisjoint(c[key] for c in retention))
            self.assertTrue({c[key] for c in exposure}.isdisjoint(c[key] for c in other_topic))
        for cards in (exposure, retention):
            families = defaultdict(list)
            for card in cards:
                families[card["source_family_id"]].append(card)
            self.assertEqual(len(families), len(cards) // 3)
            for family in families.values():
                self.assertEqual(len(family), 3)
                self.assertEqual({c["stance"] for c in family}, {-1, 0, 1})
            self.assertTrue(any(len({c["is_accurate"] for c in family}) == 2 for family in families.values()))

    def test_fixture_reproducibility_and_independent_copies(self):
        baseline = make_fixture_cards()
        self.assertEqual(baseline, make_fixture_cards())
        modified = make_fixture_cards()
        modified[0]["text"] = "仅修改本副本"
        self.assertEqual(baseline, make_fixture_cards())

    def test_invalid_phase_and_topic(self):
        for phase in ("intervention", "unknown", "", None):
            with self.subTest(phase=phase), self.assertRaises(ValueError):
                make_fixture_cards(phase=phase)
        for topic in ("", "  ", None, 1):
            with self.subTest(topic=topic), self.assertRaises(ValueError):
                make_fixture_cards(topic_id=topic)


class TestScheduling(unittest.TestCase):
    def test_all_environments_both_stances_and_fixture_phases(self):
        for phase, rounds in (("exposure", 8), ("retention", 4)):
            cards = make_fixture_cards(phase=phase)
            by_id = {c["card_id"]: c for c in cards}
            for environment in "ABCD":
                for stance in (-1, 1):
                    for seed in range(6):
                        with self.subTest(phase=phase, environment=environment, stance=stance, seed=seed):
                            rows = schedule_cards(cards, stance, environment, rounds, seed)
                            assert_schedule(self, rows, stance, environment, rounds)
                            for row in rows:
                                for card in row:
                                    self.assertEqual(card, by_id[card["card_id"]])

    def test_joint_error_categories_and_positions_are_balanced(self):
        cards = make_fixture_cards()
        for environment in ("B", "D"):
            for stance in (-1, 1):
                for seed in range(12):
                    with self.subTest(environment=environment, stance=stance, seed=seed):
                        rows = schedule_cards(cards, stance, environment, 8, seed)
                        categories = Counter(c["stance"] for row in rows for c in row if not c["is_accurate"])
                        positions = Counter(p for row in rows for p, c in enumerate(row) if not c["is_accurate"])
                        self.assertEqual(set(categories), {-1, 0, 1})
                        self.assertEqual(set(positions), set(range(6)))
                        self.assertLessEqual(max(categories.values()) - min(categories.values()), 1)
                        self.assertLessEqual(max(positions.values()) - min(positions.values()), 1)
                        for start in range(6):
                            triplet = rows[start:start + 3]
                            self.assertEqual(Counter(c["stance"] for row in triplet for c in row if not c["is_accurate"]), {-1: 2, 0: 2, 1: 2})
                            self.assertEqual(Counter(p for row in triplet for p, c in enumerate(row) if not c["is_accurate"]), dict.fromkeys(range(6), 1))
                            self.assertTrue(any(all(c["is_accurate"] for c in row if c["stance"] == stance) for row in triplet))
                        for category in (-1, 0, 1):
                            seen_positions = {p for row in rows for p, c in enumerate(row) if c["stance"] == category and not c["is_accurate"]}
                            self.assertGreaterEqual(len(seen_positions), 2)

    def test_frozen_patterns_rotate_with_seed(self):
        cards = make_fixture_cards()
        patterns = []
        for seed in range(3):
            rows = schedule_cards(cards, 1, "D", 3, seed)
            patterns.append([
                tuple(sum(c["stance"] == stance and not c["is_accurate"] for c in row) for stance in (1, -1, 0))
                for row in rows
            ])
        self.assertEqual(patterns[0], [(1, 1, 0), (0, 1, 1), (1, 0, 1)])
        self.assertEqual(patterns[1], patterns[0][1:] + patterns[0][:1])
        self.assertEqual(patterns[2], patterns[0][2:] + patterns[0][:2])

    def test_reproducibility_and_different_seeds(self):
        cards = make_fixture_cards()
        for environment in "ABCD":
            baseline = schedule_cards(cards, 1, environment, 8, 91)
            self.assertEqual(baseline, schedule_cards(cards, 1, environment, 8, 91))
            self.assertNotEqual(baseline, schedule_cards(cards, 1, environment, 8, 92))

    def test_negative_seed_is_supported(self):
        cards = make_fixture_cards()
        rows = schedule_cards(cards, -1, "D", 8, -17)
        assert_schedule(self, rows, -1, "D", 8)
        self.assertEqual(rows, schedule_cards(cards, -1, "D", 8, -17))

    def test_input_repeated_deliveries_and_branches_are_deeply_isolated(self):
        cards = make_fixture_cards()
        for card in cards:
            card["candidate_pool"] = {"ids": [card["card_id"]]}
        baseline = deepcopy(cards)
        rows = schedule_cards(cards, 1, "C", 8, 2)
        branch = schedule_cards(cards, 1, "C", 8, 2)
        occurrences = defaultdict(list)
        for row in rows:
            for card in row:
                occurrences[card["card_id"]].append(card)
        repeated = next(group for group in occurrences.values() if len(group) == 2)
        self.assertIsNot(repeated[0], repeated[1])
        self.assertIsNot(repeated[0]["candidate_pool"], repeated[1]["candidate_pool"])
        repeated[0]["candidate_pool"]["ids"].append("仅修改本次分发")
        self.assertEqual(len(repeated[1]["candidate_pool"]["ids"]), 1)
        self.assertEqual(cards, baseline)
        self.assertEqual(branch, schedule_cards(cards, 1, "C", 8, 2))

    def test_exact_capacity_does_not_greedily_exhaust_last_round(self):
        source = make_fixture_cards()
        cards = [c for stance in (-1, 0, 1) for c in [p for p in source if p["stance"] == stance and p["is_accurate"]][:3]]
        for seed in range(80):
            with self.subTest(seed=seed):
                rows = schedule_cards(cards, 1, "A", 3, seed)
                assert_schedule(self, rows, 1, "A", 3)
                self.assertEqual(set(Counter(c["card_id"] for row in rows for c in row).values()), {2})

    def test_tight_joint_strata_inventory_succeeds_for_every_frozen_pattern(self):
        source = make_fixture_cards()
        patterns = ((1, 1, 0), (0, 1, 1), (1, 0, 1))
        for environment, quotas in (("B", (2, 2, 2)), ("D", (4, 1, 1))):
            for seed in range(6):
                for rounds in (1, 2, 4, 8, 9):
                    with self.subTest(environment=environment, seed=seed, rounds=rounds):
                        cards = []
                        for category, stance in enumerate((1, -1, 0)):
                            wrong = [patterns[(r + seed) % 3][category] for r in range(rounds)]
                            for accurate in (True, False):
                                demand = [quotas[category] - count if accurate else count for count in wrong]
                                minimum = max(max(demand), (sum(demand) + 1) // 2)
                                pool = [c for c in source if c["stance"] == stance and c["is_accurate"] == accurate]
                                self.assertGreaterEqual(len(pool), minimum)
                                cards.extend(pool[:minimum])
                        rows = schedule_cards(cards, 1, environment, rounds, seed)
                        assert_schedule(self, rows, 1, environment, rounds)
                        # 第一类已压到可行下界；再少一张必须在分发前拒绝。
                        with self.assertRaises(ValueError):
                            schedule_cards(cards[1:], 1, environment, rounds, seed)

    def test_missing_required_stratum_fails_before_any_delivery_copy(self):
        source = make_fixture_cards()
        for environment in "ABCD":
            for stance in (-1, 0, 1):
                for accurate in ((True, False) if environment in ("B", "D") else (True,)):
                    with self.subTest(environment=environment, stance=stance, accurate=accurate):
                        cards = [c for c in source if (c["stance"], c["is_accurate"]) != (stance, accurate)]
                        with patch("cocoon.scheduling.deepcopy", side_effect=AssertionError("库存不足时不得开始分发")):
                            with self.assertRaisesRegex(ValueError, "库存不足"):
                                schedule_cards(cards, 1, environment, 8, 5)

    def test_reports_all_deficient_strata(self):
        with self.assertRaises(ValueError) as caught:
            schedule_cards([], 1, "D", 8, 0)
        for stance in (-1, 0, 1):
            for accurate in (True, False):
                self.assertIn(f"stance={stance}, is_accurate={accurate}", str(caught.exception))

    def test_per_round_uniqueness_inventory_is_checked(self):
        source = make_fixture_cards()
        cards = [next(c for c in source if c["stance"] == stance and c["is_accurate"]) for stance in (-1, 0, 1)]
        # 总容量 3*2=6 足够，但每类只有一张，无法同轮取两张不同的卡。
        with self.assertRaisesRegex(ValueError, "单轮需求峰值"):
            schedule_cards(cards, 1, "A", 1, 0)

    def test_capacity_boundary_and_unneeded_error_pool(self):
        cards = [c for c in make_fixture_cards() if c["is_accurate"]]
        for environment, rounds in (("A", 16), ("C", 8)):
            rows = schedule_cards(cards, 1, environment, rounds, 0)
            assert_schedule(self, rows, 1, environment, rounds)
            with self.assertRaises(ValueError):
                schedule_cards(cards, 1, environment, rounds + 1, 0)
        retention = make_fixture_cards(phase="retention")
        with self.assertRaises(ValueError):
            schedule_cards(retention, 1, "C", 5, 0)

    def test_repeat_limit_can_be_tightened_to_one(self):
        cards = make_fixture_cards()
        for environment in "ABCD":
            rows = schedule_cards(cards, 1, environment, 4, 0, max_repeats=1)
            assert_schedule(self, rows, 1, environment, 4, max_repeats=1)
        with self.assertRaises(ValueError):
            schedule_cards(cards, 1, "C", 8, 0, max_repeats=1)

    def test_zero_rounds(self):
        self.assertEqual(schedule_cards([], 1, "A", 0, 0), [])
        self.assertEqual(schedule_cards(make_fixture_cards(), -1, "D", 0, 0), [])

    def test_invalid_schedule_parameters(self):
        cards = make_fixture_cards()
        defaults = dict(cards=cards, initial_stance=1, environment="A", rounds=1, seed=1, max_repeats=2)
        invalid = {
            "cards": (None, "cards"),
            "initial_stance": (0, 2, True, "1"),
            "environment": ("E", "a", None, []),
            "rounds": (-1, 1.5, True, "8"),
            "max_repeats": (0, 3, -1, True, 1.5),
        }
        for key, values in invalid.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    schedule_cards(**(defaults | {key: value}))

    def test_invalid_cards_and_duplicate_ids(self):
        source = make_fixture_cards()
        malformed = [None, {}, {"card_id": "x", "stance": True, "is_accurate": True},
                     {"card_id": "x", "stance": 2, "is_accurate": True},
                     {"card_id": "x", "stance": 0, "is_accurate": 1}]
        for card in malformed:
            with self.subTest(card=card), self.assertRaises(ValueError):
                schedule_cards([card], 1, "A", 1, 0)
        with self.assertRaisesRegex(ValueError, "唯一"):
            schedule_cards(source + [deepcopy(source[0])], 1, "A", 1, 0)


class TestPublicCard(unittest.TestCase):
    def test_strict_whitelist_blocks_labels_answers_and_unknown_metadata(self):
        for fixture in make_fixture_cards():
            card = deepcopy(fixture)
            card.update({
                "label": "不可见", "analysis": "不可见", "justification": "不可见",
                "answer": True, "答案": "不可见", "truth_directory": "true/",
                "candidate_pool": {"is_accurate": True}, "future_secret": "不可见",
            })
            original = deepcopy(card)
            visible = public_card(card)
            self.assertEqual(set(visible), PUBLIC_KEYS | {"is_demo"})
            self.assertIs(visible["is_demo"], True)
            self.assertEqual(card, original)
            for key, value in visible.items():
                self.assertEqual(value, fixture[key])

    def test_optional_demo_flag_and_copy(self):
        card = make_fixture_cards()[0]
        del card["is_demo"]
        visible = public_card(card)
        self.assertEqual(set(visible), PUBLIC_KEYS)
        visible["text"] = "仅修改公共副本"
        self.assertNotEqual(visible["text"], card["text"])

    def test_rejects_truth_directories_or_labels_in_visible_identifiers(self):
        baseline = make_fixture_cards()[0]
        bad_metadata = {
            "source_url": [
                "synthetic://fixture/true/card", "synthetic://fixture/false/card",
                "synthetic://fixture/accurate/card", "synthetic://fixture/错误/card",
                "synthetic://fixture/%74rue/card", "synthetic://fixture/%E7%AD%94%E6%A1%88/card",
                "synthetic://fixture/card?is_accurate=1", "synthetic://fixture/真/card",
            ],
            "card_id": ["false_001", "answer-001"],
            "source_family_id": ["label-001", "gold_standard"],
            "topic_id": ["答案", "真假目录"],
            "transformation_type": ["synthetic/true"],
        }
        for key, values in bad_metadata.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    public_card(baseline | {key: value})

    def test_rejects_missing_or_nested_public_fields(self):
        card = make_fixture_cards()[0]
        for key in PUBLIC_KEYS:
            missing = dict(card)
            del missing[key]
            with self.subTest(key=key), self.assertRaises(ValueError):
                public_card(missing)
            with self.assertRaises(ValueError):
                public_card(card | {key: {"answer": "不得嵌套泄漏"}})
        with self.assertRaises(ValueError):
            public_card(card | {"is_demo": "true"})


class TestThreePhaseEngineeringFlow(unittest.TestCase):
    def test_exposure_eight_rounds_for_eight_and_sixteen_agents(self):
        cards = make_fixture_cards(phase="exposure")
        for n in (8, 16):
            agents = make_population(n, 67)
            network = make_network(agents, 67)
            original = deepcopy((agents, network))
            for environment in "ABCD":
                for agent in agents:
                    rows = schedule_cards(cards, agent["initial_stance"], environment, 8, 67)
                    assert_schedule(self, rows, agent["initial_stance"], environment, 8)
            self.assertEqual((agents, network), original)

    def test_intervention_six_slots_two_rounds_only_engineering_smoke(self):
        # 此处仅用曝光夹具跑两轮接口与分支隔离，绝非干预证据包或干预剂量实现。
        cards = make_fixture_cards()
        arms = [f"I{i}" for i in range(6)]
        for n in (8, 16):
            agents = make_population(n, 12)
            network = make_network(agents, 12)
            baseline = {"agents": agents, "network": network, "is_simulation": True}
            branches = [deepcopy(baseline) for _ in arms]
            for allocation, branch in zip(allocate(arms, 12), branches):
                self.assertEqual(branch, baseline)
                self.assertIs(branch["is_simulation"], True)
                for agent in branch["agents"]:
                    rows = schedule_cards(cards, agent["initial_stance"], "D", 2, allocation["response_seed"])
                    assert_schedule(self, rows, agent["initial_stance"], "D", 2)
                    self.assertTrue(all(public_card(c)["is_demo"] for row in rows for c in row))
            branches[0]["agents"][0]["preferences"]["background"] = "分支内修改"
            branches[0]["network"][0]["followee_id"] = "分支内修改"
            for branch in branches[1:]:
                self.assertEqual(branch, baseline)

    def test_retention_four_rounds_uses_disjoint_fixture_pool(self):
        exposure_ids = {c["card_id"] for c in make_fixture_cards()}
        cards = make_fixture_cards(phase="retention")
        for n in (8, 16):
            for agent in make_population(n, 4):
                for environment in "ABCD":
                    rows = schedule_cards(cards, agent["initial_stance"], environment, 4, 4)
                    assert_schedule(self, rows, agent["initial_stance"], environment, 4)
                    self.assertTrue(exposure_ids.isdisjoint(c["card_id"] for row in rows for c in row))
                    self.assertTrue(all(c["phase"] == "retention" for row in rows for c in row))


class TestInterfaceContract(unittest.TestCase):
    def test_public_parameter_names_and_defaults(self):
        expected = {
            make_population: ["n", "seed"],
            make_network: ["agents", "seed"],
            allocate: ["arms", "seed"],
            make_fixture_cards: ["topic_id", "phase"],
            schedule_cards: ["cards", "initial_stance", "environment", "rounds", "seed", "max_repeats"],
            public_card: ["card"],
        }
        for function, names in expected.items():
            self.assertEqual(list(inspect.signature(function).parameters), names)
        self.assertEqual(inspect.signature(make_fixture_cards).parameters["topic_id"].default, "SYNTHETIC")
        self.assertEqual(inspect.signature(make_fixture_cards).parameters["phase"].default, "exposure")
        self.assertEqual(inspect.signature(schedule_cards).parameters["max_repeats"].default, 2)

    def test_invalid_seeds(self):
        agents = make_population(8, 0)
        cards = make_fixture_cards()
        for seed in (True, "1", 1.0, None):
            calls = (
                lambda: make_population(8, seed),
                lambda: make_network(agents, seed),
                lambda: allocate(["I0"], seed),
                lambda: schedule_cards(cards, 1, "A", 1, seed),
            )
            for call in calls:
                with self.subTest(seed=seed), self.assertRaises(ValueError):
                    call()

    def test_no_global_random_state_mutation(self):
        before = random.getstate()
        agents = make_population(8, 1)
        make_network(agents, 1)
        allocate(["I0", "I1"], 1)
        cards = make_fixture_cards()
        schedule_cards(cards, 1, "B", 8, 1)
        public_card(cards[0])
        self.assertEqual(random.getstate(), before)


if __name__ == "__main__":
    unittest.main()
