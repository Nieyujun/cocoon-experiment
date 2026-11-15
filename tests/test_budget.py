"""免费、离线的假 usage 测试，不是供应商真实账单。

仅在 mkdtemp(dir=ROOT / 'tests') 内写假账本，全部永久保留，不自动清理。
使用 python -B -m unittest discover -s tests -p test_budget.py -v 收集自身测试。
"""

from copy import deepcopy
from decimal import Decimal, Inexact, localcontext
import errno
from hashlib import sha256
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import mkdtemp
import threading
import unittest
from unittest.mock import patch

from cocoon import budget
from cocoon.budget import (
    BudgetError,
    BudgetLedger,
    COST_BASIS,
    INPUT_RESERVE_TOKENS,
    MAX_COMPLETION_TOKENS,
    RATE_CACHED_INPUT_CNY_PER_MILLION,
    RATE_INPUT_CNY_PER_MILLION,
    RATE_OUTPUT_CNY_PER_MILLION,
)


ROOT = Path(__file__).resolve().parents[1]


def fake_usage(prompt=1000, completion=10, **extra):
    return {
        "prompt_tokens": prompt, "completion_tokens": completion,
        "total_tokens": prompt + completion,
    } | extra


def encode(value):
    return json.dumps(
        value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(mkdtemp(prefix="budget_fake_free_", dir=ROOT / "tests"))
        self.path = self.directory / "fake_free_not_invoice.jsonl"
        self._path_number = 0

    def fresh_path(self):
        self._path_number += 1
        return self.directory / f"fake_free_{self._path_number}.jsonl"

    def assert_error(self, code, function, *args, **kwargs):
        with self.assertRaises(BudgetError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        self.assertIsInstance(caught.exception, ValueError)

    def records(self, path=None):
        return [json.loads(line) for line in (path or self.path).read_bytes().splitlines()]

    def write_rehashed(self, path, records):
        previous = "0" * 64
        for record in records:
            record["prev_hash"] = previous
            record.pop("hash", None)
            record["hash"] = sha256(encode(record)).hexdigest()
            previous = record["hash"]
        path.write_bytes(b"".join(encode(record) + b"\n" for record in records))

    def child(self, code):
        result = subprocess.run(
            [sys.executable, "-B", "-c", code, str(self.path)],
            cwd=ROOT, env=os.environ | {"PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True, text=True, encoding="utf-8", timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_public_interface_constants_and_no_cancel(self):
        self.assertEqual(list(inspect.signature(BudgetLedger).parameters), ["path", "limit_cny"])
        self.assertEqual(inspect.signature(BudgetLedger).parameters["limit_cny"].default, "5.00")
        for method, names in (
            (BudgetLedger.status, ["self"]),
            (BudgetLedger.reserve, ["self", "request_id", "max_completion_tokens"]),
            (BudgetLedger.settle, ["self", "request_id", "usage"]),
            (BudgetLedger.mark_uncertain, ["self", "request_id", "reason"]),
        ):
            self.assertEqual(list(inspect.signature(method).parameters), names)
        self.assertFalse(hasattr(BudgetLedger, "cancel"))
        self.assertEqual(RATE_INPUT_CNY_PER_MILLION, Decimal("1.00"))
        self.assertEqual(RATE_CACHED_INPUT_CNY_PER_MILLION, Decimal("0.02"))
        self.assertEqual(RATE_OUTPUT_CNY_PER_MILLION, Decimal("2.00"))
        self.assertEqual(INPUT_RESERVE_TOKENS, 1048576)
        self.assertEqual(MAX_COMPLETION_TOKENS, 131072)
        self.assertEqual(budget.PRICE_VERIFIED_DATE, "2026-11-08")
        self.assertEqual(budget.PRICE_EFFECTIVE_DATE, "2026-10-21")
        self.assertIn("PRICE_EFFECTIVE_DATE", budget.__all__)
        self.assertIsInstance(BudgetError.code, property)

    def test_initialize_empty_file_and_reopen_never_reset(self):
        self.path.touch()
        ledger = BudgetLedger(self.path)
        initial = self.path.read_bytes()
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.records()[0]["type"], "init")
        self.assertEqual(self.records()[0]["limit_cny"], "5.00")
        policy = self.records()[0]["policy"]
        self.assertEqual(policy["price_verified_date"], "2026-11-08")
        self.assertEqual(policy["price_effective_date"], "2026-10-21")
        ledger.reserve("fake-first", 100)
        reserved = self.path.read_bytes()
        self.assertTrue(reserved.startswith(initial))
        other = BudgetLedger(self.path, Decimal("5.000"))
        self.assertEqual(self.path.read_bytes(), reserved)
        self.assertEqual(ledger.status(), other.status())
        self.assertEqual(other.status()["request_count"], 1)
        self.assertTrue(Path(str(self.path) + ".lock").exists())

    def test_parent_must_exist_and_path_must_be_path(self):
        target = self.directory / "must_not_be_created" / "fake.jsonl"
        self.assert_error("invalid_path", BudgetLedger, target)
        self.assertFalse(target.parent.exists())
        self.assert_error("invalid_path", BudgetLedger, str(self.path))
        self.assert_error("invalid_path", BudgetLedger, None)
        directory_path = self.directory / "fake_directory_not_ledger"
        directory_path.mkdir()
        self.assert_error("budget_io_error", BudgetLedger, directory_path)
        self.assertTrue(directory_path.is_dir())

    def test_limit_validation_overflow_bool_none_nan_and_float(self):
        invalid = (
            None, True, False, 5.0, float("nan"), float("inf"), "", "bad",
            "NaN", "sNaN", "Infinity", "-Infinity", "0", "-1", 0, -5,
            Decimal("NaN"), Decimal("Infinity"), "1e999999999",
            "1e-999999999", "1e999999999999999999999", "0.000000001",
            Decimal("1e100"), [], {},
        )
        for value in invalid:
            with self.subTest(value=repr(value)):
                path = self.fresh_path()
                self.assert_error("invalid_limit", BudgetLedger, path, value)
                self.assertFalse(path.exists())
                self.assertFalse(Path(str(path) + ".lock").exists())
        for value in ("5.00", 5, Decimal("5.00"), "1.048578", "0.00000001"):
            with self.subTest(valid=repr(value)):
                ledger = BudgetLedger(self.fresh_path(), value)
                self.assertEqual(Decimal(ledger.status()["limit_cny"]), Decimal(value))

    def test_existing_limit_cannot_change_even_without_requests(self):
        BudgetLedger(self.path)
        before = self.path.read_bytes()
        for changed in ("4.00", "6.00", "500"):
            with self.subTest(changed=changed):
                self.assert_error("limit_mismatch", BudgetLedger, self.path, changed)
                self.assertEqual(self.path.read_bytes(), before)
        self.assert_error("invalid_limit", BudgetLedger, self.path, None)
        self.assertEqual(BudgetLedger(self.path, "5").status()["limit_cny"], "5.00")

    def test_status_json_safe_and_unrecorded_cost_not_claimed_free(self):
        ledger = BudgetLedger(self.path)
        empty = ledger.status()
        for name in ("limit_cny", "charged_cny", "reserved_cny", "available_cny"):
            self.assertIsInstance(empty[name], str)
        self.assertEqual(empty["limit_cny"], "5.00")
        self.assertEqual(empty["charged_cny"], "0.00")
        self.assertIs(empty["blocked"], False)
        self.assertIs(empty["needs_reconciliation"], False)
        self.assertEqual(empty["cost_basis"], COST_BASIS)
        reservation = ledger.reserve("fake-unknown", 10)
        self.assertEqual(reservation["reservation_id"], "fake-unknown")
        self.assertEqual(reservation["reserved_cny"], "1.048596")
        self.assertIsNone(reservation["charged_cny"])
        status = ledger.status()
        self.assertEqual(status["charged_cny"], "0.00")
        self.assertEqual(status["charged_scope"], "settled_requests_only")
        self.assertIsNone(status["unresolved_cost_cny"])
        self.assertIsNone(status["total_cost_cny"])
        self.assertEqual(status["unresolved_request_count"], 1)
        self.assertIs(status["needs_reconciliation"], True)
        self.assertEqual(status["cache_policy"], "missing_or_null_cached_tokens_assumed_zero")
        self.assertIn("conservative_bound_not_estimate", status["reservation_basis"])
        json.dumps([empty, reservation, status], allow_nan=False)

    def test_full_context_reserve_and_exact_available_boundary(self):
        ledger = BudgetLedger(self.path, "1.048578")
        result = ledger.reserve("fake-boundary", 1)
        self.assertEqual(result["reserved_cny"], "1.048578")
        self.assertEqual(ledger.status()["available_cny"], "0.00")
        before = self.path.read_bytes()
        self.assert_error("unresolved_attempt", ledger.reserve, "fake-too-much", 1)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(ledger.status()["request_count"], 1)
        settled = ledger.settle("fake-boundary", fake_usage(INPUT_RESERVE_TOKENS, 1))
        self.assertEqual(settled["charged_cny"], result["reserved_cny"])
        reopened = BudgetLedger(self.path, "1.048578")
        status = reopened.status()
        self.assertEqual(status["available_cny"], "0.00")
        self.assertEqual(status["reserved_cny"], "0.00")
        self.assertIs(status["blocked"], False)
        self.assertIs(status["needs_reconciliation"], False)
        before = self.path.read_bytes()
        self.assert_error("budget_exceeded", reopened.reserve, "fake-after-cap", 1)
        self.assertEqual(self.path.read_bytes(), before)

    def test_limit_below_reservation_rejects_first_attempt(self):
        ledger = BudgetLedger(self.path, "1.04857799")
        before = self.path.read_bytes()
        self.assert_error("budget_exceeded", ledger.reserve, "fake-under-cap", 1)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(ledger.status()["request_count"], 0)
        self.assertIs(ledger.status()["needs_reconciliation"], False)

    def test_zero_usage_settlement_allows_next_serial_reservation(self):
        ledger = BudgetLedger(self.path, "1.048578")
        ledger.reserve("fake-zero-usage", 1)
        ledger.settle("fake-zero-usage", fake_usage(0, 0))
        self.assertEqual(ledger.status()["available_cny"], "1.048578")
        self.assertIs(ledger.status()["needs_reconciliation"], False)
        ledger.reserve("fake-next", 1)
        self.assertEqual(ledger.status()["request_count"], 2)

    def test_invalid_ids_and_output_limits_never_append(self):
        ledger = BudgetLedger(self.path)
        before = self.path.read_bytes()
        for value in (None, True, 1, "", "   ", [], {}):
            with self.subTest(request_id=repr(value)):
                self.assert_error("invalid_request_id", ledger.reserve, value, 1)
        for value in (None, True, False, 0, -1, 131073, 10**100, 1.0, "10"):
            with self.subTest(max_completion_tokens=repr(value)):
                self.assert_error("invalid_max_completion_tokens", ledger.reserve, "fake", value)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(ledger.reserve("fake-max", 131072)["reserved_cny"], "1.31072")

    def test_same_id_permanently_rejected_across_states_and_instances(self):
        ledger = BudgetLedger(self.path)
        ledger.reserve("fake-same", 100)
        for output in (1, 100, 131072):
            self.assert_error("duplicate_request", ledger.reserve, "fake-same", output)
        ledger.mark_uncertain("fake-same", "timeout")
        self.assert_error("duplicate_request", BudgetLedger(self.path).reserve, "fake-same", 100)
        ledger.settle("fake-same", fake_usage(completion=10))
        before = self.path.read_bytes()
        self.assert_error("duplicate_request", BudgetLedger(self.path).reserve, "fake-same", 100)
        self.assert_error("already_settled", ledger.settle, "fake-same", fake_usage())
        self.assert_error("already_settled", ledger.mark_uncertain, "fake-same", "timeout")
        self.assertEqual(self.path.read_bytes(), before)

    def test_cap_is_cumulative_across_instances_and_settlements(self):
        first = BudgetLedger(self.path)
        second = BudgetLedger(self.path)
        for index in range(3):
            ledger = first if index % 2 else second
            ledger.reserve(f"fake-{index}", MAX_COMPLETION_TOKENS)
            ledger.settle(f"fake-{index}", fake_usage(INPUT_RESERVE_TOKENS - 1, MAX_COMPLETION_TOKENS))
        self.assertEqual(first.status()["charged_cny"], "3.932157")
        self.assertEqual(second.status()["reserved_cny"], "0.00")
        self.assert_error("budget_exceeded", BudgetLedger(self.path).reserve, "fake-fourth", MAX_COMPLETION_TOKENS)
        self.assertEqual(BudgetLedger(self.path).status()["request_count"], 3)

    def test_fresh_process_observes_prior_charges_and_cap(self):
        ledger = BudgetLedger(self.path)
        ledger.reserve("fake-parent", MAX_COMPLETION_TOKENS)
        ledger.settle("fake-parent", fake_usage(INPUT_RESERVE_TOKENS, MAX_COMPLETION_TOKENS))
        output = self.child(
            "from pathlib import Path\n"
            "import sys\n"
            "from cocoon.budget import BudgetLedger\n"
            "ledger = BudgetLedger(Path(sys.argv[1]))\n"
            "usage = {'prompt_tokens': 1048576, 'completion_tokens': 131072, 'total_tokens': 1179648}\n"
            "ledger.reserve('fake-child-1', 131072)\n"
            "ledger.settle('fake-child-1', usage)\n"
            "ledger.reserve('fake-child-2', 131072)\n"
            "ledger.settle('fake-child-2', usage)\n"
            "print(ledger.status()['request_count'])\n"
        )
        self.assertEqual(output, "3")
        self.assertEqual(ledger.status()["charged_cny"], "3.93216")
        self.assertEqual(ledger.status()["reserved_cny"], "0.00")
        self.assert_error("budget_exceeded", ledger.reserve, "fake-fourth", MAX_COMPLETION_TOKENS)
        self.assertEqual(self.child(
            "from pathlib import Path\nimport sys\n"
            "from cocoon.budget import BudgetLedger\n"
            "print(BudgetLedger(Path(sys.argv[1])).status()['charged_cny'])\n"
        ), "3.93216")

    def test_process_exit_keeps_reservation_and_blocks_rebuilt_instances(self):
        ledger = BudgetLedger(self.path)
        self.child(
            "from pathlib import Path\nimport os, sys\n"
            "from cocoon.budget import BudgetLedger\n"
            "BudgetLedger(Path(sys.argv[1])).reserve('fake-interrupted', 10)\n"
            "os._exit(0)\n"
        )
        for state in ("reserved", "uncertain"):
            with self.subTest(state=state):
                if state == "uncertain":
                    ledger.mark_uncertain("fake-interrupted", "interrupted")
                before = self.path.read_bytes()
                self.assert_error("unresolved_attempt", ledger.reserve, "fake-rebuilt", 10)
                self.assertEqual(self.child(
                    "from pathlib import Path\nimport sys\n"
                    "from cocoon.budget import BudgetError, BudgetLedger\n"
                    "try:\n    BudgetLedger(Path(sys.argv[1])).reserve('fake-new-process', 10)\n"
                    "except BudgetError as exc:\n    print(exc.code)\n"
                    "else:\n    raise AssertionError('fake unresolved attempt accepted')\n"
                ), "unresolved_attempt")
                self.assertEqual(self.path.read_bytes(), before)
                status = BudgetLedger(self.path).status()
                self.assertEqual(status["request_count"], 1)
                self.assertEqual(status["reserved_cny"], "1.048596")
                self.assertIs(status["needs_reconciliation"], True)
                self.assertIsNone(status["total_cost_cny"])

    def test_usage_cache_uses_decimal_official_rates_and_releases_only_remainder(self):
        ledger = BudgetLedger(self.path)
        ledger.reserve("fake-cached", 100)
        usage = fake_usage(1000, 100, prompt_tokens_details={"cached_tokens": 400})
        original = deepcopy(usage)
        result = ledger.settle("fake-cached", usage)
        self.assertEqual(usage, original)
        self.assertEqual(result["charged_cny"], "0.000808")
        self.assertEqual(result["reserved_cny"], "0.00")
        status = BudgetLedger(self.path).status()
        self.assertEqual(status["charged_cny"], "0.000808")
        self.assertEqual(status["available_cny"], "4.999192")
        self.assertEqual(status["total_cost_cny"], "0.000808")
        self.assertEqual(status["unresolved_request_count"], 0)
        self.assertEqual(status["cache_assumed_zero_request_count"], 0)
        json.dumps(result, allow_nan=False)

    def test_single_cached_token_preserves_eight_decimal_places(self):
        ledger = BudgetLedger(self.path)
        ledger.reserve("fake-one-token", 1)
        result = ledger.settle("fake-one-token", fake_usage(1, 0, prompt_tokens_details={"cached_tokens": 1}))
        self.assertEqual(result["charged_cny"], "0.00000002")
        self.assertEqual(ledger.status()["available_cny"], "4.99999998")

    def test_missing_null_or_empty_cache_is_conservatively_uncached(self):
        for details in ("absent", None, {}, {"cached_tokens": None}):
            with self.subTest(details=details):
                ledger = BudgetLedger(self.fresh_path())
                ledger.reserve("fake-no-cache-details", 10)
                usage = fake_usage()
                if details != "absent":
                    usage["prompt_tokens_details"] = details
                ledger.settle("fake-no-cache-details", usage)
                self.assertEqual(ledger.status()["charged_cny"], "0.00102")
                self.assertEqual(ledger.status()["cache_assumed_zero_request_count"], 1)

    def test_explicit_zero_cache_is_known_not_assumed(self):
        ledger = BudgetLedger(self.path)
        ledger.reserve("fake-known-zero", 10)
        ledger.settle("fake-known-zero", fake_usage(prompt_tokens_details={"cached_tokens": 0}))
        self.assertEqual(ledger.status()["charged_cny"], "0.00102")
        self.assertEqual(ledger.status()["cache_assumed_zero_request_count"], 0)

    def test_invalid_or_missing_usage_never_releases_reservation(self):
        ledger = BudgetLedger(self.path)
        ledger.reserve("fake-invalid", 100)
        baseline = self.path.read_bytes()
        invalid = [None, {}, [], "usage"]
        for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
            missing = fake_usage()
            missing.pop(name)
            invalid.append(missing)
            for value in (None, True, False, -1, 1.0, "1", Decimal("1")):
                invalid.append(fake_usage() | {name: value})
        invalid.extend([
            fake_usage() | {"total_tokens": 1},
            fake_usage(prompt_tokens_details=True),
            fake_usage(prompt_tokens_details=[]),
            fake_usage(prompt_tokens_details="raw"),
        ])
        for value in (True, False, -1, 1001, 1.0, "1", []):
            invalid.append(fake_usage(prompt_tokens_details={"cached_tokens": value}))
        for value in (True, False, -1, "0", 0.0, {}, [], {"tool_usage": 0}, {"page_usage": 0}):
            invalid.append(fake_usage(web_search_usage=value))
        for name in ("tool_usage", "page_usage"):
            for value in (None, True, False, -1, 0.0, "0", Decimal("0"), []):
                search = {"tool_usage": 0, "page_usage": 0} | {name: value}
                invalid.append(fake_usage(web_search_usage=search))
        for usage in invalid:
            with self.subTest(usage=repr(usage)):
                self.assert_error("invalid_usage", ledger.settle, "fake-invalid", usage)
                self.assertEqual(self.path.read_bytes(), baseline)
                self.assertEqual(ledger.status()["reserved_cny"], "1.048776")
                self.assertIsNone(ledger.status()["unresolved_cost_cny"])
                self.assertIs(ledger.status()["blocked"], False)
                self.assertIs(ledger.status()["needs_reconciliation"], True)
        self.assert_error("unresolved_attempt", ledger.reserve, "fake-after-invalid", 100)
        self.assertEqual(self.path.read_bytes(), baseline)

    def test_web_search_object_null_absent_and_legacy_zero_are_canonical(self):
        for search in ("absent", None, 0, {"tool_usage": 0, "page_usage": 0}):
            with self.subTest(search=search):
                path = self.fresh_path()
                ledger = BudgetLedger(path)
                ledger.reserve("fake-no-search", 10)
                usage = fake_usage()
                if search != "absent":
                    usage["web_search_usage"] = search
                original = deepcopy(usage)
                settled = ledger.settle("fake-no-search", usage)
                self.assertEqual(usage, original)
                self.assertEqual(settled["charged_cny"], "0.00102")
                record = self.records(path)[-1]
                self.assertEqual(record["usage"]["web_search_usage"], {
                    "tool_usage": 0, "page_usage": 0,
                })
                self.assertTrue(all(type(value) is int for value in record["usage"]["web_search_usage"].values()))
                self.assertEqual(path.read_bytes(), b"".join(encode(row) + b"\n" for row in self.records(path)))
                reopened = BudgetLedger(path)
                self.assertEqual(reopened.status(), ledger.status())
                self.assertIs(reopened.status()["needs_reconciliation"], False)
                reopened.reserve("fake-next-serial", 10)

    def test_unknown_request_rejected_without_mutation(self):
        ledger = BudgetLedger(self.path)
        before = self.path.read_bytes()
        self.assert_error("unknown_request", ledger.settle, "fake-missing", fake_usage())
        self.assert_error("unknown_request", ledger.mark_uncertain, "fake-missing", "timeout")
        self.assertEqual(self.path.read_bytes(), before)

    def test_401_and_timeout_remain_reserved_without_retry(self):
        for request_id, reason in (("fake-401", "http_401"), ("fake-timeout", "timeout")):
            with self.subTest(reason=reason):
                path = self.fresh_path()
                ledger = BudgetLedger(path)
                ledger.reserve(request_id, 100)
                result = ledger.mark_uncertain(request_id, reason)
                self.assertEqual(result["state"], "uncertain")
                self.assertIsNone(result["charged_cny"])
                self.assertEqual(result["reserved_cny"], "1.048776")
                before = path.read_bytes()
                self.assertEqual(ledger.mark_uncertain(request_id, reason), result)
                self.assert_error("duplicate_request", ledger.reserve, request_id, 100)
                reopened = BudgetLedger(path)
                self.assert_error("unresolved_attempt", reopened.reserve, "fake-no-retry", 100)
                self.assertEqual(path.read_bytes(), before)
                status = reopened.status()
                self.assertEqual(status["reserved_cny"], "1.048776")
                self.assertEqual(status["charged_cny"], "0.00")
                self.assertEqual(status["uncertain_request_count"], 1)
                self.assertIs(status["needs_reconciliation"], True)
                self.assertIsNone(status["total_cost_cny"])
                ledger.settle(request_id, fake_usage())
                self.assertEqual(ledger.status()["uncertain_request_count"], 0)
                self.assertEqual(ledger.status()["reserved_cny"], "0.00")
                self.assertIs(ledger.status()["needs_reconciliation"], False)
                reopened.reserve("fake-after-explicit-settlement", 100)

    def test_uncertain_reason_only_fixed_codes_not_upstream_text(self):
        ledger = BudgetLedger(self.path)
        ledger.reserve("fake-reason", 10)
        before = self.path.read_bytes()
        for reason in (None, True, [], "", "401: token=FAKE_SECRET", "timeout\nbody", "custom_code"):
            with self.subTest(reason=repr(reason)):
                self.assert_error("invalid_reason", ledger.mark_uncertain, "fake-reason", reason)
                self.assertEqual(self.path.read_bytes(), before)
        self.assertNotIn(b"FAKE_SECRET", self.path.read_bytes())

    def test_billing_anomalies_persist_block_and_keep_reservations(self):
        cases = (
            (fake_usage(INPUT_RESERVE_TOKENS + 1, 1), "input_bound_exceeded"),
            (fake_usage(1, 11), "completion_limit_exceeded"),
            (fake_usage(web_search_usage=1), "web_search_usage"),
            (fake_usage(web_search_usage=7), "web_search_usage"),
            (fake_usage(web_search_usage={"tool_usage": 1, "page_usage": 0}), "web_search_usage"),
            (fake_usage(web_search_usage={"tool_usage": 0, "page_usage": 1}), "web_search_usage"),
            (fake_usage(web_search_usage={"tool_usage": 2, "page_usage": 3}), "web_search_usage"),
            (fake_usage(10**100, 1), "input_bound_exceeded"),
        )
        for usage, reason in cases:
            with self.subTest(reason=reason):
                path = self.fresh_path()
                ledger = BudgetLedger(path)
                ledger.reserve("fake-anomaly", 10)
                with patch.object(budget.os, "fsync", wraps=os.fsync) as synced:
                    self.assert_error("billing_anomaly", ledger.settle, "fake-anomaly", usage)
                    self.assertEqual(synced.call_count, 1)
                reopened = BudgetLedger(path)
                status = reopened.status()
                self.assertIs(status["blocked"], True)
                self.assertIs(status["needs_reconciliation"], True)
                self.assertEqual(status["charged_cny"], "0.00")
                self.assertEqual(status["reserved_cny"], "1.048596")
                self.assertIsNone(status["total_cost_cny"])
                self.assertEqual(self.records(path)[-1]["type"], "blocked")
                self.assertEqual(self.records(path)[-1]["reason"], reason)
                self.assertEqual(path.read_bytes(), b"".join(encode(row) + b"\n" for row in self.records(path)))
                before = path.read_bytes()
                self.assert_error("budget_blocked", reopened.reserve, "fake-new", 1)
                self.assert_error("duplicate_request", reopened.reserve, "fake-anomaly", 10)
                self.assert_error("billing_anomaly", reopened.settle, "fake-anomaly", fake_usage())
                self.assert_error("billing_anomaly", reopened.mark_uncertain, "fake-anomaly", "timeout")
                self.assertEqual(path.read_bytes(), before)

    def test_bound_and_output_max_are_not_precise_estimates(self):
        for prompt, completion in (
            (INPUT_RESERVE_TOKENS, 9), (INPUT_RESERVE_TOKENS - 1, 10),
            (INPUT_RESERVE_TOKENS, 10),
        ):
            with self.subTest(prompt=prompt, completion=completion):
                path = self.fresh_path()
                ledger = BudgetLedger(path)
                reservation = ledger.reserve("fake-near-bound", 10)
                settled = ledger.settle("fake-near-bound", fake_usage(prompt, completion))
                self.assertLessEqual(Decimal(settled["charged_cny"]), Decimal(reservation["reserved_cny"]))
                if prompt == INPUT_RESERVE_TOKENS and completion == 10:
                    self.assertEqual(settled["charged_cny"], reservation["reserved_cny"])
                self.assertIs(BudgetLedger(path).status()["blocked"], False)

    def test_serial_guard_keeps_anomalous_reservation_permanently(self):
        ledger = BudgetLedger(self.path)
        ledger.reserve("fake-bad", 10)
        before = self.path.read_bytes()
        self.assert_error("unresolved_attempt", ledger.reserve, "fake-good", 10)
        self.assertEqual(self.path.read_bytes(), before)
        self.assert_error("billing_anomaly", ledger.settle, "fake-bad", fake_usage(1, 11))
        self.assert_error("budget_blocked", ledger.reserve, "fake-good", 10)
        status = BudgetLedger(self.path).status()
        self.assertIs(status["blocked"], True)
        self.assertIs(status["needs_reconciliation"], True)
        self.assertEqual(status["charged_cny"], "0.00")
        self.assertEqual(status["reserved_cny"], "1.048596")
        self.assertEqual(status["request_count"], 1)

    def test_serial_guard_allows_next_only_after_explicit_settlement(self):
        ledger = BudgetLedger(self.path)
        ledger.reserve("fake-first-serial", 10)
        for state in ("reserved", "uncertain"):
            with self.subTest(state=state):
                if state == "uncertain":
                    ledger.mark_uncertain("fake-first-serial", "timeout")
                before = self.path.read_bytes()
                for instance in (ledger, BudgetLedger(self.path)):
                    self.assert_error("unresolved_attempt", instance.reserve, "fake-next-serial", 10)
                    self.assertIs(instance.status()["needs_reconciliation"], True)
                self.assertEqual(self.path.read_bytes(), before)
        ledger.settle("fake-first-serial", fake_usage())
        self.assertIs(ledger.status()["needs_reconciliation"], False)
        BudgetLedger(self.path).reserve("fake-next-serial", 10)
        status = ledger.status()
        self.assertEqual(status["request_count"], 2)
        self.assertEqual(status["charged_cny"], "0.00102")
        self.assertEqual(status["reserved_cny"], "1.048596")
        self.assertIs(status["needs_reconciliation"], True)

    def test_append_hash_chain_sequence_and_fsync_for_every_record(self):
        with patch.object(budget.os, "fsync", wraps=os.fsync) as synced:
            ledger = BudgetLedger(self.path)
            self.assertEqual(synced.call_count, 1)
            ledger.reserve("fake-durable", 10)
            self.assertEqual(synced.call_count, 2)
            ledger.mark_uncertain("fake-durable", "timeout")
            self.assertEqual(synced.call_count, 3)
            ledger.settle("fake-durable", fake_usage())
            self.assertEqual(synced.call_count, 4)
            ledger.status()
            BudgetLedger(self.path)
            self.assertEqual(synced.call_count, 4)
        previous = "0" * 64
        for index, record in enumerate(self.records(), 1):
            self.assertEqual(record["seq"], index)
            self.assertEqual(record["prev_hash"], previous)
            self.assertTrue(record["timestamp"].endswith("+00:00"))
            previous = record.pop("hash")
            self.assertEqual(previous, sha256(encode(record)).hexdigest())
        self.assertTrue(self.path.read_bytes().endswith(b"\n"))

    def test_fsync_failure_never_returns_permission_to_send(self):
        ledger = BudgetLedger(self.path)
        with patch.object(budget.os, "fsync", side_effect=OSError(errno.EIO, "假写盘失败")):
            self.assert_error("budget_io_error", ledger.reserve, "fake-unsynced", 10)
        self.assertEqual(BudgetLedger(self.path).status()["reserved_cny"], "1.048596")
        self.assert_error("duplicate_request", ledger.reserve, "fake-unsynced", 10)
        self.assert_error("unresolved_attempt", BudgetLedger(self.path).reserve, "fake-rebuilt-unsynced", 10)
        self.assertIs(ledger.status()["needs_reconciliation"], True)

    def test_corrupt_hash_partial_line_and_blank_lines_refused_unchanged(self):
        ledger = BudgetLedger(self.path)
        ledger.reserve("fake-corrupt", 10)
        original = self.path.read_bytes()
        changed_hash = self.records()
        changed_hash[-1]["hash"] = "f" * 64
        variants = [
            b"".join(encode(record) + b"\n" for record in changed_hash),
            original[:-1], original[:-17], original + b'{"seq":3',
            original + b"\n", original + b" \n", original + b"not-json\n",
            original + b"\xff\n", b"[]\n", b"null\n", b"{}\n",
        ]
        for data in variants:
            with self.subTest(data=data[-30:]):
                path = self.fresh_path()
                path.write_bytes(data)
                self.assert_error("ledger_corrupt", BudgetLedger, path)
                self.assertEqual(path.read_bytes(), data)
        self.path.write_bytes(original + b"partial")
        self.assert_error("ledger_corrupt", ledger.status)
        self.assert_error("ledger_corrupt", ledger.reserve, "fake-no-repair", 1)
        self.assertEqual(self.path.read_bytes(), original + b"partial")

    def test_forged_semantics_rejected_even_with_recomputed_hashes(self):
        ledger = BudgetLedger(self.path)
        ledger.reserve("fake-semantic", 10)
        ledger.settle("fake-semantic", fake_usage())
        original = self.records()
        changes = (
            (0, "version", True), (0, "limit_cny", None),
            (0, "limit_cny", "NaN"), (0, "timestamp", "not-a-date"),
            (0, "timestamp", "2026-10-21T00:00:00"),
            (1, "seq", True), (1, "seq", 99),
            (1, "reserved_cny", "0.00"), (1, "max_completion_tokens", True),
            (1, "unexpected", 1), (2, "charged_cny", "0.00"),
            (2, "usage", fake_usage() | {"prompt_tokens": True}),
        )
        for index, key, value in changes:
            with self.subTest(index=index, key=key, value=value):
                records = deepcopy(original)
                records[index][key] = value
                path = self.fresh_path()
                self.write_rehashed(path, records)
                before = path.read_bytes()
                self.assert_error("ledger_corrupt", BudgetLedger, path)
                self.assertEqual(path.read_bytes(), before)

    def test_duplicate_json_fields_and_nonfinite_json_are_corrupt(self):
        BudgetLedger(self.path)
        original = self.path.read_bytes()
        variants = (
            original.replace(b'"seq":1', b'"seq":1,"seq":1'),
            original.replace(b'"seq":1', b'"seq":NaN'),
            original.replace(b'"seq":1', b'"seq":Infinity'),
        )
        for data in variants:
            path = self.fresh_path()
            path.write_bytes(data)
            self.assert_error("ledger_corrupt", BudgetLedger, path)
            self.assertEqual(path.read_bytes(), data)

    def test_reordering_and_missing_middle_event_are_corrupt(self):
        ledger = BudgetLedger(self.path)
        ledger.reserve("fake-middle", 10)
        ledger.mark_uncertain("fake-middle", "timeout")
        ledger.settle("fake-middle", fake_usage())
        lines = self.path.read_bytes().splitlines(keepends=True)
        for rows in ([lines[0], lines[2], lines[1], lines[3]], lines[:1] + lines[2:]):
            path = self.fresh_path()
            path.write_bytes(b"".join(rows))
            self.assert_error("ledger_corrupt", BudgetLedger, path)

    def test_live_instance_never_reinitializes_emptied_file(self):
        ledger = BudgetLedger(self.path)
        ledger.reserve("fake-kept", 10)
        self.path.write_bytes(b"")
        self.assert_error("ledger_corrupt", ledger.status)
        self.assert_error("ledger_corrupt", ledger.reserve, "fake-reset", 1)
        self.assertEqual(self.path.read_bytes(), b"")

    def test_unknown_usage_metadata_is_not_logged(self):
        ledger = BudgetLedger(self.path)
        ledger.reserve("fake-metadata", 10)
        ledger.settle("fake-metadata", fake_usage(
            provider_raw_body="FAKE_PRIVATE_BODY", credential="FAKE_SECRET",
            prompt_tokens_details={"cached_tokens": 100, "raw": "FAKE_PRIVATE_BODY"},
            web_search_usage={"tool_usage": 0, "page_usage": 0, "raw": "FAKE_PRIVATE_BODY"},
        ))
        raw = self.path.read_bytes()
        self.assertNotIn(b"FAKE_PRIVATE_BODY", raw)
        self.assertNotIn(b"FAKE_SECRET", raw)

    def test_decimal_global_precision_cannot_change_money(self):
        with localcontext() as context:
            context.prec = 2
            context.traps[Inexact] = True
            ledger = BudgetLedger(self.path)
            self.assertEqual(ledger.reserve("fake-context", 100)["reserved_cny"], "1.048776")
            result = ledger.settle("fake-context", fake_usage(1, 0, prompt_tokens_details={"cached_tokens": 1}))
            self.assertEqual(result["charged_cny"], "0.00000002")
            self.assertEqual(ledger.status()["available_cny"], "4.99999998")
        self.assertEqual(BudgetLedger(self.path).status()["charged_cny"], "0.00000002")

    def test_thread_contention_returns_busy_without_wait_or_retry(self):
        ledger = BudgetLedger(self.path)
        other = BudgetLedger(self.path)
        errors = []

        def attempt():
            try:
                other.reserve("fake-thread", 10)
            except BudgetError as exc:
                errors.append(exc.code)

        before = self.path.read_bytes()
        with ledger._locked():
            worker = threading.Thread(target=attempt)
            worker.start()
            worker.join(timeout=5)
            finished_without_unlock = not worker.is_alive()
            self.assert_error("budget_busy", ledger.status)
            self.assert_error("budget_busy", BudgetLedger, self.path)
        worker.join(timeout=5)
        self.assertTrue(finished_without_unlock)
        self.assertEqual(errors, ["budget_busy"])
        self.assertEqual(self.path.read_bytes(), before)
        ledger.reserve("fake-after-release", 10)
        self.assertTrue(Path(str(self.path) + ".lock").exists())

    def test_process_file_lock_is_nonblocking_and_retained(self):
        ledger = BudgetLedger(self.path)
        before = self.path.read_bytes()
        with ledger._locked():
            self.assertEqual(self.child(
                "from pathlib import Path\nimport sys\n"
                "from cocoon.budget import BudgetError, BudgetLedger\n"
                "try:\n    BudgetLedger(Path(sys.argv[1]))\n"
                "except BudgetError as exc:\n    print(exc.code)\n"
                "else:\n    raise AssertionError('fake lock missed')\n"
            ), "budget_busy")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(Path(str(self.path) + ".lock").exists())
        self.assertEqual(BudgetLedger(self.path).status()["request_count"], 0)

    def test_mock_lock_contention_and_other_io_errors_do_not_retry(self):
        ledger = BudgetLedger(self.path)
        for error, code in (
            (BlockingIOError(errno.EAGAIN, "假锁竞争"), "budget_busy"),
            (PermissionError(errno.EACCES, "假锁竞争"), "budget_busy"),
            (OSError(errno.EIO, "假锁 I/O 错误"), "budget_io_error"),
        ):
            with self.subTest(code=code):
                with patch.object(budget, "_lock_file", side_effect=error) as lock:
                    self.assert_error(code, ledger.reserve, "fake-lock", 1)
                    self.assertEqual(lock.call_count, 1)
                self.assertEqual(ledger.status()["request_count"], 0)
        ledger.reserve("fake-lock", 1)
        self.assertTrue(Path(str(self.path) + ".lock").exists())

    def test_platform_lock_flags_are_explicitly_nonblocking(self):
        ledger = BudgetLedger(self.path)
        if os.name == "nt":
            with patch.object(budget.msvcrt, "locking", wraps=budget.msvcrt.locking) as locking:
                ledger.status()
                self.assertEqual([call.args[1:] for call in locking.call_args_list], [
                    (budget.msvcrt.LK_NBLCK, 1), (budget.msvcrt.LK_UNLCK, 1),
                ])
        else:
            with patch.object(budget.fcntl, "flock", wraps=budget.fcntl.flock) as flock:
                ledger.status()
                self.assertEqual([call.args[1] for call in flock.call_args_list], [
                    budget.fcntl.LOCK_EX | budget.fcntl.LOCK_NB, budget.fcntl.LOCK_UN,
                ])


class RaiseLimitTests(unittest.TestCase):
    """步骤41：受控提额事件写入哈希链、可重放、只升不降。"""

    def setUp(self):
        self.directory = Path(mkdtemp(prefix="budget_fake_free_raise_", dir=ROOT / "tests"))
        self.path = self.directory / "fake_free_not_invoice.jsonl"

    def records(self):
        return [json.loads(line) for line in self.path.read_bytes().splitlines()]

    def assert_code(self, code, function, *args, **kwargs):
        with self.assertRaises(BudgetError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_raise_extends_capacity_and_replays(self):
        ledger = BudgetLedger(self.path, "1.00")
        self.assert_code("budget_exceeded", ledger.reserve, "fake-big", MAX_COMPLETION_TOKENS)
        status = ledger.raise_limit("2.00", "fake_test_raise")
        self.assertEqual(status["limit_cny"], "2.00")
        self.assertEqual(status["initial_limit_cny"], "1.00")
        self.assertEqual(status["limit_raise_count"], 1)
        ledger.reserve("fake-big", MAX_COMPLETION_TOKENS)
        ledger.settle("fake-big", fake_usage())
        reopened = BudgetLedger(self.path, "1.00")
        self.assertEqual(reopened.status(), ledger.status())
        rows = self.records()
        raise_row = next(row for row in rows if row["type"] == "raise_limit")
        self.assertEqual((raise_row["previous_limit_cny"], raise_row["new_limit_cny"],
                          raise_row["reason"]), ("1.00", "2.00", "fake_test_raise"))
        self.assertEqual(raise_row["seq"], 2)
        ledger.raise_limit("50.00", "fake_second_raise")
        self.assertEqual(BudgetLedger(self.path, "1.00").status()["limit_raise_count"], 2)

    def test_raise_must_strictly_increase_and_reason_is_short_code(self):
        ledger = BudgetLedger(self.path, "5.00")
        before = self.path.read_bytes()
        self.assert_code("limit_not_increased", ledger.raise_limit, "5.00", "fake_same")
        self.assert_code("limit_not_increased", ledger.raise_limit, "4.00", "fake_lower")
        for reason in ("", "Has Space", "中文原因", "x" * 121, None):
            self.assert_code("invalid_reason", ledger.raise_limit, "6.00", reason)
        self.assertEqual(self.path.read_bytes(), before)

    def test_constructor_still_binds_initial_limit(self):
        BudgetLedger(self.path, "5.00").raise_limit("50.00", "fake_raise")
        self.assertEqual(BudgetLedger(self.path, "5.00").status()["limit_cny"], "50.00")
        self.assert_code("limit_mismatch", BudgetLedger, self.path, "50.00")

    def test_blocked_ledger_refuses_raise(self):
        ledger = BudgetLedger(self.path, "5.00")
        ledger.reserve("fake-anomaly", 10)
        with self.assertRaises(BudgetError):
            ledger.settle("fake-anomaly", fake_usage(completion=11))
        self.assertTrue(ledger.status()["blocked"])
        self.assert_code("budget_blocked", ledger.raise_limit, "10.00", "fake_raise")

    def test_tampered_raise_rejected_on_replay(self):
        BudgetLedger(self.path, "5.00").raise_limit("50.00", "fake_raise")
        # 不重算哈希的改写：哈希链直接拦截。
        rows = self.records()
        rows[1]["new_limit_cny"] = "500.00"
        unhashed = self.directory / "tampered_unhashed.jsonl"
        unhashed.write_bytes(b"".join(encode(row) + b"\n" for row in rows))
        self.assert_code("ledger_corrupt", BudgetLedger, unhashed, "5.00")
        # 重算哈希后内容违规（上限对不上、不升反降、原因非短码）：回放语义校验拦截。
        for field, value in (("previous_limit_cny", "4.00"),
                             ("new_limit_cny", "3.00"), ("reason", "Bad Reason")):
            with self.subTest(field=field, value=value):
                rows = self.records()
                rows[1][field] = value
                rows[1].pop("hash")
                rows[1]["hash"] = sha256(encode(rows[1])).hexdigest()
                tampered = self.directory / f"tampered_{field}_{value.replace(' ', '_')}.jsonl"
                tampered.write_bytes(b"".join(encode(row) + b"\n" for row in rows))
                self.assert_code("ledger_corrupt", BudgetLedger, tampered, "5.00")


class HeldInterruptedTests(unittest.TestCase):
    """步骤40：仅用户逐条授权的中断在途预留可被“持有”，其余未决原因照旧串行阻塞。"""

    HELD = next(iter(budget.HELD_INTERRUPTED_REQUEST_IDS))

    def setUp(self):
        self.directory = Path(mkdtemp(prefix="budget_fake_free_held_", dir=ROOT / "tests"))

    def ledger(self, name):
        return BudgetLedger(self.directory / name)

    def test_allowlist_is_single_explicit_request(self):
        self.assertEqual(len(budget.HELD_INTERRUPTED_REQUEST_IDS), 1)
        self.assertTrue(self.HELD.startswith("live_micro_pilot_TOPIC_01_"))

    def test_held_interrupted_allows_next_reserve_and_keeps_worst_case(self):
        ledger = self.ledger("held.jsonl")
        ledger.reserve(self.HELD, 1024)
        with self.assertRaises(BudgetError) as caught:
            ledger.reserve("fake-next-before-mark", 10)
        self.assertEqual(caught.exception.code, "unresolved_attempt")
        ledger.mark_uncertain(self.HELD, "interrupted")
        held_reserved = Decimal(ledger.status()["reserved_cny"])
        ledger.reserve("fake-next", 10)
        ledger.settle("fake-next", fake_usage())
        status = ledger.status()
        self.assertEqual(Decimal(status["reserved_cny"]), held_reserved)
        self.assertEqual(status["unresolved_request_count"], 1)
        self.assertEqual(status["held_interrupted_request_count"], 1)
        self.assertIs(status["needs_reconciliation"], True)
        self.assertIsNone(status["total_cost_cny"])
        # 回放（新实例）结果一致；真实用量到位后仍可补结算。
        self.assertEqual(self.ledger("held.jsonl").status(), status)
        ledger.settle(self.HELD, fake_usage())
        self.assertEqual(ledger.status()["unresolved_request_count"], 0)

    def test_other_reasons_or_ids_still_block(self):
        ledger = self.ledger("other_reason.jsonl")
        ledger.reserve(self.HELD, 1024)
        ledger.mark_uncertain(self.HELD, "timeout")
        with self.assertRaises(BudgetError) as caught:
            ledger.reserve("fake-next", 10)
        self.assertEqual(caught.exception.code, "unresolved_attempt")
        other = self.ledger("other_id.jsonl")
        other.reserve("fake-not-allowlisted", 1024)
        other.mark_uncertain("fake-not-allowlisted", "interrupted")
        with self.assertRaises(BudgetError) as caught:
            other.reserve("fake-next", 10)
        self.assertEqual(caught.exception.code, "unresolved_attempt")
        self.assertEqual(other.status()["held_interrupted_request_count"], 0)


class HeldHttpErrorTests(unittest.TestCase):
    """步骤42：仅用户逐条授权的单条 HTTP 500 请求（uncertain/http_error）可被持有。"""

    HELD = next(iter(budget.HELD_HTTP_ERROR_REQUEST_IDS))

    def setUp(self):
        self.directory = Path(mkdtemp(prefix="budget_fake_free_http_", dir=ROOT / "tests"))

    def ledger(self, name):
        return BudgetLedger(self.directory / name)

    def test_allowlist_is_single_explicit_request(self):
        self.assertEqual(len(budget.HELD_HTTP_ERROR_REQUEST_IDS), 1)
        self.assertTrue(self.HELD.startswith("closed_world_synth01_"))
        self.assertFalse(budget.HELD_HTTP_ERROR_REQUEST_IDS & budget.HELD_INTERRUPTED_REQUEST_IDS)

    def test_held_http_error_allows_next_reserve_and_keeps_worst_case(self):
        ledger = self.ledger("held_http.jsonl")
        ledger.reserve(self.HELD, 2048)
        ledger.mark_uncertain(self.HELD, "http_error")
        held_reserved = Decimal(ledger.status()["reserved_cny"])
        ledger.reserve("fake-next", 10)
        ledger.settle("fake-next", fake_usage())
        status = ledger.status()
        self.assertEqual(Decimal(status["reserved_cny"]), held_reserved)
        self.assertEqual(status["held_interrupted_request_count"], 1)
        self.assertIs(status["needs_reconciliation"], True)
        self.assertEqual(self.ledger("held_http.jsonl").status(), status)

    def test_same_id_other_reason_or_other_id_still_blocks(self):
        ledger = self.ledger("http_other_reason.jsonl")
        ledger.reserve(self.HELD, 1024)
        ledger.mark_uncertain(self.HELD, "interrupted")
        with self.assertRaises(BudgetError) as caught:
            ledger.reserve("fake-next", 10)
        self.assertEqual(caught.exception.code, "unresolved_attempt")
        other = self.ledger("http_other_id.jsonl")
        other.reserve("fake-not-allowlisted", 1024)
        other.mark_uncertain("fake-not-allowlisted", "http_error")
        with self.assertRaises(BudgetError) as caught:
            other.reserve("fake-next", 10)
        self.assertEqual(caught.exception.code, "unresolved_attempt")


if __name__ == "__main__":
    unittest.main()
