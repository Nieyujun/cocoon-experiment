"""提示词静态冻结测试；tests 下的临时证据全部保留，不清理或删除。"""

import copy
import hashlib
import io
import json
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest import mock

from cocoon.prompts import (
    STAGES,
    extract_prompts,
    freeze_prompts,
    load_prompt_bundle,
    verify_original_sources,
)
from cocoon.runtime import canonical_hash, write_json_new


ROOT = Path(__file__).resolve().parents[1]
ORIGINAL = ROOT / "vendor/cocoon-breaker-original"
ENTRY = ORIGINAL / "source_cdd042dba34f/index.html"
ZIP = ORIGINAL / "cocoon-breaker-demo.zip"
ENTRY_SHA = "d6c168938f79326bccad5ee21f72b9527cd6fb5f1e3dd7d424d4c32088d6f701"
ZIP_SHA = "cdd042dba34f8f1e9e01e9e0c7a43540825e7f0de477029952c4aa798f0ce6f8"
BUNDLE_FIELDS = {
    "schema_version", "version", "source_entry_sha256", "source_zip_sha256",
    "prompt_source", "prompts", "bundle_sha256",
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def inline_source(pairs=None):
    if pairs is None:
        pairs = [(stage, f"原始 {stage}") for stage in STAGES]
    return "const PROMPT_TEMPLATES = {\n" + ",\n".join(
        f"  {key}: `{value}`" for key, value in pairs
    ) + "\n};"


def seal(bundle):
    bundle["bundle_sha256"] = canonical_hash({
        key: value for key, value in bundle.items() if key != "bundle_sha256"
    })
    return bundle


def sample_bundle():
    prompts = {}
    for index, stage in enumerate(STAGES):
        text = f"原始 {stage}\n第二行"
        prompts[stage] = {
            "text": text, "sha256": sha(text.encode("utf-8")),
            "line_start": index * 3 + 1, "line_end": index * 3 + 2,
        }
    return seal({
        "schema_version": "1.0", "version": "cocoon-original-inline-v1",
        "source_entry_sha256": "a" * 64, "source_zip_sha256": "b" * 64,
        "prompt_source": "index.html:PROMPT_TEMPLATES", "prompts": prompts,
    })


class LocalFilesTests(unittest.TestCase):
    def setUp(self):
        if not (ROOT / "tests").is_dir():
            raise FileNotFoundError("测试父目录必须已存在")
        self.root = Path(tempfile.mkdtemp(dir=ROOT / "tests", prefix="prompts-"))
        self.file_number = 0

    def save_bytes(self, data, suffix=".json"):
        self.file_number += 1
        path = self.root / f"fixture-{self.file_number}{suffix}"
        with path.open("xb") as stream:
            stream.write(data)
        return path

    def save_bundle(self, bundle):
        return self.save_bytes(json.dumps(bundle, ensure_ascii=False).encode("utf-8"))

    def make_sources(self, entry=None, members=None):
        if entry is None:
            entry = inline_source().encode("utf-8")
        if members is None:
            members = [("index.html", entry)]
        buffer = io.BytesIO()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
                for name, data in members:
                    archive.writestr(name, data)
        archive_bytes = buffer.getvalue()
        return (
            self.save_bytes(entry, ".html"), self.save_bytes(archive_bytes, ".zip"),
            sha(entry), sha(archive_bytes),
        )


class ExtractTests(unittest.TestCase):
    def test_stage_tuple_and_exact_records(self):
        self.assertEqual(STAGES, ("search", "label", "blindspot", "integrate", "mirror"))
        result = extract_prompts(inline_source())
        self.assertEqual(tuple(result), STAGES)
        for index, stage in enumerate(STAGES, 2):
            raw = f"原始 {stage}"
            self.assertEqual(result[stage], {
                "text": raw, "sha256": sha(raw.encode("utf-8")),
                "line_start": index, "line_end": index,
            })

    def test_raw_whitespace_and_line_endings_are_preserved(self):
        for newline in ("\n", "\r\n", "\r"):
            with self.subTest(newline=repr(newline)):
                raw = f"  开头{newline}{newline}末尾  "
                pairs = [(stage, "  开头\n\n末尾  ") for stage in STAGES]
                source = inline_source(pairs).replace("\n", newline)
                result = extract_prompts(source)
                for stage in STAGES:
                    self.assertEqual(result[stage]["text"], raw)
                    self.assertEqual(result[stage]["sha256"], sha(raw.encode("utf-8")))
                    self.assertEqual(result[stage]["line_end"] - result[stage]["line_start"], 2)

    def test_opening_and_closing_delimiter_lines(self):
        source = "前置行\r\n\r\n" + inline_source([(stage, "\n原文\n") for stage in STAGES])
        result = extract_prompts(source)
        for index, stage in enumerate(STAGES):
            self.assertEqual(result[stage]["line_start"], 4 + index * 3)
            self.assertEqual(result[stage]["line_end"], 6 + index * 3)

    def test_literal_braces_and_terminators_do_not_end_object(self):
        raw = '原文 }; { "a": [1, 2] } $ { value }'
        result = extract_prompts(inline_source([(stage, raw) for stage in STAGES]))
        self.assertEqual(result["integrate"]["text"], raw)

    def test_suffix_is_not_parsed_and_trailing_comma_is_supported(self):
        source = inline_source().replace("\n};", ",\n};") + "\n后续原始页面不执行"
        self.assertEqual(tuple(extract_prompts(source)), STAGES)

    def test_reordered_keys_are_returned_in_stage_order(self):
        pairs = [(stage, stage) for stage in reversed(STAGES)]
        self.assertEqual(tuple(extract_prompts(inline_source(pairs))), STAGES)

    def test_non_string_inputs_rejected(self):
        for value in (None, b"const PROMPT_TEMPLATES = {};", {}, 1):
            with self.subTest(value=value), self.assertRaises(TypeError):
                extract_prompts(value)

    def test_missing_or_ambiguous_declaration_rejected(self):
        source = inline_source()
        for value in ("", source + "\n" + source, source + "\nconst PROMPT_TEMPLATES = other;"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                extract_prompts(value)

    def test_similar_identifiers_do_not_match(self):
        for name in ("PROMPT_TEMPLATES_v2", "PROMPT_TEMPLATES$", "PROMPT_TEMPLATESX"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                extract_prompts(inline_source().replace("PROMPT_TEMPLATES", name))
        source = inline_source().replace("PROMPT_TEMPLATES", "PROMPT_TEMPLATES_v2")
        self.assertEqual(tuple(extract_prompts(source + "\n" + inline_source())), STAGES)

    def test_missing_duplicate_and_extra_keys_rejected(self):
        pairs = [(stage, stage) for stage in STAGES]
        cases = [[], pairs[:-1], pairs + [("search", "重复")], pairs + [("extra", "额外")]]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                extract_prompts(inline_source(case))

    def test_empty_and_whitespace_templates_rejected(self):
        for raw in ("", " ", "\r\n\t"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                extract_prompts(inline_source([(stage, raw) for stage in STAGES]))

    def test_escapes_and_interpolation_rejected(self):
        for raw in (r"a\nb", r"\u4e00", r"\x61", "a\\`b", "a\\\\b", "a\\\nb", "${}", "${value}", "${`nested`}"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                extract_prompts(inline_source([(stage, raw) for stage in STAGES]))

    def test_expressions_and_nonliteral_values_rejected(self):
        source = inline_source()
        for value in ('"原文"', "'原文'", "other", "fn()", "null", "{a: 1}", "[`原文`]", "tag`原文`", "(`原文`)", "`原文` + `追加`"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                extract_prompts(source.replace("`原始 search`", value))
        for initializer in ("({", "fn({", "other || {", "/* 注释 */ {"):
            with self.subTest(initializer=initializer), self.assertRaises(ValueError):
                extract_prompts(source.replace("= {", "= " + initializer))

    def test_quoted_computed_spread_and_method_keys_rejected(self):
        for member in ('"search":', "'search':", "[search]:", "...search,", "get search() {", "search() {", "search /* 注释 */:"):
            with self.subTest(member=member), self.assertRaises(ValueError):
                extract_prompts(inline_source().replace("search:", member, 1))

    def test_unclosed_or_unsupported_object_structure_rejected(self):
        source = inline_source()
        cases = (
            source[:-1], source[:-2], source[:-3], source + "\nconst PROMPT_TEMPLATES",
            source.replace("};", "} ;"), source.replace("};", "} + other;"),
            source.replace("`,", "`", 1),
            source.replace("`,", "`,,", 1), source.replace("  label:", "  // 注释\n  label:"),
        )
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                extract_prompts(value)


class LoadTests(LocalFilesTests):
    def test_valid_bundle_and_expected_content_hash(self):
        bundle = sample_bundle()
        path = self.save_bundle(bundle)
        self.assertEqual(load_prompt_bundle(path), bundle)
        self.assertEqual(load_prompt_bundle(path, bundle["bundle_sha256"]), bundle)

    def test_file_byte_hash_is_not_bundle_hash(self):
        bundle = sample_bundle()
        path = self.save_bytes(json.dumps(bundle, ensure_ascii=True, indent=4).encode("utf-8") + b"\n")
        self.assertNotEqual(sha(path.read_bytes()), bundle["bundle_sha256"])
        self.assertEqual(load_prompt_bundle(path, bundle["bundle_sha256"]), bundle)
        with self.assertRaises(ValueError):
            load_prompt_bundle(path, sha(path.read_bytes()))

    def test_invalid_or_wrong_expected_hash_rejected(self):
        path = self.save_bundle(sample_bundle())
        for expected in ("0" * 64, "A" * 64, "bad", "", 123, False):
            with self.subTest(expected=expected), self.assertRaises(ValueError):
                load_prompt_bundle(path, expected)

    def test_top_level_fields_and_versions_are_exact(self):
        original = sample_bundle()
        cases = [[], None, {**original, "timestamp": "不允许"}]
        for key in BUNDLE_FIELDS:
            bundle = copy.deepcopy(original)
            del bundle[key]
            cases.append(bundle)
        for key in ("schema_version", "version", "prompt_source"):
            bundle = copy.deepcopy(original)
            bundle[key] = "其他版本"
            cases.append(seal(bundle))
        for bundle in cases:
            with self.subTest(bundle=bundle), self.assertRaises(ValueError):
                load_prompt_bundle(self.save_bundle(bundle))

    def test_prompt_stage_set_is_exact(self):
        for prompts in ([], {}, {**sample_bundle()["prompts"], "extra": {}}, {stage: value for stage, value in sample_bundle()["prompts"].items() if stage != "mirror"}):
            bundle = sample_bundle()
            bundle["prompts"] = prompts
            with self.subTest(prompts=prompts), self.assertRaises(ValueError):
                load_prompt_bundle(self.save_bundle(seal(bundle)))

    def test_prompt_record_fields_are_exact(self):
        record = sample_bundle()["prompts"]["search"]
        cases = [[], None, {**record, "extra": True}]
        cases.extend({key: value for key, value in record.items() if key != removed} for removed in record)
        for value in cases:
            bundle = sample_bundle()
            bundle["prompts"]["search"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                load_prompt_bundle(self.save_bundle(seal(bundle)))

    def test_empty_nonstring_and_unsupported_text_rejected(self):
        for value in ("", "  \n", None, 3, [], r"有\n转义", "${value}", "内嵌`反引号"):
            bundle = sample_bundle()
            bundle["prompts"]["search"]["text"] = value
            if isinstance(value, str):
                bundle["prompts"]["search"]["sha256"] = sha(value.encode("utf-8"))
            with self.subTest(value=value), self.assertRaises(ValueError):
                load_prompt_bundle(self.save_bundle(seal(bundle)))

    def test_hash_formats_rejected(self):
        for key in ("source_entry_sha256", "source_zip_sha256", "bundle_sha256", "prompt_sha256"):
            for value in (None, "A" * 64, "0" * 63, "0" * 64 + "\n", 123):
                bundle = sample_bundle()
                if key == "prompt_sha256":
                    bundle["prompts"]["search"]["sha256"] = value
                else:
                    bundle[key] = value
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    load_prompt_bundle(self.save_bundle(bundle))

    def test_text_tampering_rejected_even_with_new_bundle_hash(self):
        for reseal in (False, True):
            bundle = sample_bundle()
            bundle["prompts"]["search"]["text"] += "篡改"
            if reseal:
                seal(bundle)
            with self.subTest(reseal=reseal), self.assertRaises(ValueError):
                load_prompt_bundle(self.save_bundle(bundle))

    def test_prompt_and_bundle_hash_tampering_rejected(self):
        for field in ("prompt", "bundle", "source"):
            bundle = sample_bundle()
            if field == "prompt":
                bundle["prompts"]["search"]["sha256"] = "0" * 64
                seal(bundle)
            elif field == "bundle":
                bundle["bundle_sha256"] = "0" * 64
            else:
                bundle["source_zip_sha256"] = "0" * 64
            with self.subTest(field=field), self.assertRaises(ValueError):
                load_prompt_bundle(self.save_bundle(bundle))

    def test_line_ranges_reject_bool_float_and_inconsistent_span(self):
        for start, end in ((True, 2), (1, True), (1.0, 2), (1, 2.0), (0, 1), (-1, 0), (2, 1), (1, 3), ("1", 2)):
            bundle = sample_bundle()
            bundle["prompts"]["search"].update(line_start=start, line_end=end)
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                load_prompt_bundle(self.save_bundle(seal(bundle)))

    def test_duplicate_json_keys_at_every_depth_rejected(self):
        raw = json.dumps(sample_bundle(), ensure_ascii=False)
        cases = (
            raw.replace('"schema_version": "1.0"', '"schema_version": "1.0", "schema_version": "1.0"'),
            raw.replace('"prompts": {', '"prompts": {"search": {}, ', 1),
            raw.replace('"line_start": 1', '"line_start": 1, "line_start": 1', 1),
            raw.replace('"schema_version": "1.0"', '"schema_version": "1.0", "schema\\u005fversion": "1.0"'),
        )
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                load_prompt_bundle(self.save_bytes(value.encode("utf-8")))

    def test_nonfinite_constants_and_float_overflow_rejected(self):
        raw = json.dumps(sample_bundle())
        for constant in ("NaN", "Infinity", "-Infinity", "1e400", "-1e400"):
            data = raw.replace('"line_start": 1', '"line_start": ' + constant, 1)
            with self.subTest(constant=constant), self.assertRaisesRegex(ValueError, "严格 UTF-8 JSON"):
                load_prompt_bundle(self.save_bytes(data.encode("utf-8")))

    def test_malformed_non_utf8_and_surrogate_json_rejected(self):
        raw = json.dumps(sample_bundle())
        cases = [b"", b"{", b"\xff", b"{} {}", b"{} // comment", b'{"a":1,}', raw.replace('"text": "', '"text": "\\ud800', 1).encode("utf-8")]
        for data in cases:
            with self.subTest(data=data), self.assertRaises(ValueError):
                load_prompt_bundle(self.save_bytes(data))

    def test_load_missing_file_does_not_create_it(self):
        path = self.root / "missing.json"
        with self.assertRaises(FileNotFoundError):
            load_prompt_bundle(path)
        self.assertFalse(path.exists())


class FreezeTests(LocalFilesTests):
    def test_freeze_exact_schema_and_reproducible_bytes(self):
        sources = self.make_sources()
        first, second = self.root / "first.json", self.root / "second.json"
        bundle = freeze_prompts(*sources, first)
        self.assertEqual(set(bundle), BUNDLE_FIELDS)
        self.assertEqual(bundle, freeze_prompts(*sources, second))
        self.assertEqual(first.read_bytes(), second.read_bytes())
        self.assertEqual(bundle, load_prompt_bundle(first, bundle["bundle_sha256"]))
        content = {key: value for key, value in bundle.items() if key != "bundle_sha256"}
        raw = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        self.assertEqual(bundle["bundle_sha256"], sha(raw.encode("utf-8")))

    def test_existing_identical_bundle_verified_without_overwrite(self):
        sources = self.make_sources()
        path = self.root / "bundle.json"
        expected = freeze_prompts(*sources, path)
        before = path.read_bytes(), path.stat().st_mtime_ns
        self.assertEqual(freeze_prompts(*sources, path), expected)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_existing_same_content_with_different_layout_preserved(self):
        sources = self.make_sources()
        expected = freeze_prompts(*sources, self.root / "canonical.json")
        path = self.save_bytes(json.dumps(expected, indent=2, ensure_ascii=True).encode("utf-8"))
        before = path.read_bytes(), path.stat().st_mtime_ns
        self.assertEqual(freeze_prompts(*sources, path), expected)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_existing_different_valid_bundle_never_overwritten(self):
        sources = self.make_sources()
        other_sources = self.make_sources(("\n" + inline_source()).encode("utf-8"))
        path = self.root / "bundle.json"
        freeze_prompts(*sources, path)
        before = path.read_bytes()
        with self.assertRaises(FileExistsError):
            freeze_prompts(*other_sources, path)
        self.assertEqual(path.read_bytes(), before)

    def test_existing_invalid_bundle_is_not_repaired(self):
        sources = self.make_sources()
        for raw in (b"invalid", b"{}", json.dumps(sample_bundle()).replace('"schema_version": "1.0"', '"schema_version": "1.0", "schema_version": "1.0"').encode("utf-8")):
            path = self.save_bytes(raw)
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                freeze_prompts(*sources, path)
            self.assertEqual(path.read_bytes(), raw)

    def test_source_hashes_checked_before_zip_or_prompt_parsing(self):
        entry, archive, entry_hash, archive_hash = self.make_sources()
        output = self.root / "bundle.json"
        with mock.patch("cocoon.prompts.zipfile.ZipFile", side_effect=AssertionError("不应打开 ZIP")), mock.patch("cocoon.prompts.extract_prompts", side_effect=AssertionError("不应解析提示词")):
            for left, right in (("0" * 64, archive_hash), (entry_hash, "0" * 64)):
                with self.subTest(left=left, right=right), self.assertRaises(ValueError):
                    freeze_prompts(entry, archive, left, right, output)
        self.assertFalse(output.exists())

    def test_invalid_expected_source_hashes_rejected(self):
        entry, archive, entry_hash, archive_hash = self.make_sources()
        output = self.root / "bundle.json"
        for invalid in (None, "", "A" * 64, "x" * 64, 1):
            for hashes in ((invalid, archive_hash), (entry_hash, invalid)):
                with self.subTest(hashes=hashes), self.assertRaises(ValueError):
                    freeze_prompts(entry, archive, *hashes, output)
        self.assertFalse(output.exists())

    def test_zip_entry_bytes_must_match_even_when_hashes_match(self):
        entry = inline_source().encode("utf-8")
        for other in (entry + b"x", entry.replace(b"search", b"SEARCH", 1)):
            sources = self.make_sources(entry, [("index.html", other)])
            output = self.root / "bundle.json"
            with self.subTest(other=other), self.assertRaises(ValueError):
                freeze_prompts(*sources, output)
            self.assertFalse(output.exists())

    def test_zip_missing_nested_or_duplicate_index_rejected(self):
        entry = inline_source().encode("utf-8")
        for members in ([], [("nested/index.html", entry)], [("index.html", entry), ("index.html", entry)]):
            sources = self.make_sources(entry, members)
            with self.subTest(members=members), self.assertRaises(ValueError):
                freeze_prompts(*sources, self.root / "bundle.json")
        self.assertFalse((self.root / "bundle.json").exists())

    def test_unrelated_zip_members_are_not_parsed(self):
        entry = inline_source().encode("utf-8")
        sources = self.make_sources(entry, [("index.html", entry), ("different.txt", b"${unsupported}")])
        bundle = freeze_prompts(*sources, self.root / "bundle.json")
        self.assertEqual(bundle["prompts"], extract_prompts(entry.decode("utf-8")))

    def test_invalid_zip_with_correct_hash_rejected(self):
        entry = inline_source().encode("utf-8")
        archive = b"not a ZIP"
        output = self.root / "bundle.json"
        with self.assertRaises(ValueError):
            freeze_prompts(self.save_bytes(entry, ".html"), self.save_bytes(archive, ".zip"), sha(entry), sha(archive), output)
        self.assertFalse(output.exists())

    def test_zip_crc_failure_rejected_without_output(self):
        entry, archive, entry_hash, _ = self.make_sources()
        corrupted = archive.read_bytes().replace(b"const PROMPT_TEMPLATES", b"CONST PROMPT_TEMPLATES", 1)
        output = self.root / "bundle.json"
        with self.assertRaises(ValueError):
            freeze_prompts(entry, self.save_bytes(corrupted, ".zip"), entry_hash, sha(corrupted), output)
        self.assertFalse(output.exists())

    def test_bad_utf8_or_unsupported_source_never_creates_output(self):
        for data in (b"\xff", b"const PROMPT_TEMPLATES = other;", inline_source().replace("原始 search", "${value}").encode("utf-8")):
            sources = self.make_sources(data)
            with self.subTest(data=data), self.assertRaises(ValueError):
                freeze_prompts(*sources, self.root / "bundle.json")
        self.assertFalse((self.root / "bundle.json").exists())

    def test_missing_output_parent_not_created(self):
        sources = self.make_sources()
        output = self.root / "missing" / "bundle.json"
        with self.assertRaises(FileNotFoundError):
            freeze_prompts(*sources, output)
        self.assertFalse(output.parent.exists())

    def test_missing_source_never_creates_output(self):
        entry, archive, entry_hash, archive_hash = self.make_sources()
        for left, right in ((self.root / "missing.html", archive), (entry, self.root / "missing.zip")):
            with self.subTest(left=left, right=right), self.assertRaises(FileNotFoundError):
                freeze_prompts(left, right, entry_hash, archive_hash, self.root / "bundle.json")
        self.assertFalse((self.root / "bundle.json").exists())

    def test_racing_creation_is_verified_not_overwritten(self):
        sources = self.make_sources()
        output = self.root / "bundle.json"

        def competing_writer(path, bundle):
            write_json_new(path, bundle)
            raise FileExistsError("模拟并发创建同一内容")

        with mock.patch("cocoon.prompts.write_json_new", side_effect=competing_writer):
            bundle = freeze_prompts(*sources, output)
        self.assertEqual(bundle, load_prompt_bundle(output))


class VerifyTests(LocalFilesTests):
    def setUp(self):
        super().setUp()
        self.sources = self.make_sources()
        self.bundle = freeze_prompts(*self.sources, self.root / "bundle.json")

    def test_verify_returns_evidence_without_mutation(self):
        before = copy.deepcopy(self.bundle)
        files = {path.name: path.read_bytes() for path in self.root.iterdir()}
        result = verify_original_sources(self.bundle, *self.sources[:2])
        self.assertTrue(result["hashes_verified"])
        self.assertTrue(result["zip_entry_verified"])
        self.assertTrue(result["prompts_verified"])
        self.assertEqual(result["stage_count"], 5)
        self.assertEqual(result["source_entry_sha256"], self.sources[2])
        self.assertEqual(result["source_zip_sha256"], self.sources[3])
        self.assertEqual(result["bundle_sha256"], self.bundle["bundle_sha256"])
        self.assertEqual(self.bundle, before)
        self.assertEqual(files, {path.name: path.read_bytes() for path in self.root.iterdir()})

    def test_resealed_text_not_matching_original_rejected(self):
        prompt = self.bundle["prompts"]["search"]
        prompt["text"] += "不是原件"
        prompt["sha256"] = sha(prompt["text"].encode("utf-8"))
        seal(self.bundle)
        load_prompt_bundle(self.save_bundle(self.bundle))
        with self.assertRaises(ValueError):
            verify_original_sources(self.bundle, *self.sources[:2])

    def test_resealed_lines_not_matching_original_rejected(self):
        prompt = self.bundle["prompts"]["search"]
        prompt["line_start"] += 1
        prompt["line_end"] += 1
        seal(self.bundle)
        load_prompt_bundle(self.save_bundle(self.bundle))
        with self.assertRaises(ValueError):
            verify_original_sources(self.bundle, *self.sources[:2])

    def test_wrong_source_hashes_rejected(self):
        for field in ("source_entry_sha256", "source_zip_sha256"):
            bundle = copy.deepcopy(self.bundle)
            bundle[field] = "0" * 64
            with self.subTest(field=field), self.assertRaises(ValueError):
                verify_original_sources(seal(bundle), *self.sources[:2])

    def test_bad_bundle_rejected_before_source_reads(self):
        self.bundle["bundle_sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            verify_original_sources(self.bundle, self.root / "missing.html", self.root / "missing.zip")

    def test_zip_relationship_is_rechecked(self):
        entry = self.sources[0].read_bytes()
        _, archive, _, archive_hash = self.make_sources(entry, [("index.html", entry + b"x")])
        self.bundle["source_zip_sha256"] = archive_hash
        with self.assertRaises(ValueError):
            verify_original_sources(seal(self.bundle), self.sources[0], archive)


class OriginalTests(LocalFilesTests):
    def test_core_and_actual_source_hashes_match_pinned_values(self):
        core = json.loads((ROOT / "configs/core.json").read_bytes())
        assistant = core["assistant"]
        self.assertEqual(Path(assistant["entry_path"]).resolve(), ENTRY.resolve())
        self.assertEqual(Path(assistant["source_path"]).resolve(), ZIP.resolve())
        self.assertEqual(assistant["source_sha256"], ZIP_SHA)
        self.assertEqual(sha(ENTRY.read_bytes()), ENTRY_SHA)
        self.assertEqual(sha(ZIP.read_bytes()), ZIP_SHA)
        with zipfile.ZipFile(io.BytesIO(ZIP.read_bytes())) as archive:
            self.assertEqual(archive.read("index.html"), ENTRY.read_bytes())

    def test_actual_extract_matches_exact_line_slices_and_raw_hashes(self):
        text = ENTRY.read_bytes().decode("utf-8")
        prompts = extract_prompts(text)
        self.assertEqual(tuple(prompts), STAGES)
        ranges = ((1190, 1202), (1204, 1215), (1217, 1227), (1229, 1245), (1247, 1267))
        lines = text.splitlines(keepends=True)
        for stage, (start, end) in zip(STAGES, ranges):
            segment = "".join(lines[start - 1:end])
            raw = segment[segment.index("`") + 1:segment.rindex("`")]
            self.assertEqual(prompts[stage], {
                "text": raw, "sha256": sha(raw.encode("utf-8")),
                "line_start": start, "line_end": end,
            })

    def test_actual_freeze_reproduces_and_preserves_originals(self):
        before = ENTRY.read_bytes(), ZIP.read_bytes()
        first, second = self.root / "first.json", self.root / "second.json"
        bundle = freeze_prompts(ENTRY, ZIP, ENTRY_SHA, ZIP_SHA, first)
        self.assertEqual(bundle, freeze_prompts(ENTRY, ZIP, ENTRY_SHA, ZIP_SHA, second))
        self.assertEqual(first.read_bytes(), second.read_bytes())
        self.assertEqual(bundle, freeze_prompts(ENTRY, ZIP, ENTRY_SHA, ZIP_SHA, first))
        self.assertEqual(load_prompt_bundle(first, bundle["bundle_sha256"]), bundle)
        self.assertTrue(verify_original_sources(bundle, ENTRY, ZIP)["hashes_verified"])
        self.assertEqual((ENTRY.read_bytes(), ZIP.read_bytes()), before)


if __name__ == "__main__":
    unittest.main()
