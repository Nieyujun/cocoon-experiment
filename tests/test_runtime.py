"""运行时审计测试；项目 tests 下的证据目录全部保留，不执行清理或删除。"""

import copy
import hashlib
import json
import math
import os
import random
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from cocoon.runtime import (
    EventStore,
    branch_state,
    canonical_hash,
    load_snapshot,
    make_intervention_pair,
    measurement_view,
    save_snapshot,
    utc_now,
    verify_events,
    write_json_new,
)


PROJECT = Path(__file__).resolve().parents[1]


def json_bytes(obj):
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")


class LocalFilesTestCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(dir=PROJECT / "tests", prefix="runtime-"))

    def tree_bytes(self):
        return {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("*") if path.is_file()
        }


class CanonicalTests(unittest.TestCase):
    def test_exact_json_hash(self):
        obj = {"乙": "汉字", "a": [1, True, None], "b": {"z": 2, "a": 1}}
        self.assertEqual(canonical_hash(obj), hashlib.sha256(json_bytes(obj)).hexdigest())
        self.assertEqual(canonical_hash(obj), canonical_hash(dict(reversed(list(obj.items())))))
        self.assertEqual(len(canonical_hash(obj)), 64)
        self.assertIn("汉字".encode("utf-8"), json_bytes(obj))
        self.assertNotIn(b"\\u", json_bytes(obj))

    def test_nonfinite_and_unsupported_values_rejected(self):
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value), self.assertRaises(ValueError):
                canonical_hash({"number": value})
        for value in ({1, 2}, random.Random(1), object()):
            with self.subTest(kind=type(value)), self.assertRaises(TypeError):
                canonical_hash({"rng": value})

    def test_utc_time_is_iso_with_z(self):
        value = utc_now()
        self.assertTrue(value.endswith("Z"))
        self.assertIn("T", value)
        self.assertEqual(datetime.fromisoformat(value[:-1] + "+00:00").utcoffset(), timedelta(0))


class JsonAndSnapshotTests(LocalFilesTestCase):
    def test_new_json_is_canonical_and_fsynced(self):
        path = self.root / "new.json"
        obj = {"乙": "中文", "a": 2}
        with mock.patch("cocoon.runtime.os.fsync", wraps=os.fsync) as fsync:
            write_json_new(path, obj)
        fsync.assert_called_once()
        self.assertEqual(path.read_bytes(), json_bytes(obj))

    def test_json_never_overwrites_even_identical_content(self):
        path = self.root / "new.json"
        write_json_new(path, {"value": 1})
        original = path.read_bytes()
        for value in (1, 2):
            with self.assertRaises(FileExistsError):
                write_json_new(path, {"value": value})
            self.assertEqual(path.read_bytes(), original)

    def test_parent_must_exist_and_is_not_created(self):
        path = self.root / "missing" / "new.json"
        with self.assertRaises(FileNotFoundError):
            write_json_new(path, {})
        self.assertFalse(path.parent.exists())

    def test_serialization_failure_creates_no_file(self):
        path = self.root / "new.json"
        for obj in ({"number": math.nan}, {"rng": random.Random(1)}):
            with self.assertRaises((ValueError, TypeError)):
                write_json_new(path, obj)
            self.assertFalse(path.exists())

    def test_sensitive_keys_rejected_not_redacted(self):
        path = self.root / "new.json"
        for key in ("api_key", "authorization", "access_token", "password", "secret", "API_KEY"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                write_json_new(path, {"outer": [{"nested": {key: "内容"}}]})
            self.assertFalse(path.exists())
        write_json_new(path, {"secretary": "secret", "text": "authorization api_key password"})
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["secretary"], "secret")

    def test_snapshot_envelope_and_hash(self):
        path = self.root / "snapshot.json"
        state = {"world_id": "w0", "agents": [{"items": [1, 2]}], "rng": [3, [1, 2], None]}
        result = save_snapshot(path, state)
        self.assertEqual(result, {"ref": str(path.resolve()), "sha256": canonical_hash(state)})
        self.assertTrue(Path(result["ref"]).is_absolute())
        self.assertEqual(
            json.loads(path.read_text(encoding="utf-8")),
            {"state": state, "state_sha256": canonical_hash(state)},
        )
        self.assertEqual(load_snapshot(path), state)
        loaded = load_snapshot(path)
        loaded["agents"][0]["items"].append(3)
        state["agents"][0]["items"].append(4)
        self.assertEqual(load_snapshot(path)["agents"][0]["items"], [1, 2])

    def test_snapshot_never_overwrites(self):
        path = self.root / "snapshot.json"
        save_snapshot(path, {"value": 1})
        original = path.read_bytes()
        with self.assertRaises(FileExistsError):
            save_snapshot(path, {"value": 2})
        self.assertEqual(path.read_bytes(), original)

    def test_snapshot_rejects_state_and_hash_tampering(self):
        path = self.root / "snapshot.json"
        state = {"value": [1]}
        for envelope in (
            {"state": {"value": [2]}, "state_sha256": canonical_hash(state)},
            {"state": state, "state_sha256": "0" * 64},
            {"state": [], "state_sha256": canonical_hash([])},
            {"state": state},
            {"state": state, "state_sha256": canonical_hash(state), "extra": True},
        ):
            with self.subTest(envelope=envelope):
                path.write_bytes(json_bytes(envelope))
                with self.assertRaises(ValueError):
                    load_snapshot(path)

    def test_snapshot_rejects_truncated_duplicate_and_nonfinite_json(self):
        path = self.root / "snapshot.json"
        for raw in (b'{"state":', b'{"state":{},"state":{}}', b'{"state":{"x":NaN}}'):
            with self.subTest(raw=raw):
                path.write_bytes(raw)
                with self.assertRaises(ValueError):
                    load_snapshot(path)

    def test_snapshot_rejects_secret_and_rng_object_without_writing(self):
        path = self.root / "snapshot.json"
        for state, error in (({"nested": {"password": "x"}}, ValueError), ({"rng": random.Random(1)}, TypeError)):
            with self.subTest(state=state), self.assertRaises(error):
                save_snapshot(path, state)
            self.assertFalse(path.exists())
        rng_state = random.Random(1).getstate()
        serializable = {"rng": [rng_state[0], list(rng_state[1]), rng_state[2]]}
        save_snapshot(path, serializable)
        self.assertEqual(load_snapshot(path), serializable)

    def test_snapshot_read_rejects_sensitive_keys_even_with_valid_hash(self):
        path = self.root / "snapshot.json"
        state = {"nested": [{"secret": "x"}]}
        path.write_bytes(json_bytes({"state": state, "state_sha256": canonical_hash(state)}))
        with self.assertRaises(ValueError):
            load_snapshot(path)


class StateIsolationTests(LocalFilesTestCase):
    def test_branch_changes_only_world_and_parent(self):
        state = {
            "world_id": "original", "parent_world_id": "older", "step": 4,
            "agents": [{"inbox": ["first"]}], "rng": [3, [1, 2, 3], None],
        }
        before = copy.deepcopy(state)
        first = branch_state(state, "branch-a")
        second = branch_state(state, "branch-b")
        expected = copy.deepcopy(state)
        expected.update(world_id="branch-a", parent_world_id="original")
        self.assertEqual(first, expected)
        self.assertEqual(state, before)
        first["agents"][0]["inbox"].append("new")
        first["rng"][1][0] = 99
        self.assertEqual(state, before)
        self.assertEqual(second["agents"], before["agents"])
        self.assertEqual(second["rng"], before["rng"])
        self.assertFalse(any(self.root.iterdir()))

    def test_measurement_cannot_mutate_state_or_write_files(self):
        path = self.root / "snapshot.json"
        state = {"world_id": "w", "nested": [{"values": [1]}]}
        save_snapshot(path, state)
        before = self.tree_bytes()
        with mock.patch.object(Path, "open", side_effect=AssertionError("测量禁止文件访问")):
            view = measurement_view(state)
            self.assertEqual(view, state)
            view["nested"][0]["values"].append(2)
            view["world_id"] = "changed"
        self.assertEqual(state, {"world_id": "w", "nested": [{"values": [1]}]})
        self.assertEqual(self.tree_bytes(), before)


class EventStoreTests(LocalFilesTestCase):
    def setUp(self):
        super().setUp()
        self.config = {"seed": 42, "language": "中文"}
        self.manifest = canonical_hash({"materials": []})
        self.store = self.reopen()
        self.log = self.root / "events/events.jsonl"

    def reopen(self, **changes):
        options = {
            "root": self.root, "config": self.config, "manifest_hash": self.manifest,
            "is_demo": True, "experiment_id": "test-experiment",
        }
        options.update(changes)
        return EventStore(**options)

    def events(self):
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def write_events(self, events, rechain=False):
        previous = None
        for event in events:
            if rechain:
                event["previous_event_hash"] = previous
                event["event_hash"] = canonical_hash({key: value for key, value in event.items() if key != "event_hash"})
            previous = event["event_hash"]
        self.log.write_bytes(b"".join(json_bytes(event) + b"\n" for event in events))

    def assert_invalid_store(self):
        before = self.tree_bytes()
        with self.assertRaises((ValueError, OSError)):
            verify_events(self.root)
        with self.assertRaises((ValueError, OSError)):
            self.reopen()
        self.assertEqual(self.tree_bytes(), before)

    def test_empty_store_and_existing_root_requirement(self):
        self.assertEqual(verify_events(self.root), {"event_count": 0, "blob_count": 0, "chain_valid": True})
        missing = self.root / "must-not-create"
        with self.assertRaises(FileNotFoundError):
            self.reopen(root=missing)
        self.assertFalse(missing.exists())
        with self.assertRaises(NotADirectoryError):
            self.reopen(root=self.log)

    def test_all_fields_preserved_and_protocol_hashes(self):
        original = self.store.capture({"input": "first"})
        before = self.store.capture({"step": 0})
        after = self.store.capture({"step": 1})
        fields = {
            "payload": {"result": "成功"}, "world_id": "w1", "parent_world_id": "w0",
            "topic_id": "t", "block_id": "b", "seed": 42, "arm": "I4", "step": 1,
            "phase": "delivery", "actor_id": "a", "actor_role": "reader", "target_ids": ["b"],
            "status": "ok", "task_id": "task", "attempt_id": "try", "parent_event_ids": [],
            "input_ref": original["ref"], "input_sha256": original["sha256"],
            "state_before_ref": before, "state_after_ref": after["ref"],
            "model": None, "usage": {"input_tokens": 0}, "latency_ms": 0, "error": None,
            "extra_field": {"retained": True},
        }
        event = self.store.append("delivery", **fields)
        for key, value in fields.items():
            self.assertEqual(event[key], value, key)
        self.assertEqual(event["schema_version"], "1.0")
        self.assertEqual(event["experiment_id"], "test-experiment")
        self.assertEqual(event["logical_seq"], 1)
        self.assertEqual(event["config_hash"], canonical_hash(self.config))
        self.assertEqual(event["material_manifest_hash"], self.manifest)
        self.assertIs(event["is_demo"], True)
        self.assertIsNone(event["previous_event_hash"])
        payload_hash = canonical_hash(fields["payload"])
        self.assertEqual(event["output_ref"], f"raw/blobs/{payload_hash}.json")
        self.assertEqual(event["output_sha256"], payload_hash)
        self.assertEqual(event["event_hash"], canonical_hash({key: value for key, value in event.items() if key != "event_hash"}))
        self.assertEqual(verify_events(self.root), {"event_count": 1, "blob_count": 4, "chain_valid": True})

    def test_custom_output_preserved_and_mock_missing_payload_blob_rejected(self):
        output = self.store.capture({"other_output": True})
        event = self.store.append("custom", payload={"payload": "different"}, output_ref=output["ref"], output_sha256=output["sha256"])
        self.assertEqual(event["output_ref"], output["ref"])
        self.assertEqual(event["output_sha256"], output["sha256"])
        self.assertEqual(verify_events(self.root)["blob_count"], 2)
        payload_path = self.root / f"raw/blobs/{canonical_hash(event['payload'])}.json"
        before = self.tree_bytes()
        original_exists = Path.exists
        with mock.patch.object(
            Path, "exists", new=lambda path: path != payload_path and original_exists(path),
        ):
            self.assertFalse(payload_path.exists())
            self.assert_invalid_store()
        self.assertEqual(self.tree_bytes(), before)
        self.assertEqual(verify_events(self.root)["blob_count"], 2)

    def test_null_fields_and_unknown_fields_not_dropped(self):
        event = self.store.append("custom", payload=None, output_ref=None, output_sha256=None, custom={"text": "secret is ordinary text"})
        self.assertIsNone(event["payload"])
        self.assertIsNone(event["output_ref"])
        self.assertIsNone(event["output_sha256"])
        self.assertEqual(event["custom"], {"text": "secret is ordinary text"})
        self.assertEqual(verify_events(self.root)["blob_count"], 1)

    def test_no_automatic_or_fake_model_events(self):
        event = self.store.append("measurement", payload={"value": 1})
        self.assertNotIn("model", event)
        self.assertEqual([item["event_type"] for item in self.events()], ["measurement"])

    def test_append_fsync_and_append_only_prefix(self):
        first = self.store.append("start")
        prefix = self.log.read_bytes()
        with mock.patch("cocoon.runtime.os.fsync", wraps=os.fsync) as fsync:
            second = self.store.append("finish", parent_event_ids=[first["event_id"]])
        fsync.assert_called_once()
        self.assertTrue(self.log.read_bytes().startswith(prefix))
        self.assertEqual(second["previous_event_hash"], first["event_hash"])
        self.assertNotEqual(first["event_id"], second["event_id"])
        self.assertEqual(verify_events(self.root)["event_count"], 2)

    def test_reload_continues_sequence_and_chain(self):
        first = self.store.append("first", payload={"n": 1})
        prefix = self.log.read_bytes()
        second = self.reopen().append("second", parent_event_ids=[first["event_id"]])
        self.assertEqual(second["logical_seq"], 2)
        self.assertEqual(second["previous_event_hash"], first["event_hash"])
        self.assertTrue(self.log.read_bytes().startswith(prefix))
        self.assertEqual(verify_events(self.root)["event_count"], 2)

    def test_reload_rejects_different_experiment_or_metadata(self):
        self.store.append("first")
        before = self.tree_bytes()
        for changes in (
            {"experiment_id": "other"}, {"config": {"seed": 99}},
            {"manifest_hash": "0" * 64}, {"is_demo": False},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.reopen(**changes)
            self.assertEqual(self.tree_bytes(), before)

    def test_inputs_and_returned_events_are_isolated(self):
        payload = {"items": [1]}
        first = self.store.append("first", payload=payload)
        original_hash = first["event_hash"]
        payload["items"].append(2)
        first["payload"]["items"].append(3)
        first["event_hash"] = "changed"
        self.config["seed"] = -1
        second = self.store.append("second")
        self.assertEqual(second["previous_event_hash"], original_hash)
        self.assertEqual(second["config_hash"], canonical_hash({"seed": 42, "language": "中文"}))
        self.assertEqual(self.events()[0]["payload"], {"items": [1]})
        self.assertEqual(verify_events(self.root)["event_count"], 2)

    def test_capture_is_content_addressed_and_immutable(self):
        obj = {"b": 2, "a": "汉字"}
        ref = self.store.capture(obj)
        path = self.root / ref["ref"]
        before = path.stat().st_mtime_ns
        self.assertEqual(path.read_bytes(), json_bytes(obj))
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), ref["sha256"])
        self.assertEqual(self.store.capture({"a": "汉字", "b": 2}), ref)
        self.assertEqual(path.stat().st_mtime_ns, before)
        obj["b"] = 3
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["b"], 2)
        self.assertEqual(verify_events(self.root)["blob_count"], 1)

    def test_capture_rejects_corrupt_existing_blob_without_overwrite(self):
        obj = {"a": 1}
        ref = self.store.capture(obj)
        path = self.root / ref["ref"]
        for corrupt in (b'{"a":2}', b'{ "a": 1 }', b'{"a":', b'{"a":1,"a":1}'):
            with self.subTest(corrupt=corrupt):
                path.write_bytes(corrupt)
                with self.assertRaises(ValueError):
                    self.store.capture(obj)
                self.assertEqual(path.read_bytes(), corrupt)
                self.assert_invalid_store()

    def test_sensitive_keys_reject_entire_append_and_capture(self):
        before = self.tree_bytes()
        for key in ("api_key", "authorization", "access_token", "password", "secret", "AUTHORIZATION"):
            for fields in ({key: "x"}, {"payload": {"outer": [{key: "x"}]}}, {"usage": {key: "x"}}):
                with self.subTest(key=key, fields=fields), self.assertRaises(ValueError):
                    self.store.append("rejected", **fields)
                self.assertEqual(self.tree_bytes(), before)
            with self.assertRaises(ValueError):
                self.store.capture({"data": ({key: "x"},)})
            self.assertEqual(self.tree_bytes(), before)
        event = self.store.append("allowed", payload={"text": "secret api_key password", "secretary": "普通值"})
        self.assertEqual(event["payload"]["text"], "secret api_key password")

    def test_sensitive_config_creates_no_subdirectories(self):
        root = self.root / "config-test"
        root.mkdir()
        with self.assertRaises(ValueError):
            self.reopen(root=root, config={"nested": {"access_token": "x"}})
        self.assertEqual(list(root.iterdir()), [])

    def test_reserved_metadata_collision_is_not_silently_dropped(self):
        before = self.tree_bytes()
        for key in ("event_id", "logical_seq", "schema_version", "event_hash", "experiment_id", "config_hash"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.store.append("test", **{key: "caller-value", "payload": {"a": 1}})
            self.assertEqual(self.tree_bytes(), before)

    def test_invalid_serialization_writes_nothing(self):
        before = self.tree_bytes()
        for payload in ({"x": math.nan}, {"x": {1, 2}}, {"rng": random.Random(1)}):
            with self.subTest(payload=payload), self.assertRaises((TypeError, ValueError)):
                self.store.append("invalid", payload=payload)
            self.assertEqual(self.tree_bytes(), before)

    def test_log_tampering_and_wrong_hash_rejected(self):
        self.store.append("first", payload={"n": 1})
        original = self.events()
        for field, value in (("status", "tampered"), ("event_hash", "0" * 64), ("event_hash", "short")):
            events = copy.deepcopy(original)
            events[0][field] = value
            self.write_events(events)
            self.assert_invalid_store()

    def test_blank_malformed_and_truncated_lines_never_ignored(self):
        self.store.append("first")
        valid = self.log.read_bytes()
        for raw in (valid[:-1], valid + b'\n', valid + b'{"unfinished":', b'\n' + valid, valid + b'not-json\n', valid + b'\xff\n'):
            with self.subTest(raw=raw):
                self.log.write_bytes(raw)
                self.assert_invalid_store()

    def test_duplicate_json_keys_and_nan_in_log_rejected(self):
        self.store.append("first")
        valid = self.log.read_bytes()
        for raw in (b'{"event_type":"duplicate",' + valid[1:], b'{"number":NaN,' + valid[1:]):
            self.log.write_bytes(raw)
            self.assert_invalid_store()

    def test_sequence_gap_duplicate_ids_and_metadata_mix_rejected_after_rehash(self):
        self.store.append("first")
        self.store.append("second")
        original = self.events()
        changes = (
            (0, "logical_seq", 0), (1, "logical_seq", 3), (0, "logical_seq", True),
            (1, "event_id", original[0]["event_id"]), (1, "experiment_id", "other"),
            (1, "schema_version", "2.0"), (1, "wall_time_utc", "not-a-timeZ"),
        )
        for index, key, value in changes:
            with self.subTest(index=index, key=key, value=value):
                events = copy.deepcopy(original)
                events[index][key] = value
                self.write_events(events, rechain=True)
                self.assert_invalid_store()

    def test_previous_hash_chain_is_checked_independently(self):
        self.store.append("first")
        self.store.append("second")
        events = self.events()
        events[1]["previous_event_hash"] = "0" * 64
        events[1]["event_hash"] = canonical_hash({key: value for key, value in events[1].items() if key != "event_hash"})
        self.write_events(events)
        self.assert_invalid_store()

    def test_parent_ids_must_already_exist_before_append(self):
        first = self.store.append("first")
        before = self.tree_bytes()
        for parents in (["future"], [1], "not-a-list"):
            with self.subTest(parents=parents), self.assertRaises(ValueError):
                self.store.append("invalid", parent_event_ids=parents, payload={"not_written": True})
            self.assertEqual(self.tree_bytes(), before)
        self.store.append("valid", parent_event_ids=[first["event_id"]])
        self.assertEqual(verify_events(self.root)["event_count"], 2)

    def test_forward_self_and_missing_parent_rejected_by_verifier(self):
        self.store.append("first")
        self.store.append("second")
        original = self.events()
        for parent in (original[1]["event_id"], original[0]["event_id"], "absent"):
            events = copy.deepcopy(original)
            events[0]["parent_event_ids"] = [parent]
            self.write_events(events, rechain=True)
            self.assert_invalid_store()

    def test_every_supported_reference_rejects_mock_missing_blob(self):
        original_exists = Path.exists
        for field in ("input_ref", "output_ref", "state_before_ref", "state_after_ref"):
            with self.subTest(field=field):
                ref = self.store.capture({"field": field})
                self.store.append("referenced", **{field: ref})
                missing_path = self.root / ref["ref"]
                before = self.tree_bytes()
                with mock.patch.object(
                    Path, "exists", new=lambda path: path != missing_path and original_exists(path),
                ):
                    self.assertFalse(missing_path.exists())
                    self.assert_invalid_store()
                self.assertEqual(self.tree_bytes(), before)
        self.assertEqual(verify_events(self.root)["event_count"], 4)

    def test_reference_path_escape_and_invalid_hash_write_nothing(self):
        before = self.tree_bytes()
        sha = "0" * 64
        invalid = (
            "../outside.json", f"raw/blobs/../{sha}.json", f"/raw/blobs/{sha}.json",
            f"C:/raw/blobs/{sha}.json", f"raw\\blobs\\{sha}.json",
            f"raw/blobs//{sha}.json", f"raw/./blobs/{sha}.json",
            "raw/blobs/not-a-sha.json", f"raw/blobs/{sha}.json",
        )
        for ref in invalid:
            with self.subTest(ref=ref), self.assertRaises(ValueError):
                self.store.append("invalid", input_ref=ref, payload={"not_written": True})
            self.assertEqual(self.tree_bytes(), before)

    def test_verifier_rejects_rehashed_escaping_and_wrong_sha_references(self):
        ref = self.store.capture({"value": 1})
        self.store.append("first", input_ref=ref["ref"], input_sha256=ref["sha256"])
        original = self.events()
        for changes in ({"input_ref": "../escape.json"}, {"input_sha256": "0" * 64}, {"input_ref": {"ref": ref["ref"], "sha256": "0" * 64}}):
            events = copy.deepcopy(original)
            events[0].update(changes)
            self.write_events(events, rechain=True)
            self.assert_invalid_store()

    def test_nested_reference_objects_are_validated(self):
        ref = self.store.capture({"value": 1})
        self.store.append("nested", payload={"artifacts": [ref]})
        self.assertEqual(verify_events(self.root)["blob_count"], 2)
        before = self.tree_bytes()
        with self.assertRaises(ValueError):
            self.store.append("invalid", custom={"nested": {"ref": "../escape", "sha256": "0" * 64}})
        self.assertEqual(self.tree_bytes(), before)

    def test_blob_tampering_and_unreferenced_blobs_are_checked(self):
        ref = self.store.capture({"unreferenced": True})
        path = self.root / ref["ref"]
        path.write_bytes(b'{"unreferenced":false}')
        self.assert_invalid_store()

    def test_unexpected_blob_filename_is_rejected(self):
        path = self.root / "raw/blobs/not-a-hash.json"
        path.write_bytes(b"{}")
        self.assert_invalid_store()

    def test_existing_log_secret_is_rejected_even_after_rehash(self):
        self.store.append("first")
        events = self.events()
        events[0]["usage"] = {"authorization": "x"}
        self.write_events(events, rechain=True)
        self.assert_invalid_store()

    def test_external_log_growth_is_rejected_without_writes(self):
        self.store.append("first")
        valid = self.log.read_bytes()
        self.log.write_bytes(valid + b"broken")
        before = self.tree_bytes()
        with self.assertRaises(ValueError):
            self.store.append("second", payload={"not_written": True})
        self.assertEqual(self.tree_bytes(), before)

    def test_mock_missing_log_is_rejected_without_writes(self):
        self.store.append("first")
        before = self.tree_bytes()
        original_is_file = Path.is_file
        with mock.patch.object(
            Path, "is_file", new=lambda path: path != self.log and original_is_file(path),
        ):
            self.assertFalse(self.log.is_file())
            with self.assertRaisesRegex(ValueError, "事件日志缺失"):
                verify_events(self.root)
            with self.assertRaisesRegex(ValueError, "事件日志缺失"):
                self.store.append("second", payload={"not_written": True})
        self.assertTrue(self.log.is_file())
        self.assertEqual(self.tree_bytes(), before)
        self.assertEqual(verify_events(self.root)["event_count"], 1)


class InterventionTests(unittest.TestCase):
    def setUp(self):
        self.packet = {
            "packet_id": "packet-1",
            "evidence": [
                {"evidence_id": "e2", "text": "  保留原文与空白。\n第二行。  ", "source_url": "urn:fixture:source:2", "source_family_id": "f2"},
                {"evidence_id": "e1", "text": "另一条证据，不删改。", "source_url": "urn:fixture:source:1", "source_family_id": "f1"},
            ],
        }

    def text_for_length(self, count, char="甲"):
        base = make_intervention_pair(self.packet, "甲", "乙", min_chars=0, max_chars=10000)["I4"]["char_count"] - 1
        return char * (count - base)

    def test_equal_evidence_and_visible_text_are_deterministic(self):
        original = copy.deepcopy(self.packet)
        i4 = self.text_for_length(700)
        i5 = self.text_for_length(700, "乙")
        pair = make_intervention_pair(self.packet, i4, i5)
        self.assertEqual(pair, make_intervention_pair(self.packet, i4, i5))
        self.assertEqual(self.packet, original)
        for arm, additional in (("I4", i4), ("I5", i5)):
            delivery = pair[arm]
            self.assertEqual(delivery["evidence_ids"], ["e2", "e1"])
            self.assertEqual(delivery["additional_text"], additional)
            self.assertEqual(delivery["packet_hash"], canonical_hash(self.packet))
            self.assertEqual(delivery["evidence_hash"], canonical_hash(delivery["evidence_block"]))
            self.assertEqual(delivery["visible_text"], delivery["evidence_block"] + "\n\n" + additional)
            self.assertEqual(delivery["char_count"], len(delivery["visible_text"]))
            self.assertEqual(delivery["char_count"], 700)
            for item in original["evidence"]:
                self.assertIn(item["text"], delivery["evidence_block"])
                self.assertIn(item["source_url"], delivery["evidence_block"])
        self.assertEqual(pair["I4"]["evidence_block"], pair["I5"]["evidence_block"])
        self.assertTrue(pair["checks"]["evidence_equal"])
        self.assertTrue(pair["checks"]["dose_within_range"])
        self.assertTrue(pair["checks"]["length_matched"])
        self.assertEqual(pair["checks"]["deviations"], [])

    def test_delivery_lists_and_input_packet_are_isolated(self):
        pair = make_intervention_pair(self.packet, "组织段甲", "组织段乙")
        pair["I4"]["evidence_ids"].append("changed")
        self.packet["evidence"][0]["text"] = "changed"
        self.assertEqual(pair["I5"]["evidence_ids"], ["e2", "e1"])
        self.assertIn("保留原文", pair["I4"]["evidence_block"])

    def test_length_range_and_ten_percent_boundary(self):
        for count in (600, 800):
            text = self.text_for_length(count)
            pair = make_intervention_pair(self.packet, text, text)
            self.assertTrue(pair["checks"]["dose_within_range"])
        left = self.text_for_length(700)
        at_boundary = make_intervention_pair(self.packet, left, self.text_for_length(770))
        over_boundary = make_intervention_pair(self.packet, left, self.text_for_length(771))
        self.assertTrue(at_boundary["checks"]["length_matched"])
        self.assertEqual(at_boundary["checks"]["length_difference_ratio"], .1)
        self.assertFalse(over_boundary["checks"]["length_matched"])
        self.assertTrue(over_boundary["checks"]["dose_within_range"])
        self.assertIn("length_mismatch", [item["code"] for item in over_boundary["checks"]["deviations"]])
        custom = make_intervention_pair(self.packet, left, self.text_for_length(771), tolerance=.2)
        self.assertTrue(custom["checks"]["length_matched"])

    def test_mismatch_is_audited_without_truncation_or_dropped_evidence(self):
        short = "短组织段"
        long = "完整保留" * 600
        pair = make_intervention_pair(self.packet, short, long)
        self.assertFalse(pair["checks"]["dose_within_range"])
        self.assertFalse(pair["checks"]["length_matched"])
        self.assertTrue(pair["checks"]["evidence_equal"])
        codes = [item["code"] for item in pair["checks"]["deviations"]]
        self.assertEqual(codes.count("dose_out_of_range"), 2)
        self.assertIn("length_mismatch", codes)
        self.assertEqual(pair["I4"]["additional_text"], short)
        self.assertEqual(pair["I5"]["additional_text"], long)
        self.assertTrue(pair["I5"]["visible_text"].endswith(long))
        self.assertEqual(pair["I4"]["evidence_block"], pair["I5"]["evidence_block"])

    def test_oversized_evidence_is_not_cut_or_removed(self):
        self.packet["evidence"][0]["text"] = "证据原文" * 600
        pair = make_intervention_pair(self.packet, "甲组织段", "乙组织段")
        self.assertFalse(pair["checks"]["dose_within_range"])
        for arm in ("I4", "I5"):
            self.assertIn(self.packet["evidence"][0]["text"], pair[arm]["evidence_block"])
            self.assertEqual(pair[arm]["evidence_ids"], ["e2", "e1"])

    def test_semantics_always_pending_not_claimed_as_formal_i4(self):
        pair = make_intervention_pair(self.packet, "组织段声称新增且尚未核验的事实。", "另一个组织段。")
        self.assertEqual(pair["checks"]["semantic_review"], "pending")
        notice = pair["checks"]["audit_notice"]
        self.assertIn("不验证", notice)
        self.assertIn("新增事实", notice)
        self.assertIn("人工/独立引用审计", notice)
        self.assertIn("不是正式 I4 原产品集成", notice)

    def test_duplicate_ids_are_rejected(self):
        self.packet["evidence"][1]["evidence_id"] = "e2"
        with self.assertRaises(ValueError):
            make_intervention_pair(self.packet, "甲", "乙")

    def test_all_empty_evidence_fields_and_missing_fields_rejected(self):
        for field in ("evidence_id", "text", "source_url", "source_family_id"):
            for empty in ("", " \n\t", None):
                packet = copy.deepcopy(self.packet)
                packet["evidence"][0][field] = empty
                with self.subTest(field=field, empty=empty), self.assertRaises(ValueError):
                    make_intervention_pair(packet, "甲", "乙")
            packet = copy.deepcopy(self.packet)
            del packet["evidence"][0][field]
            with self.assertRaises(ValueError):
                make_intervention_pair(packet, "甲", "乙")
        for changes in ({"packet_id": " "}, {"evidence": []}, {"evidence": {}}, {"evidence": [None]}):
            packet = copy.deepcopy(self.packet)
            packet.update(changes)
            with self.assertRaises(ValueError):
                make_intervention_pair(packet, "甲", "乙")
        for texts in (("", "乙"), ("甲", " ")):
            with self.assertRaises(ValueError):
                make_intervention_pair(self.packet, *texts)

    def test_evidence_and_packet_whitelists_reject_annotations(self):
        for field in ("label", "analysis", "justification", "gold", "arbitrary_extra", "LABEL"):
            for level in ("packet", "evidence"):
                packet = copy.deepcopy(self.packet)
                target = packet if level == "packet" else packet["evidence"][0]
                target[field] = "不得进入交付的附加内容"
                with self.subTest(field=field, level=level), self.assertRaises(ValueError):
                    make_intervention_pair(packet, "甲", "乙")
        packet = copy.deepcopy(self.packet)
        packet["evidence"][0]["text"] = "label、analysis 和 secret 是普通文本，不是字段键。"
        self.assertTrue(make_intervention_pair(packet, "甲", "乙")["checks"]["evidence_equal"])

    def test_invalid_length_options_rejected(self):
        for options in (
            {"min_chars": -1}, {"min_chars": 801, "max_chars": 800},
            {"min_chars": True}, {"max_chars": 1.5}, {"tolerance": math.nan},
            {"tolerance": math.inf}, {"tolerance": -0.1}, {"tolerance": 1.1}, {"tolerance": True},
        ):
            with self.subTest(options=options), self.assertRaises((TypeError, ValueError)):
                make_intervention_pair(self.packet, "甲", "乙", **options)


if __name__ == "__main__":
    unittest.main()
