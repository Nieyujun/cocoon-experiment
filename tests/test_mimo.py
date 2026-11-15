"""仅使用离线 HTTP mock 和预算 stub；临时审计产物保留在 tests 下。"""
import copy
from contextlib import ExitStack, contextmanager
from email.message import Message
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@contextmanager
def offline_network():
    targets = ("socket.socket.connect", "socket.socket.connect_ex", "socket.socket.sendto",
               "socket.create_connection", "socket.getaddrinfo", "urllib.request.urlopen",
               "urllib.request.OpenerDirector.open", "urllib.request.HTTPHandler.http_open",
               "urllib.request.HTTPSHandler.https_open")
    with ExitStack() as stack:
        guards = [stack.enter_context(mock.patch(name, side_effect=AssertionError("离线测试禁止联网")))
                  for name in targets]
        yield guards


with offline_network() as import_guards:
    from cocoon.mimo import (MIMO_ENDPOINT, MIMO_MODEL, MIMO_ERROR_CODES, MiMoError,
                             MiMoTransport, NoRedirect, credential_status, load_key)
    from cocoon.runtime import EventStore, canonical_hash, verify_events
    from cocoon.budget import BudgetError, UNCERTAIN_REASONS
    for guard in import_guards:
        guard.assert_not_called()


KEY = "sk-offline-test-fixture"
CONFIG = {"provider": "xiaomi_mimo", "model": MIMO_MODEL, "endpoint": MIMO_ENDPOINT,
          "credential_env": "MIMO_API_KEY", "temperature": 0, "max_tokens": 4096,
          "thinking": "disabled", "timeout_seconds": 60, "live_enabled": True}
USAGE = {"prompt_tokens": 20, "completion_tokens": 4, "total_tokens": 24,
         "prompt_tokens_details": {"cached_tokens": 12},
         "completion_tokens_details": {"reasoning_tokens": 0}}
MAX_BYTES = 2 * 1024 * 1024


def make_request(request_id="assistant-task:connectivity:1"):
    return {"request_id": request_id, "stage": "connectivity", "is_demo": False,
            "model_settings": {"provider": "xiaomi_mimo", "model": MIMO_MODEL,
                               "temperature": 0, "max_tokens": 128},
            "messages": [{"role": "system", "content": "只输出严格 JSON，不调用工具。"},
                         {"role": "user", "content": '请输出 {"ok":true}。'}]}


def make_response():
    return {"object": "chat.completion", "id": "offline-response-1", "model": MIMO_MODEL,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": ' {"ok":true}\n'},
                         "finish_reason": "stop"}], "usage": copy.deepcopy(USAGE)}


def raw_json(value):
    return (" \r\n" + json.dumps(value, ensure_ascii=False, indent=2) + "\r\n\t").encode("utf-8")


def headers(values=None):
    result = Message()
    for name, value in (values or {}).items():
        result[name] = value
    return result


class StubBudgetError(BudgetError):
    pass


class StubLedger:
    def __init__(self, *, blocked=False, settle_error=False):
        self.blocked = blocked
        self.settle_error = settle_error
        self.calls = []
        self.reservations = {}
        self.settled = {}
        self.uncertain = {}

    def reserve(self, request_id, max_tokens):
        self.calls.append(("reserve", request_id, max_tokens))
        if self.blocked:
            raise StubBudgetError("budget_exceeded")
        if request_id in self.reservations or request_id in self.settled:
            raise StubBudgetError("duplicate_request")
        self.reservations[request_id] = max_tokens

    def settle(self, request_id, raw_usage):
        self.calls.append(("settle", request_id, copy.deepcopy(raw_usage)))
        if self.settle_error:
            raise StubBudgetError("invalid_usage" if self.settle_error is True else self.settle_error)
        self.settled[request_id] = copy.deepcopy(raw_usage)
        self.reservations.pop(request_id)

    def mark_uncertain(self, request_id, reason):
        if reason not in UNCERTAIN_REASONS:
            raise StubBudgetError("invalid_reason")
        self.calls.append(("mark_uncertain", request_id, reason))
        self.uncertain[request_id] = reason

    def status(self):
        return {"reserved_cny": "0.010000" if self.reservations else "0.000000",
                "spent_cny": "0.000024" if self.settled else "0.000000",
                "uncertain_requests": len(self.uncertain)}


class CountingStream(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)


class StubResponse:
    def __init__(self, data, status=200, response_headers=None, read_error=None):
        self.data = data
        self.status = status
        self.headers = headers(response_headers)
        self.read_error = read_error
        self.read_sizes = []
        self.closed = False

    def read(self, size):
        self.read_sizes.append(size)
        if self.read_error:
            raise self.read_error
        return self.data[:size]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True


class MiMoOfflineTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.guards = self.stack.enter_context(offline_network())
        self.stack.enter_context(mock.patch.object(os, "environ", {"MIMO_API_KEY": KEY}))
        self.opener = mock.Mock()
        self.build = self.stack.enter_context(mock.patch("urllib.request.build_opener", return_value=self.opener))
        self.stores = []

    def tearDown(self):
        for guard in self.guards:
            guard.assert_not_called()
        for store in self.stores:
            self.assertTrue(verify_events(store.root)["chain_valid"])
            self.assert_not_persisted(store, KEY)

    def make_transport(self, config=None, ledger=None, *, is_demo=False):
        self.assertTrue((ROOT / "tests").is_dir())
        root = Path(tempfile.mkdtemp(prefix="mimo_offline_", dir=ROOT / "tests"))
        store = EventStore(root, {"suite": "mimo_offline_http_mock"}, canonical_hash({"fixture": True}),
                           is_demo, "mimo_offline_test")
        self.stores.append(store)
        ledger = StubLedger() if ledger is None else ledger
        transport = MiMoTransport(copy.deepcopy(CONFIG) if config is None else config, store, ledger)
        return transport, store, ledger

    def respond(self, value=None, *, data=None, status=200, response_headers=None, read_error=None):
        response = StubResponse(raw_json(make_response() if value is None else value) if data is None else data,
                                status, response_headers, read_error)
        self.opener.side_effect = None
        self.opener.open.side_effect = None
        self.opener.open.return_value = response
        return response

    def events(self, store):
        return [json.loads(line) for line in (store.root / "events/events.jsonl").read_text(encoding="utf-8").splitlines()]

    def blob(self, store, reference):
        return json.loads((store.root / reference).read_text(encoding="utf-8"))

    def raw_response(self, store):
        event = next(row for row in self.events(store) if row["event_type"] == "mimo_response_received")
        return self.blob(store, event["output_ref"]), event

    def assert_not_persisted(self, store, value):
        for path in store.root.rglob("*"):
            if path.is_file():
                self.assertNotIn(value.encode("utf-8"), path.read_bytes(), str(path))

    def expect_error(self, transport, request, code):
        before = self.opener.open.call_count
        with self.assertRaises(MiMoError) as caught:
            transport.complete(request)
        self.assertEqual(caught.exception.code, code)
        self.assertNotIn(KEY, str(caught.exception))
        self.assertNotIn(KEY, repr(caught.exception))
        self.assertNotIn(KEY, json.dumps(caught.exception.details))
        self.assertLessEqual(self.opener.open.call_count - before, 1)
        return caught.exception

    def test_header_wire_body_smoke_override_raw_and_full_usage(self):
        transport, store, ledger = self.make_transport()
        self.assertEqual(transport.execution_mode, "live_probe")
        self.assertEqual(transport.actual_model_calls, 0)
        request = make_request()
        original = copy.deepcopy(request)
        response = self.respond(response_headers={"X-Request-ID": "offline-http-1", "Request-ID": "req:1",
                                                 "Set-Cookie": "unknown-offline-cookie", "X-Other": KEY})
        result = transport.complete(request)
        self.assertEqual(request, original)
        self.assertEqual(transport.actual_model_calls, 1)
        self.opener.open.assert_called_once()
        self.build.assert_called_once()
        self.assertIsInstance(self.build.call_args.args[0], NoRedirect)
        wire = self.opener.open.call_args.args[0]
        self.assertEqual(wire.full_url, MIMO_ENDPOINT)
        self.assertEqual(wire.get_method(), "POST")
        self.assertEqual({name.lower(): value for name, value in wire.header_items()},
                         {"api-key": KEY, "content-type": "application/json", "accept": "application/json"})
        self.assertEqual(self.opener.open.call_args.kwargs, {"timeout": 60})
        body = json.loads(wire.data)
        self.assertEqual(body, {"model": MIMO_MODEL, "messages": request["messages"],
                               "max_completion_tokens": 128, "temperature": 0,
                               "thinking": {"type": "disabled"}, "stream": False})
        self.assertNotIn(KEY.encode(), wire.data)
        self.assertTrue({"tools", "top_p", "seed", "response_format", "max_tokens", "network", "search"}.isdisjoint(body))
        self.assertEqual(set(result), {"raw_text", "provider", "model", "response_id", "usage", "finish_reason", "is_demo"})
        self.assertIs(result["is_demo"], False)
        self.assertEqual(result["raw_text"], ' {"ok":true}\n')
        self.assertEqual(result["usage"], {name: USAGE[name] for name in ("prompt_tokens", "completion_tokens", "total_tokens")})
        self.assertEqual(ledger.settled[request["request_id"]], USAGE)
        self.assertEqual([call[0] for call in ledger.calls], ["reserve", "settle"])
        self.assertEqual(ledger.calls[0][2], 128)
        raw, event = self.raw_response(store)
        self.assertEqual(raw, {"raw_http_text": response.data.decode("utf-8")})
        self.assertEqual(event["payload"]["headers"], {"request-id": "req:1", "x-request-id": "offline-http-1"})
        self.assertEqual(event["payload"]["response_sha256"], hashlib.sha256(response.data).hexdigest())
        self.assertEqual(response.read_sizes, [MAX_BYTES + 1])
        self.assertTrue(response.closed)
        prepared = self.events(store)[0]
        self.assertEqual(prepared["event_type"], "mimo_request_prepared")
        self.assertEqual(self.blob(store, prepared["input_ref"]), body)
        self.assertEqual(prepared["task_id"], request["request_id"])
        self.assertNotEqual(prepared["task_id"], "assistant-task")
        self.assert_not_persisted(store, "unknown-offline-cookie")
        self.assertNotIn(KEY, repr(transport))
        self.assertNotIn(KEY, repr(vars(transport)))

    def test_credentials_only_environment_and_safe_status(self):
        self.assertEqual(load_key(), KEY)
        self.assertEqual(credential_status(), {"env": "MIMO_API_KEY", "present": True, "usable_format": True})
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(MiMoError) as caught:
                load_key()
            self.assertEqual(caught.exception.code, "missing_credential")
            self.assertEqual(credential_status(), {"env": "MIMO_API_KEY", "present": False, "usable_format": False})
        for value in ("", "sk-", "wrong", "sk-中文", "sk-has space", "sk-has\nnewline", "sk-tab\t", "sk-null\x00", "sk-del\x7f"):
            with self.subTest(格式="非法凭据"), mock.patch.dict(os.environ, {"MIMO_API_KEY": value}):
                with self.assertRaises(MiMoError) as caught:
                    load_key()
                self.assertEqual(caught.exception.code, "invalid_credential")
                self.assertFalse(credential_status()["usable_format"])
        with mock.patch.dict(os.environ, {"OFFLINE_CREDENTIAL": KEY}):
            self.assertEqual(load_key("OFFLINE_CREDENTIAL"), KEY)
        for name in (None, "", "A=B", "A\nB", []):
            with self.assertRaises(MiMoError):
                load_key(name)
        self.build.assert_not_called()

    def test_error_codes_and_details_never_accept_arbitrary_secrets(self):
        for code in MIMO_ERROR_CODES:
            error = MiMoError(code, {"status": 429, "response_sha256": "a" * 64,
                                    "response_bytes": 22, "omitted_for_credentials": True,
                                    "message": KEY, "api_key": KEY, "headers": {"secret": KEY}})
            self.assertEqual(error.code, code)
            self.assertEqual(set(error.details), {"status", "response_sha256", "response_bytes", "omitted_for_credentials"})
            self.assertNotIn(KEY, repr(error))
            self.assertNotIn(KEY, json.dumps(error.details))
        error = MiMoError(KEY, {"status": KEY, "response_sha256": KEY, "response_bytes": True})
        self.assertEqual(error.code, "transport_error")
        self.assertEqual(error.details, {})

    def test_forbidden_endpoints_and_token_plan_rejected(self):
        endpoints = [MIMO_ENDPOINT.replace("https:", "http:"), MIMO_ENDPOINT + "/", MIMO_ENDPOINT + "?key=hidden",
                     MIMO_ENDPOINT + "#fragment", MIMO_ENDPOINT.replace("api.xiaomimimo.com", "api.xiaomimimo.com.invalid"),
                     MIMO_ENDPOINT.replace("//", "//user:pass@"), MIMO_ENDPOINT.replace("/v1/", "/tp/v1/"),
                     MIMO_ENDPOINT.replace("/v1/", "/ttp/v1/")]
        for endpoint in endpoints:
            with self.subTest(地址="非白名单"), self.assertRaises(MiMoError) as caught:
                self.make_transport(dict(CONFIG, endpoint=endpoint))
            self.assertEqual(caught.exception.code, "forbidden_endpoint")
        for provider in ("tp", "ttp", "token_plan", "other"):
            with self.subTest(provider=provider), self.assertRaises(MiMoError) as caught:
                self.make_transport(dict(CONFIG, provider=provider))
            self.assertEqual(caught.exception.code, "invalid_config")
        self.build.assert_not_called()

    def test_config_is_strict_and_not_mutated_by_caller(self):
        cases = [dict(CONFIG, live_enabled=False), dict(CONFIG, live_enabled=1), dict(CONFIG, thinking="enabled"),
                 dict(CONFIG, temperature=True), dict(CONFIG, temperature=float("nan")), dict(CONFIG, temperature=1.6),
                 dict(CONFIG, temperature=10 ** 400), dict(CONFIG, max_tokens=True), dict(CONFIG, timeout_seconds=True),
                 dict(CONFIG, timeout_seconds=float("inf")), dict(CONFIG, timeout_seconds=0),
                 dict(CONFIG, timeout_seconds=10 ** 400), dict(CONFIG, credential_env="OTHER"),
                 dict(CONFIG, token_plan=True), dict(CONFIG, api_key=KEY)]
        cases += [{name: value for name, value in CONFIG.items() if name != missing} for missing in CONFIG]
        for config in cases:
            with self.subTest(配置="拒绝缺失或额外字段"), self.assertRaises(MiMoError):
                self.make_transport(config)
        config = dict(CONFIG)
        transport, _, _ = self.make_transport(config)
        config["endpoint"] += "/changed"
        self.assertEqual(transport.config["endpoint"], MIMO_ENDPOINT)
        transport.config["live_enabled"] = False
        self.expect_error(transport, make_request(), "live_disabled")
        self.assertEqual(transport.actual_model_calls, 0)
        self.build.assert_not_called()

    def test_max_tokens_above_131072_rejected_before_any_http_call(self):
        ledger = StubLedger()
        with self.assertRaises(MiMoError) as caught:
            self.make_transport(dict(CONFIG, max_tokens=131073), ledger=ledger)
        self.assertEqual(caught.exception.code, "invalid_config")
        self.assertEqual(ledger.calls, [])

        transport, store, ledger = self.make_transport(dict(CONFIG, max_tokens=131072))
        request = make_request()
        request["model_settings"]["max_tokens"] = 131073
        self.expect_error(transport, request, "invalid_request")
        transport.config["max_tokens"] = 131073
        self.expect_error(transport, make_request(), "invalid_config")
        self.assertEqual(transport.actual_model_calls, 0)
        self.assertEqual(ledger.calls, [])
        for store in self.stores:
            self.assertEqual(self.events(store), [])
            self.assertEqual(list((store.root / "raw/blobs").iterdir()), [])
        self.build.assert_not_called()
        self.opener.open.assert_not_called()

    def test_validation_before_capture_reserve_and_http(self):
        cases = []
        for extra in ("tools", "top_p", "seed", "response_format", "settings", "search"):
            request = make_request()
            request[extra] = True
            cases.append((request, "invalid_request"))
        for name, value in (("provider", "tp"), ("model", "other"), ("temperature", 0.1),
                            ("temperature", True), ("max_tokens", 4097), ("max_tokens", True), ("max_tokens", 0)):
            request = make_request()
            request["model_settings"][name] = value
            cases.append((request, "invalid_request"))
        for messages in ([], make_request()["messages"] * 2, [{"role": "assistant", "content": "hello"}],
                         [{"role": "user", "content": [{"type": "text", "text": "hello"}]}],
                         [{"role": "user", "content": "hello", "name": "extra"}],
                         [{"role": "user", "content": "\ud800"}]):
            request = make_request()
            request["messages"] = messages
            cases.append((request, "invalid_request"))
        for marker in (True, 0, "false", None):
            request = make_request()
            request["is_demo"] = marker
            cases.append((request, "mode_mismatch"))
        request = make_request()
        request["messages"][1]["content"] = "不要意外发送 " + KEY
        cases.append((request, "credential_in_request"))
        request = make_request(KEY)
        cases.append((request, "credential_in_request"))
        for request, code in cases:
            with self.subTest(错误=code):
                transport, store, ledger = self.make_transport()
                self.expect_error(transport, request, code)
                self.assertEqual(ledger.calls, [])
                self.assertEqual(self.events(store), [])
                self.assertEqual(list((store.root / "raw/blobs").iterdir()), [])
                self.assertEqual(transport.actual_model_calls, 0)
        self.build.assert_not_called()

    def test_missing_key_and_demo_store_never_reach_ledger(self):
        transport, store, ledger = self.make_transport()
        with mock.patch.dict(os.environ, {}, clear=True):
            self.expect_error(transport, make_request(), "missing_credential")
        with self.assertRaises(MiMoError) as caught:
            self.make_transport(is_demo=True)
        self.assertEqual(caught.exception.code, "mode_mismatch")
        self.assertEqual(ledger.calls, [])
        self.assertEqual(self.events(store), [])
        self.build.assert_not_called()

    def test_context_utf8_byte_boundary_and_no_truncation(self):
        for text, allowed in (("中" * 83333 + "a", True), ("中" * 83333 + "ab", False)):
            with self.subTest(允许=allowed):
                transport, store, ledger = self.make_transport()
                request = make_request()
                request["messages"] = [{"role": "user", "content": text}]
                before = self.opener.open.call_count
                self.respond()
                if allowed:
                    transport.complete(request)
                    self.assertEqual(json.loads(self.opener.open.call_args.args[0].data)["messages"][0]["content"], text)
                    self.assertEqual(self.opener.open.call_count, before + 1)
                else:
                    self.expect_error(transport, request, "context_too_large")
                    self.assertEqual(ledger.calls, [])
                    self.assertEqual(self.events(store), [])
                    self.assertEqual(self.opener.open.call_count, before)

    def test_budget_blocked_records_wire_but_makes_zero_http_attempts(self):
        ledger = StubLedger(blocked=True)
        transport, store, _ = self.make_transport(ledger=ledger)
        with self.assertRaises(StubBudgetError) as caught:
            transport.complete(make_request())
        self.assertEqual(caught.exception.code, "budget_exceeded")
        self.assertEqual(transport.actual_model_calls, 0)
        self.build.assert_not_called()
        self.opener.open.assert_not_called()
        self.assertEqual([row["event_type"] for row in self.events(store)], ["mimo_request_prepared"])

    def test_capture_and_reservation_precede_open(self):
        transport, store, ledger = self.make_transport()
        response = self.respond()
        request = make_request()

        def open_once(wire, timeout):
            self.assertEqual(ledger.reservations, {request["request_id"]: 128})
            self.assertEqual(self.events(store)[0]["event_type"], "mimo_request_prepared")
            self.assertEqual(transport.actual_model_calls, 1)
            return response

        self.opener.open.side_effect = open_once
        transport.complete(request)
        self.opener.open.assert_called_once()

    def test_http_errors_map_codes_keep_raw_and_never_retry(self):
        mapping = {301: "redirect_blocked", 302: "redirect_blocked", 399: "redirect_blocked", 400: "http_error",
                   401: "auth_failed", 403: "forbidden", 429: "rate_limited", 500: "upstream_error", 503: "upstream_error"}
        for status, code in mapping.items():
            with self.subTest(status=status):
                transport, store, ledger = self.make_transport()
                body = raw_json({"error": {"message": "合成上游错误，不应进入异常详情。"}})
                stream = io.BytesIO(body)
                error = urllib.error.HTTPError(MIMO_ENDPOINT, status, KEY, headers({"X-Request-ID": "http-error-1"}), stream)
                self.opener.open.side_effect = error
                before = self.opener.open.call_count
                caught = self.expect_error(transport, make_request(), code)
                self.assertEqual(self.opener.open.call_count, before + 1)
                self.assertEqual(transport.actual_model_calls, 1)
                self.assertEqual(caught.details["status"], status)
                self.assertNotIn("message", caught.details)
                self.assertEqual(self.raw_response(store)[0]["raw_http_text"], body.decode())
                self.assertTrue(ledger.reservations)
                self.assertTrue(ledger.uncertain)
                self.assertTrue(stream.closed)

    def test_no_redirect_handler_never_opens_location(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                transport, store, ledger = self.make_transport()
                body = raw_json({"error": "合成重定向"})
                location = MIMO_ENDPOINT.replace("api.xiaomimimo.com", "redirect.invalid")

                def redirect(wire, timeout):
                    handler = self.build.call_args.args[0]
                    handler.parent = mock.Mock()
                    self.assertIsNone(handler.redirect_request(wire, None, status, KEY, {}, location))
                    try:
                        getattr(handler, "http_error_" + str(status))(
                            wire, io.BytesIO(body), status, KEY, headers({"Location": location}))
                    finally:
                        handler.parent.open.assert_not_called()

                self.opener.open.side_effect = redirect
                before = self.opener.open.call_count
                self.expect_error(transport, make_request(), "redirect_blocked")
                self.assertEqual(self.opener.open.call_count, before + 1)
                self.assertEqual(self.raw_response(store)[0]["raw_http_text"], body.decode())
                self.assert_not_persisted(store, location)
                self.assertTrue(ledger.uncertain)

    def test_timeout_connection_failures_keep_reserve_and_stop_later_calls(self):
        cases = ((TimeoutError(KEY), "timeout"), (urllib.error.URLError(TimeoutError(KEY)), "timeout"),
                 (urllib.error.URLError(KEY), "connection_error"), (ConnectionError(KEY), "connection_error"),
                 (RuntimeError(KEY), "transport_error"))
        for error, code in cases:
            with self.subTest(错误=code):
                transport, _, ledger = self.make_transport()
                self.opener.open.side_effect = error
                before = self.opener.open.call_count
                self.expect_error(transport, make_request(), code)
                self.assertEqual(self.opener.open.call_count, before + 1)
                self.assertEqual(transport.actual_model_calls, 1)
                self.assertEqual(ledger.reservations, {make_request()["request_id"]: 128})
                self.assertFalse(ledger.settled)
                with self.assertRaises(MiMoError) as caught:
                    transport.complete(make_request("next-request"))
                self.assertEqual(caught.exception.code, "unresolved_attempt")
                self.assertEqual(self.opener.open.call_count, before + 1)
                self.assertEqual(transport.actual_model_calls, 1)

    def test_read_timeout_closes_response_and_keeps_reserve(self):
        transport, _, ledger = self.make_transport()
        response = self.respond(read_error=TimeoutError(KEY))
        self.expect_error(transport, make_request(), "timeout")
        self.assertTrue(response.closed)
        self.assertEqual(response.read_sizes, [MAX_BYTES + 1])
        self.assertTrue(ledger.reservations)
        self.assertEqual(transport.actual_model_calls, 1)

    def test_invalid_json_and_encoding_stop_without_releasing_reserve(self):
        cases = ((b'{"unfinished":', "invalid_json"), (b'{"same":1,"same":2}', "invalid_json"),
                 (b'{"v":NaN}', "invalid_json"), (b'\xffbad', "invalid_encoding"))
        for data, code in cases:
            with self.subTest(错误=code):
                transport, store, ledger = self.make_transport()
                self.respond(data=data)
                self.expect_error(transport, make_request(), code)
                captured, _ = self.raw_response(store)
                self.assertNotIn("raw_http_text", captured)
                self.assertEqual(captured["response_sha256"], hashlib.sha256(data).hexdigest())
                if code == "invalid_json":
                    self.assertTrue(captured["omitted_due_to_incomplete_security_scan"])
                self.assertTrue(ledger.reservations)
                self.assertTrue(ledger.uncertain)
                self.assertFalse(ledger.settled)

    def test_incomplete_json_with_escaped_key_keeps_only_hash_and_reserve(self):
        escaped_key = "\\u0073" + KEY[1:]
        self.assertEqual(json.loads('"' + escaped_key + '"'), KEY)
        data = b'{"error":"' + escaped_key.encode("ascii") + b'",'
        self.assertNotIn(KEY.encode("ascii"), data)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(data)

        transport, store, ledger = self.make_transport()
        request = make_request()
        response = self.respond(data=data)
        error = self.expect_error(transport, request, "invalid_json")
        digest = hashlib.sha256(data).hexdigest()
        captured, event = self.raw_response(store)
        self.assertEqual(captured, {"status": 200, "response_bytes": len(data),
                                   "response_sha256": digest,
                                   "omitted_due_to_incomplete_security_scan": True})
        self.assertEqual(error.details["response_sha256"], digest)
        self.assertEqual(error.details["response_bytes"], len(data))
        self.assertEqual(event["payload"]["response_sha256"], digest)
        self.assertTrue(event["payload"]["omitted_due_to_incomplete_security_scan"])
        for fragment in (data.decode("ascii"), escaped_key, KEY, KEY[1:], "\\u0073", '"raw_http_text"'):
            self.assert_not_persisted(store, fragment)
        self.assertEqual(ledger.reservations, {request["request_id"]: 128})
        self.assertEqual(ledger.uncertain, {request["request_id"]: "invalid_response"})
        self.assertEqual(ledger.settled, {})
        self.assertEqual([call[0] for call in ledger.calls], ["reserve", "mark_uncertain"])
        self.assertEqual(transport.actual_model_calls, 1)
        self.opener.open.assert_called_once()
        self.assertEqual(response.read_sizes, [MAX_BYTES + 1])
        self.assertTrue(response.closed)

    def test_stage_json_with_twice_decoded_key_is_not_recordable(self):
        escaped_key = "\\u0073" + KEY[1:]
        stage_json = '{"stage":"connectivity","text":"' + escaped_key + '"}'
        value = make_response()
        value["choices"][0]["message"]["content"] = stage_json
        data = raw_json(value)
        decoded_content = json.loads(data)["choices"][0]["message"]["content"]
        self.assertEqual(decoded_content, stage_json)
        self.assertIn(escaped_key, decoded_content)
        self.assertNotIn(KEY, decoded_content)
        self.assertNotIn(KEY.encode("ascii"), data)
        self.assertEqual(json.loads(decoded_content)["text"], KEY)

        transport, store, ledger = self.make_transport()
        request = make_request()
        response = self.respond(data=data)
        error = self.expect_error(transport, request, "response_not_recordable")
        digest = hashlib.sha256(data).hexdigest()
        captured, event = self.raw_response(store)
        self.assertEqual(captured, {"status": 200, "response_bytes": len(data),
                                   "response_sha256": digest, "omitted_for_credentials": True})
        self.assertTrue(error.details["omitted_for_credentials"])
        self.assertEqual(error.details["response_sha256"], digest)
        self.assertEqual(event["payload"]["response_sha256"], digest)
        self.assertTrue(event["payload"]["omitted_for_credentials"])
        for fragment in (stage_json, escaped_key, KEY, KEY[1:], "\\u0073", '"raw_http_text"'):
            self.assert_not_persisted(store, fragment)
        self.assertEqual(ledger.reservations, {request["request_id"]: 128})
        self.assertEqual(ledger.uncertain, {request["request_id"]: "invalid_response"})
        self.assertEqual(ledger.settled, {})
        self.assertEqual([call[0] for call in ledger.calls], ["reserve", "mark_uncertain"])
        self.assertEqual(transport.actual_model_calls, 1)
        self.opener.open.assert_called_once()
        self.assertEqual(response.read_sizes, [MAX_BYTES + 1])
        self.assertTrue(response.closed)

    def test_raw_credentials_and_sensitive_envelope_keys_are_never_saved(self):
        cases = [b"invalid-json " + KEY.encode(), raw_json(dict(make_response(), echoed=KEY)),
                 raw_json(dict(make_response(), echoed=KEY)).replace(b"sk-", b"\\u0073k-")]
        for sensitive in ("api-key", "Authorization", "client_secret", "Set-Cookie", "X-API-Key", "access_token"):
            cases.append(raw_json(dict(make_response(), nested=[{sensitive: "unknown-offline-credential"}])))
        cases.append(b'{"nested":{"api_key":"unknown-offline-credential"},"nested":{}}')
        for data in cases:
            with self.subTest(脱敏="正文省略"):
                transport, store, ledger = self.make_transport()
                self.respond(data=data)
                error = self.expect_error(transport, make_request(), "response_not_recordable")
                captured, event = self.raw_response(store)
                self.assertTrue(captured["omitted_for_credentials"])
                self.assertTrue(error.details["omitted_for_credentials"])
                self.assertNotIn("raw_http_text", captured)
                self.assertEqual(captured["response_sha256"], hashlib.sha256(data).hexdigest())
                self.assertEqual(event["payload"]["response_bytes"], len(data))
                self.assert_not_persisted(store, "unknown-offline-credential")
                self.assertTrue(ledger.reservations)
                self.assertFalse(ledger.settled)
                self.assertTrue(ledger.uncertain)

    def test_http_error_sensitive_keys_omit_raw_and_do_not_leak_error_message(self):
        transport, store, ledger = self.make_transport()
        body = raw_json({"error": {"message": "合成错误", "details": {"api_key": "unknown-offline-credential"}}})
        self.opener.open.side_effect = urllib.error.HTTPError(MIMO_ENDPOINT, 401, KEY, headers(), io.BytesIO(body))
        self.expect_error(transport, make_request(), "response_not_recordable")
        self.assertNotIn("raw_http_text", self.raw_response(store)[0])
        self.assert_not_persisted(store, "unknown-offline-credential")
        self.assertTrue(ledger.uncertain)

    def test_only_safe_ascii_request_id_headers_are_retained(self):
        for value in (KEY, "unknown sk-fixture", "nonascii-中文", "line\nbreak", "a" * 201, "sk-another-offline-fixture"):
            with self.subTest(头部="省略不安全值"):
                transport, store, _ = self.make_transport()
                response = self.respond()
                response.headers = {"request-id": value, "x-request-id": "safe-1", "authorization": KEY}
                transport.complete(make_request())
                self.assertEqual(self.raw_response(store)[1]["payload"]["headers"], {"x-request-id": "safe-1"})

    def test_response_limit_single_bounded_read_on_success_and_http_error(self):
        for http_error in (False, True):
            with self.subTest(HTTP错误=http_error):
                transport, store, ledger = self.make_transport()
                data = b"x" * (MAX_BYTES + 20)
                if http_error:
                    stream = CountingStream(data)
                    self.opener.open.side_effect = urllib.error.HTTPError(MIMO_ENDPOINT, 500, KEY, headers(), stream)
                else:
                    response = self.respond(data=data)
                self.expect_error(transport, make_request(), "response_too_large")
                if http_error:
                    self.assertEqual(stream.read_sizes, [MAX_BYTES + 1])
                    self.assertTrue(stream.closed)
                else:
                    self.assertEqual(response.read_sizes, [MAX_BYTES + 1])
                event = next(row for row in self.events(store) if row["event_type"] == "mimo_response_omitted")
                self.assertTrue(event["payload"]["bounded_prefix"])
                self.assertEqual(event["payload"]["response_bytes"], MAX_BYTES + 1)
                self.assertNotIn("raw_http_text", self.blob(store, event["output_ref"]))
                self.assertTrue(ledger.reservations)
                self.assertTrue(ledger.uncertain)

    def test_exact_response_limit_is_accepted(self):
        transport, store, _ = self.make_transport()
        data = raw_json(make_response())
        data += b" " * (MAX_BYTES - len(data))
        response = self.respond(data=data)
        transport.complete(make_request())
        self.assertEqual(self.raw_response(store)[0]["raw_http_text"], data.decode())
        self.assertEqual(response.read_sizes, [MAX_BYTES + 1])

    def test_model_revision_preserved_and_wrong_model_still_settled(self):
        for model, accepted in ((MIMO_MODEL + "-20261101", True), ("other-model", False),
                                (MIMO_MODEL + "evil", False), ("tp-" + MIMO_MODEL, False)):
            with self.subTest(model=model):
                transport, store, ledger = self.make_transport()
                value = make_response()
                value["model"] = model
                self.respond(value)
                if accepted:
                    self.assertEqual(transport.complete(make_request())["model"], model)
                else:
                    self.expect_error(transport, make_request(), "model_mismatch")
                self.assertEqual(ledger.settled[make_request()["request_id"]], USAGE)
                self.assertFalse(ledger.uncertain)
                self.assertEqual(json.loads(self.raw_response(store)[0]["raw_http_text"])["model"], model)

    def test_truncation_refusal_and_tool_calls_settle_before_stopping(self):
        cases = (("length", "output_truncated"), ("repetition_truncation", "output_truncated"),
                 ("refusal", "refused"), ("content_filter", "refused"),
                 ("tool_calls", "unsupported_tool_call"), ("function_call", "unsupported_tool_call"))
        for reason, code in cases:
            with self.subTest(结束原因=reason):
                transport, store, ledger = self.make_transport()
                value = make_response()
                value["choices"][0]["finish_reason"] = reason
                value["choices"][0]["message"]["content"] = None
                response = self.respond(value)
                self.expect_error(transport, make_request(), code)
                self.assertEqual(ledger.settled[make_request()["request_id"]], USAGE)
                self.assertFalse(ledger.reservations)
                self.assertFalse(ledger.uncertain)
                self.assertEqual(self.raw_response(store)[0]["raw_http_text"], response.data.decode())
        for field, value, code in (("refusal", "合成拒绝", "refused"),
                                   ("tool_calls", [{"id": "offline-tool"}], "unsupported_tool_call"),
                                   ("function_call", {"name": "offline-tool"}, "unsupported_tool_call")):
            transport, _, ledger = self.make_transport()
            response = make_response()
            response["choices"][0]["message"][field] = value
            self.respond(response)
            self.expect_error(transport, make_request(), code)
            self.assertTrue(ledger.settled)

    def test_semantic_envelope_failures_still_settle_usage(self):
        cases = []
        for field, value in (("object", "wrong"), ("id", None), ("model", 4), ("choices", []),
                             ("choices", make_response()["choices"] * 2)):
            response = make_response()
            response[field] = value
            cases.append(response)
        for field, value in (("role", "user"), ("content", None), ("content", [])):
            response = make_response()
            response["choices"][0]["message"][field] = value
            cases.append(response)
        response = make_response()
        response["choices"][0]["finish_reason"] = "unknown"
        cases.append(response)
        for response in cases:
            transport, _, ledger = self.make_transport()
            self.respond(response)
            self.expect_error(transport, make_request(), "invalid_response")
            self.assertTrue(ledger.settled)
            self.assertFalse(ledger.uncertain)

    def test_missing_invalid_usage_and_settlement_failure_keep_reserve(self):
        cases = ((None, "missing_usage"), ({}, "invalid_usage"), ([], "invalid_usage"),
                 (dict(USAGE, prompt_tokens=True), "invalid_usage"),
                 (dict(USAGE, completion_tokens=-1), "invalid_usage"),
                 (dict(USAGE, total_tokens=25), "invalid_usage"),
                 (dict(USAGE, total_tokens=24.0), "invalid_usage"))
        for usage, code in cases:
            with self.subTest(错误=code):
                transport, store, ledger = self.make_transport()
                response = make_response()
                response["usage"] = usage
                self.respond(response)
                self.expect_error(transport, make_request(), code)
                self.assertTrue(ledger.reservations)
                self.assertFalse(ledger.settled)
                self.assertTrue(ledger.uncertain)
                self.assertEqual(json.loads(self.raw_response(store)[0]["raw_http_text"])["usage"], usage)
                before = self.opener.open.call_count
                with self.assertRaises(MiMoError) as stopped:
                    transport.complete(make_request("no-next-model-call"))
                self.assertEqual(stopped.exception.code, "unresolved_attempt")
                self.assertEqual(self.opener.open.call_count, before)
        transport, _, ledger = self.make_transport(ledger=StubLedger(settle_error=True))
        self.respond()
        with self.assertRaises(StubBudgetError) as caught:
            transport.complete(make_request())
        self.assertEqual(caught.exception.code, "invalid_usage")
        self.assertTrue(ledger.reservations)
        self.assertEqual(ledger.uncertain[make_request()["request_id"]], "invalid_usage")

    def test_http_error_with_usage_settles_but_never_returns_success(self):
        transport, store, ledger = self.make_transport()
        body = raw_json({"error": {"message": "合成错误"}, "usage": copy.deepcopy(USAGE)})
        self.opener.open.side_effect = urllib.error.HTTPError(MIMO_ENDPOINT, 500, KEY, headers(), io.BytesIO(body))
        self.expect_error(transport, make_request(), "upstream_error")
        self.assertEqual(ledger.settled[make_request()["request_id"]], USAGE)
        self.assertFalse(ledger.uncertain)
        self.assertEqual(self.raw_response(store)[0]["raw_http_text"], body.decode())

    def test_billing_anomaly_remains_budget_error_without_remarking(self):
        transport, _, ledger = self.make_transport(ledger=StubLedger(settle_error="billing_anomaly"))
        self.respond()
        with self.assertRaises(StubBudgetError) as caught:
            transport.complete(make_request())
        self.assertEqual(caught.exception.code, "billing_anomaly")
        self.assertEqual([call[0] for call in ledger.calls], ["reserve", "settle"])
        self.assertTrue(ledger.reservations)
        self.expect_error(transport, make_request("later-request"), "unresolved_attempt")
        self.opener.open.assert_called_once()

    def test_cumulative_attempt_counter_and_duplicate_reservation(self):
        transport, _, ledger = self.make_transport()
        for request_id in ("first-request", "second-request"):
            self.respond()
            transport.complete(make_request(request_id))
        self.assertEqual(transport.actual_model_calls, 2)
        self.assertEqual(self.opener.open.call_count, 2)
        self.assertEqual(len(ledger.settled), 2)
        with self.assertRaises(StubBudgetError) as caught:
            transport.complete(make_request("second-request"))
        self.assertEqual(caught.exception.code, "duplicate_request")
        self.assertEqual(transport.actual_model_calls, 2)
        self.assertEqual(self.opener.open.call_count, 2)

    def test_temperature_upper_boundary_and_config_maximum(self):
        transport, _, _ = self.make_transport(dict(CONFIG, temperature=1.5))
        request = make_request()
        request["model_settings"].update(temperature=1.5, max_tokens=4096)
        self.respond()
        transport.complete(request)
        body = json.loads(self.opener.open.call_args.args[0].data)
        self.assertEqual(body["temperature"], 1.5)
        self.assertEqual(body["max_completion_tokens"], 4096)

    def test_message_content_json_is_left_to_assistant_not_repaired(self):
        transport, store, ledger = self.make_transport()
        response = make_response()
        response["choices"][0]["message"]["content"] = "```json\n{unfinished\n```"
        self.respond(response)
        result = transport.complete(make_request())
        self.assertEqual(result["raw_text"], response["choices"][0]["message"]["content"])
        self.assertTrue(ledger.settled)
        self.assertIn("```json", self.raw_response(store)[0]["raw_http_text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
