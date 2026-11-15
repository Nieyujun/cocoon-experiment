"""仅使用封闭虚构交通规则，离线检查契约，不写临时文件。"""

from copy import deepcopy
import hashlib
import json
import unittest

from cocoon.contracts import STAGE_SECTIONS, output_contract, parse_stage_output, validate_packet


USER_VIEW = "在封闭虚构棋盘中，我倾向于让蓝色棋子先行，但愿意比较轮流通行规则。"
USER_QUOTE = "我倾向于让蓝色棋子先行"


def make_packet(count=2, is_demo=True):
    evidence = []
    for index in range(count):
        text = f"封闭虚构棋盘第{index}号路口规则：蓝色棋子先行，黄色棋子等待一轮。"
        evidence.append({
            "evidence_id": f"Evidence{index}",
            "text": text,
            "source_url": (
                f"synthetic://fixture/traffic/{index}" if is_demo
                else f"https://traffic-fixture.invalid/rules/{index}"
            ),
            "source_family_id": f"Family{index}",
            "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        })
    return {
        "schema_version": "1.0",
        "packet_id": "PacketTraffic",
        "topic_id": "TopicTraffic",
        "topic": "封闭虚构棋盘中，蓝色棋子先行还是两色棋子轮流通行？",
        "stance_axis": {
            "name": "虚构路口通行方案",
            "negative_label": "蓝色棋子优先",
            "positive_label": "两色棋子轮流",
        },
        "evidence": evidence,
        "is_demo": is_demo,
    }


def set_text(entry, text):
    entry["text"] = text
    entry["content_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()


def changed(original, path, value):
    result = deepcopy(original)
    target = result
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return result


def make_output(packet, stage="search", status="evidence_based"):
    sections = [{"name": name, "items": []} for name in STAGE_SECTIONS[stage]]
    item = {
        "text": "封闭虚构棋盘的第零号路口规则让蓝色棋子先行。",
        "citations": [packet["evidence"][0]["evidence_id"]] if status == "evidence_based" else [],
        "epistemic_status": status,
    }
    if stage == "mirror" and status == "evidence_based":
        item["user_quote"] = USER_QUOTE
    sections[0]["items"].append(item)
    return {"schema_version": "1.0", "stage": stage, "packet_id": packet["packet_id"], "sections": sections}


def encode(output):
    return json.dumps(output, ensure_ascii=False, allow_nan=False)


class PacketTests(unittest.TestCase):
    def setUp(self):
        self.packet = make_packet()

    def valid(self, packet=None):
        packet = self.packet if packet is None else packet
        before = deepcopy(packet)
        try:
            return validate_packet(packet, allow_demo=True)
        finally:
            self.assertEqual(packet, before)

    def test_demo_requires_explicit_permission(self):
        with self.assertRaises(ValueError):
            validate_packet(self.packet)
        with self.assertRaises(ValueError):
            validate_packet(self.packet, allow_demo=False)
        self.assertEqual(self.valid(), self.packet)
        for value in (None, 0, 1, "true", [], float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_packet(self.packet, allow_demo=value)

    def test_formal_shape_accepts_only_https_without_demo_fallback(self):
        packet = make_packet(is_demo=False)
        self.assertEqual(validate_packet(packet), packet)
        packet["evidence"][0]["source_url"] = "synthetic://fixture/traffic/0"
        for allow_demo in (False, True):
            with self.subTest(allow_demo=allow_demo), self.assertRaises(ValueError):
                validate_packet(packet, allow_demo=allow_demo)

    def test_exact_fields_reject_all_extras_at_each_level(self):
        for path in ((), ("stance_axis",), ("evidence", 0)):
            for extra in ("label", "gold", "stance", "is_accurate", "justification", "answer", "metadata", "anything", "nul\x00", 1):
                packet = deepcopy(self.packet)
                target = packet
                for key in path:
                    target = target[key]
                target[extra] = None
                with self.subTest(path=path, extra=extra), self.assertRaises(ValueError):
                    self.valid(packet)

    def test_missing_fields_at_each_level(self):
        for path in ((), ("stance_axis",), ("evidence", 0)):
            original = self.packet
            for key in path:
                original = original[key]
            for missing in original:
                packet = deepcopy(self.packet)
                target = packet
                for key in path:
                    target = target[key]
                del target[missing]
                with self.subTest(path=path, missing=missing), self.assertRaises(ValueError):
                    self.valid(packet)

    def test_containers_are_exact_types(self):
        for value in (None, [], True, "packet", 1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_packet(value, allow_demo=True)
        for path in (("stance_axis",), ("evidence",), ("evidence", 0)):
            for value in (None, False, 1, "traffic", (), {}):
                with self.subTest(path=path, value=value), self.assertRaises(ValueError):
                    self.valid(changed(self.packet, path, value))

    def test_scalar_types_null_nan_and_infinity(self):
        paths = [
            ("schema_version",), ("packet_id",), ("topic_id",), ("topic",),
            ("stance_axis", "name"), ("stance_axis", "negative_label"), ("stance_axis", "positive_label"),
            ("evidence", 0, "evidence_id"), ("evidence", 0, "text"),
            ("evidence", 0, "source_url"), ("evidence", 0, "source_family_id"),
            ("evidence", 0, "content_sha256"),
        ]
        for path in paths:
            for value in (None, True, False, 1, 1.0, float("nan"), float("inf"), -float("inf"), [], {}):
                with self.subTest(path=path, value=value), self.assertRaises(ValueError):
                    self.valid(changed(self.packet, path, value))
        for value in (None, 0, 1, "false", [], float("nan")):
            with self.subTest(is_demo=value), self.assertRaises(ValueError):
                self.valid(changed(self.packet, ("is_demo",), value))

    def test_version_is_exact(self):
        for version in ("1", "1.0 ", "2.0", "", "1.0\x00"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                self.valid(changed(self.packet, ("schema_version",), version))

    def test_id_format_for_every_id_field(self):
        for path in (("packet_id",), ("topic_id",), ("evidence", 0, "evidence_id"), ("evidence", 0, "source_family_id")):
            for value in ("", "1a", "_a", "-a", "a.b", "a b", " a", "a\n", "路口", "A" * 65, "a\x00"):
                with self.subTest(path=path, value=value), self.assertRaises(ValueError):
                    self.valid(changed(self.packet, path, value))
            for value in ("Z", "a_0-9", "Z" + "a" * 63):
                with self.subTest(path=path, valid=value):
                    self.valid(changed(self.packet, path, value))

    def test_only_evidence_ids_must_be_unique(self):
        with self.assertRaisesRegex(ValueError, "evidence_id.*重复"):
            self.valid(changed(self.packet, ("evidence", 1, "evidence_id"), "Evidence0"))
        self.valid(changed(self.packet, ("evidence", 1, "evidence_id"), "evidence0"))
        self.valid(changed(self.packet, ("evidence", 1, "source_family_id"), "Family0"))
        shared = "Traffic0"
        self.packet["packet_id"] = shared
        self.packet["topic_id"] = shared
        self.packet["evidence"][0]["evidence_id"] = shared
        for entry in self.packet["evidence"]:
            entry["source_family_id"] = shared
        self.assertEqual(self.valid(), self.packet)

    def test_hash_is_lowercase_sha256_of_exact_utf8_text(self):
        entry = self.packet["evidence"][0]
        for digest in ("0" * 64, entry["content_sha256"].upper(), entry["content_sha256"] + " ", ""):
            with self.subTest(digest=digest), self.assertRaises(ValueError):
                self.valid(changed(self.packet, ("evidence", 0, "content_sha256"), digest))
        entry["text"] += " "
        with self.assertRaises(ValueError):
            self.valid()
        set_text(entry, "  封闭虚构规则：蓝色棋子等待\n两轮。  ")
        self.assertEqual(self.valid()["evidence"][0]["text"], entry["text"])

    def test_evidence_count_bounds(self):
        for count in (1, 100):
            with self.subTest(count=count):
                self.valid(make_packet(count))
        for count in (0, 101):
            with self.subTest(count=count), self.assertRaises(ValueError):
                self.valid(make_packet(count))

    def test_topic_and_evidence_length_boundaries(self):
        self.valid(changed(self.packet, ("topic",), "路" * 2000))
        for text in ("", " \n\t", "路" * 2001):
            with self.subTest(topic_length=len(text)), self.assertRaises(ValueError):
                self.valid(changed(self.packet, ("topic",), text))
        for text, allowed in (("路" * 20000, True), ("路" * 20001, False), ("", False), (" \n", False)):
            packet = deepcopy(self.packet)
            set_text(packet["evidence"][0], text)
            if allowed:
                self.valid(packet)
            else:
                with self.subTest(text_length=len(text)), self.assertRaises(ValueError):
                    self.valid(packet)

    def test_total_evidence_characters_boundary(self):
        packet = make_packet(10)
        for entry in packet["evidence"]:
            set_text(entry, "路" * 20000)
        self.valid(packet)
        extra = make_packet(11)["evidence"][-1]
        set_text(extra, "路")
        packet["evidence"].append(extra)
        with self.assertRaises(ValueError):
            self.valid(packet)

    def test_plain_text_rejects_html_but_allows_comparisons(self):
        for path in (("topic",), ("stance_axis", "name"), ("stance_axis", "negative_label"), ("stance_axis", "positive_label"), ("evidence", 0, "text")):
            for text in ("<b>虚构规则</b>", "<IMG SRC=x>", "规则</p>", "<!--规则-->", "<!DOCTYPE html>", "<?规则?>", "<x-rule />", "<![CDATA[虚构规则]]>", "<![invalid]>", "规则\x00", "规则\ud800"):
                packet = changed(self.packet, path, text)
                if path == ("evidence", 0, "text") and "\ud800" not in text:
                    set_text(packet["evidence"][0], text)
                with self.subTest(path=path, text=repr(text)), self.assertRaises(ValueError):
                    self.valid(packet)
        text = "封闭虚构规则：等待轮次<2且通行次数 >1。label、gold、stance、is_accurate、token、secret、password 是棋盘上的普通文字。"
        for path in (("topic",), ("stance_axis", "name"), ("stance_axis", "negative_label"), ("stance_axis", "positive_label")):
            with self.subTest(path=path):
                self.valid(changed(self.packet, path, text))
        set_text(self.packet["evidence"][0], text)
        self.valid()

    def test_axis_is_nonempty_without_semantic_classification(self):
        for field in ("name", "negative_label", "positive_label"):
            for value in ("", " \t"):
                with self.subTest(field=field), self.assertRaises(ValueError):
                    self.valid(changed(self.packet, ("stance_axis", field), value))
        for negative, positive in (("左", "右"), ("Left", "Right"), ("同一路径", "同一路径")):
            packet = deepcopy(self.packet)
            packet["stance_axis"].update(name="左右轴", negative_label=negative, positive_label=positive)
            with self.subTest(negative=negative):
                self.valid(packet)

    def test_invalid_urls(self):
        urls = (
            "", " ", "\t\r\n", "NULL",
            "http://traffic-fixture.invalid/rule", "ftp://traffic-fixture.invalid/rule",
            "https:///rule", "https://", "//traffic-fixture.invalid/rule",
            "https://name@traffic-fixture.invalid/rule", "https://:pass@traffic-fixture.invalid/rule",
            "https://name:pass@traffic-fixture.invalid/rule", "https://@traffic-fixture.invalid/rule",
            "https://traffic-fixture.invalid/rule#part", "https://traffic-fixture.invalid/rule#",
            " https://traffic-fixture.invalid/rule", "https://traffic-fixture.invalid/rule ",
            "https://traffic-fixture.invalid/a b", "https://traffic-fixture.invalid/\nrule",
            "https://traffic-fixture.invalid/<b>rule</b>", "https://traffic-fixture.invalid/<!--rule-->",
            "https://traffic-fixture.invalid/\trule", "https://traffic-fixture.invalid/\x00rule",
            "https://traffic-fixture.invalid/\u3000rule", "https://traffic-fixture.invalid\\rule",
            "https://traffic-fixture.invalid:wrong/rule", "https://traffic-fixture.invalid:65536/rule",
            "https://[broken/rule", "https://traffic%20fixture.invalid/rule",
            "https://traffic-fixture.invalid/\x7frule", "https://traffic-fixture.invalid/\x01rule",
            "https://traffic／fixture.invalid/rule", "https://traffic<fixture.invalid/rule",
            "synthetic://other/traffic/0", "synthetic://fixture", "synthetic://fixture/",
            "synthetic://fixture:80/traffic/0", "synthetic://fixture:/traffic/0",
            "synthetic://name@fixture/traffic/0", "synthetic://fixture/traffic/0#part",
        )
        for url in urls:
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.valid(changed(self.packet, ("evidence", 0, "source_url"), url))

    def test_sensitive_url_query_keys_are_rejected(self):
        keys = ("api_key", "token", "password", "secret", "access_token", "key")
        keys += tuple(key.upper() for key in keys) + ("%74oken", "%2574oken", "api%5fkey", "access%5Ftoken")
        for prefix in ("https://traffic-fixture.invalid/rule?", "synthetic://fixture/rule?"):
            for key in keys:
                for suffix in (f"{key}=hidden", f"{key}=", f"{key}", f"page=2&{key}=hidden", f"page=2&{key}=hidden&page=3"):
                    with self.subTest(prefix=prefix, suffix=suffix), self.assertRaises(ValueError):
                        self.valid(changed(self.packet, ("evidence", 0, "source_url"), prefix + suffix))

    def test_safe_url_queries_and_hosts_are_preserved(self):
        urls = (
            "https://traffic-fixture.invalid/rule?page=2&lang=zh",
            "https://traffic-fixture.invalid/rule?q=token&chapter=gold",
            "https://traffic-fixture.invalid/rule?monkey=blue&token_count=2&secret_lane=blue",
            "https://traffic-fixture.invalid/rule?q=api_key,token,password,secret,access_token,key",
            "https://traffic-fixture.invalid/rule?q=lane;token=blue",
            "https://traffic-fixture.invalid:8443/rule?chapter=2",
            "https://[2001:db8::1]/rule", "https://traffic-fixture.invalid/a%20b",
            "synthetic://fixture/traffic/0?chapter=2",
        )
        for url in urls:
            with self.subTest(url=url):
                packet = changed(self.packet, ("evidence", 0, "source_url"), url)
                self.assertEqual(self.valid(packet)["evidence"][0]["source_url"], url)

    def test_return_value_is_deep_copy_and_errors_do_not_mutate(self):
        before = deepcopy(self.packet)
        result = self.valid()
        self.assertEqual(result, before)
        self.assertIsNot(result, self.packet)
        self.assertIsNot(result["evidence"], self.packet["evidence"])
        result["evidence"][0]["text"] = "另一个虚构规则"
        result["stance_axis"]["name"] = "另一个方案轴"
        self.assertEqual(self.packet, before)
        self.packet["evidence"][0]["content_sha256"] = "invalid"
        before = deepcopy(self.packet)
        with self.assertRaises(ValueError):
            self.valid()
        self.assertEqual(self.packet, before)


class StageOutputTests(unittest.TestCase):
    def setUp(self):
        self.packet = make_packet()
        self.output = make_output(self.packet)

    def parse(self, output=None, stage="search", user_view=None):
        output = self.output if output is None else output
        packet_before = deepcopy(self.packet)
        output_before = deepcopy(output)
        try:
            return parse_stage_output(encode(output), stage, self.packet, user_view)
        finally:
            self.assertEqual(self.packet, packet_before)
            self.assertEqual(output, output_before)

    def test_exact_stage_sections(self):
        self.assertEqual(STAGE_SECTIONS, {
            "search": ("evidence_overview", "coverage_gaps"),
            "label": ("source_assessment",),
            "blindspot": ("missing_dimensions", "possible_biases", "counterpoints"),
            "integrate": ("consensus", "disagreements", "blindspots", "sources"),
            "mirror": ("stance_analysis", "possible_biases", "blindspot_alert", "suggestions"),
        })

    def test_all_stages_and_all_three_statuses(self):
        for stage in STAGE_SECTIONS:
            for status in ("evidence_based", "uncertainty", "suggestion"):
                with self.subTest(stage=stage, status=status):
                    output = make_output(self.packet, stage, status)
                    result = self.parse(output, stage, USER_VIEW if stage == "mirror" else None)
                    self.assertEqual(result["output"], output)
                    self.assertEqual(result["citation_audit"], {
                        "schema_valid": True, "known_evidence_only": True,
                        "cited_ids": ["Evidence0"] if status == "evidence_based" else [],
                        "semantic_entailment": "pending_independent_review", "formal_eligible": False,
                    })

    def test_no_formal_eligibility_even_for_formal_packet_shape(self):
        self.packet = make_packet(is_demo=False)
        result = self.parse(make_output(self.packet))
        self.assertIs(result["citation_audit"]["formal_eligible"], False)

    def test_unknown_stages_and_mismatched_identity(self):
        for stage in ("SEARCH", "unknown", "", None, True, [], {}):
            with self.subTest(stage=stage), self.assertRaises(ValueError):
                parse_stage_output(encode(self.output), stage, self.packet)
        for field, value in (("schema_version", "2.0"), ("schema_version", True), ("stage", "label"), ("stage", False), ("packet_id", "OtherPacket"), ("packet_id", True)):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.parse(changed(self.output, (field,), value))

    def test_strict_json_no_fences_repairs_or_prose(self):
        raw = encode(self.output)
        bad = (
            "```json\n" + raw + "\n```", "```" + raw + "```", "结果：" + raw,
            raw + " 完成", raw + raw, raw[:-1], raw + ",", "\ufeff" + raw,
            raw.replace('"1.0"', "'1.0'", 1), raw[:-1] + ",}",
            "//说明\n" + raw, "/*说明*/" + raw,
            "null", "true", "false", "[]", '"text"', "1", "", " \t\n",
        )
        for text in bad:
            with self.subTest(text=text[:30]), self.assertRaises(ValueError):
                parse_stage_output(text, "search", self.packet)
        self.assertEqual(parse_stage_output(" \r\n" + raw + "\t ", "search", self.packet)["output"], self.output)

    def test_raw_text_type_unicode_null_and_length(self):
        for raw in (None, True, {}, [], 1, b"{}", "\ud800", "\x00{}"):
            with self.subTest(raw=repr(raw)), self.assertRaises(ValueError):
                parse_stage_output(raw, "search", self.packet)
        raw = encode(self.output)
        at_limit = raw + " " * (200000 - len(raw))
        self.assertEqual(parse_stage_output(at_limit, "search", self.packet)["output"], self.output)
        with self.assertRaises(ValueError):
            parse_stage_output(at_limit + " ", "search", self.packet)
        with self.assertRaises(ValueError):
            parse_stage_output("[" * 2000 + "]" * 2000, "search", self.packet)

    def test_duplicate_json_keys_at_every_level(self):
        raw = encode(self.output)
        replacements = (
            ('"schema_version": "1.0"', '"schema_version": "1.0", "schema_version": "1.0"'),
            ('"schema_version": "1.0"', '"schema_version": "1.0", "schema_versi\\u006fn": "1.0"'),
            ('"name": "evidence_overview"', '"name": "evidence_overview", "name": "evidence_overview"'),
            ('"epistemic_status": "evidence_based"', '"epistemic_status": "evidence_based", "epistemic_status": "evidence_based"'),
            ('"citations": ["Evidence0"]', '"citations": ["Evidence0"], "citations": []'),
        )
        for old, new in replacements:
            with self.subTest(old=old), self.assertRaises(ValueError):
                parse_stage_output(raw.replace(old, new, 1), "search", self.packet)

    def test_nonfinite_json_literals_and_overflow(self):
        raw = encode(self.output)
        for constant in ("NaN", "Infinity", "-Infinity", "inf", "nan", "1e999", "-1e999"):
            with self.subTest(constant=constant), self.assertRaises(ValueError):
                parse_stage_output(raw.replace('"text": ', '"text": ' + constant + ', "hidden": ', 1), "search", self.packet)
        for value in (None, True, False, 1, 1.5, [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.parse(changed(self.output, ("sections", 0, "items", 0, "text"), value))

    def test_exact_fields_and_sample_contamination(self):
        for path in ((), ("sections", 0), ("sections", 0, "items", 0)):
            for extra in ("label", "gold", "stance", "is_accurate", "justification", "answer", "metadata", "citation_audit", "formal_eligible", "other"):
                output = deepcopy(self.output)
                target = output
                for key in path:
                    target = target[key]
                target[extra] = "封闭虚构交通规则"
                with self.subTest(path=path, extra=extra), self.assertRaises(ValueError):
                    self.parse(output)
        item = self.output["sections"][0]["items"][0]
        item["text"] = "封闭虚构棋盘用 label、gold、stance、is_accurate、justification、answer、token 和 secret 作为普通标记。"
        self.assertEqual(self.parse()["output"], self.output)

    def test_missing_required_fields(self):
        for path in ((), ("sections", 0), ("sections", 0, "items", 0)):
            original = self.output
            for key in path:
                original = original[key]
            for missing in original:
                output = deepcopy(self.output)
                target = output
                for key in path:
                    target = target[key]
                del target[missing]
                with self.subTest(path=path, missing=missing), self.assertRaises(ValueError):
                    self.parse(output)

    def test_sections_must_be_complete_ordered_and_unique(self):
        sections = self.output["sections"]
        invalid = ([], sections[:1], sections + [sections[0]], sections[::-1], [sections[0], sections[0]], {}, True, None)
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.parse(changed(self.output, ("sections",), value))
        for value in ("other", None, True, [], {}):
            with self.subTest(name=value), self.assertRaises(ValueError):
                self.parse(changed(self.output, ("sections", 0, "name"), value))
        for value in (None, True, [], "section"):
            with self.subTest(section=value), self.assertRaises(ValueError):
                self.parse(changed(self.output, ("sections", 0), value))

    def test_items_count_and_container_type(self):
        item = self.output["sections"][0]["items"][0]
        self.parse(changed(self.output, ("sections", 0, "items"), [deepcopy(item) for _ in range(30)]))
        for value in ([], [item] * 31, None, True, {}, "item"):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ValueError):
                self.parse(changed(self.output, ("sections", 0, "items"), value))
        for value in (None, True, [], "item"):
            with self.subTest(item=value), self.assertRaises(ValueError):
                self.parse(changed(self.output, ("sections", 0, "items", 0), value))

    def test_item_text_boundaries_and_plain_text(self):
        path = ("sections", 0, "items", 0, "text")
        for text in ("路" * 8000, "封闭虚构规则：等待轮次<2且步数 >1。", "纯文本 &lt;b&gt; 标记"):
            with self.subTest(length=len(text)):
                self.parse(changed(self.output, path, text))
        for text in ("", " \n", "路" * 8001, "<b>虚构规则</b>", "<svg/>", "规则</p>", "<!--规则-->", "<!DOCTYPE html>", "<?规则?>", "规则\x00", "规则\ud800"):
            with self.subTest(text=repr(text[:20])), self.assertRaises(ValueError):
                self.parse(changed(self.output, path, text))

    def test_escaped_null_and_surrogates_are_rejected_after_json_decoding(self):
        path = ("sections", 0, "items", 0, "text")
        for text in ("虚构路口\x00参数", "虚构路口\ud800参数", "虚构路口\udfff参数"):
            raw = json.dumps(changed(self.output, path, text), ensure_ascii=True)
            with self.subTest(text=repr(text)), self.assertRaises(ValueError):
                parse_stage_output(raw, "search", self.packet)

    def test_citations_require_known_unique_evidence_ids(self):
        path = ("sections", 0, "items", 0, "citations")
        for value in ([], ["unknown"], ["Family0"], ["PacketTraffic"], ["Evidence0", "Evidence0"], [True], [1], [None], [[]], [{}], ["Evidence0\x00"], "Evidence0", {}, None, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.parse(changed(self.output, path, value))
        for status in ("uncertainty", "suggestion"):
            output = make_output(self.packet, status=status)
            self.assertEqual(self.parse(output)["citation_audit"]["cited_ids"], [])
            output["sections"][0]["items"][0]["citations"] = ["unknown"]
            with self.subTest(status=status), self.assertRaises(ValueError):
                self.parse(output)

    def test_cited_ids_are_sorted_unique_across_items_without_rewriting(self):
        item = self.output["sections"][0]["items"][0]
        item["citations"] = ["Evidence1", "Evidence0"]
        self.output["sections"][0]["items"].append(deepcopy(item))
        result = self.parse()
        self.assertEqual(result["citation_audit"]["cited_ids"], ["Evidence0", "Evidence1"])
        self.assertEqual(result["output"], self.output)
        self.assertEqual(result["output"]["sections"][0]["items"][0]["citations"], ["Evidence1", "Evidence0"])

    def test_unknown_epistemic_statuses_rejected(self):
        for value in ("fact", "certain", "EVIDENCE_BASED", "suggestions", "", "uncertainty ", True, 0, None, [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.parse(changed(self.output, ("sections", 0, "items", 0, "epistemic_status"), value))

    def test_every_section_accepts_every_status_and_other_sections_may_be_empty(self):
        for stage, names in STAGE_SECTIONS.items():
            for index, name in enumerate(names):
                for status in ("evidence_based", "uncertainty", "suggestion"):
                    output = make_output(self.packet, stage, status)
                    item = output["sections"][0]["items"].pop()
                    output["sections"][index]["items"] = [item]
                    with self.subTest(stage=stage, section=name, status=status):
                        result = self.parse(output, stage, USER_VIEW if stage == "mirror" else None)
                        self.assertEqual(result["output"], output)

    def test_all_empty_sections_rejected_in_every_stage(self):
        for stage in STAGE_SECTIONS:
            output = make_output(self.packet, stage)
            output["sections"][0]["items"].clear()
            with self.subTest(stage=stage), self.assertRaises(ValueError):
                self.parse(output, stage, USER_VIEW if stage == "mirror" else None)

    def test_each_section_enforces_item_limit_independently(self):
        for stage, names in STAGE_SECTIONS.items():
            for index, name in enumerate(names):
                output = make_output(self.packet, stage, "uncertainty")
                item = output["sections"][0]["items"].pop()
                for section in output["sections"]:
                    section["items"] = [deepcopy(item) for _ in range(30)]
                with self.subTest(stage=stage, section=name):
                    self.parse(output, stage, USER_VIEW if stage == "mirror" else None)
                    output["sections"][index]["items"].append(deepcopy(item))
                    with self.assertRaises(ValueError):
                        self.parse(output, stage, USER_VIEW if stage == "mirror" else None)

    def test_only_frozen_http_urls_are_allowed(self):
        self.packet = make_packet(is_demo=False)
        known = self.packet["evidence"][0]["source_url"]
        self.output = make_output(self.packet)
        path = ("sections", 0, "items", 0, "text")
        for text in (known, f"虚构规则见 {known}。", f"虚构规则见（{known}）。", f"规则 {known}，仍是封闭夹具。"):
            with self.subTest(text=text):
                self.parse(changed(self.output, path, text))
        bad = (
            "https://external-fixture.invalid/rule", "http://traffic-fixture.invalid/rules/0",
            known + "/extra", known + "?chapter=2", known + "?", known + "#fragment",
            known + ".extra", "HTTPS://external-fixture.invalid/rule", known + " https://external-fixture.invalid/rule",
            "https://traffic-fixture.invalid.evil.invalid/rules/0",
        )
        for text in bad:
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.parse(changed(self.output, path, text))
        frozen_query = known + "?chapter=2&lane=blue"
        self.packet["evidence"][0]["source_url"] = frozen_query
        self.parse(changed(self.output, path, frozen_query))
        with self.assertRaises(ValueError):
            self.parse(changed(self.output, path, known))

    def test_frozen_urls_are_matched_whole_even_with_punctuation_or_nested_urls(self):
        self.packet = make_packet(is_demo=False)
        base = self.packet["evidence"][0]["source_url"]
        self.packet["evidence"][1]["source_url"] = base
        path = ("sections", 0, "items", 0, "text")
        for known in (
            base + "/blue'yellow", base + "/蓝色，黄色",
            base + "?next=https://route-fixture.invalid/traffic",
            base + "?next='https://route-fixture.invalid/traffic'",
            base + "，https://route-fixture.invalid/traffic",
            "https://[2001:db8::1]/traffic", base.upper(),
        ):
            self.packet["evidence"][0]["source_url"] = known
            self.output = make_output(self.packet)
            with self.subTest(known=known):
                result = self.parse(changed(self.output, path, "虚构规则 " + known + "。"))
                self.assertEqual(result["output"]["sections"][0]["items"][0]["text"], "虚构规则 " + known + "。")
                with self.assertRaises(ValueError):
                    self.parse(changed(self.output, path, known + " https://new-fixture.invalid/rule"))

    def test_url_suffixes_are_not_trimmed_or_repaired(self):
        self.packet = make_packet(is_demo=False)
        path = ("sections", 0, "items", 0, "text")
        base = self.packet["evidence"][0]["source_url"]
        for known in (base, base + "?lane=blue"):
            self.packet["evidence"][0]["source_url"] = known
            self.output = make_output(self.packet)
            for suffix in (".", ",", ";", "!", ")", "]", "}"):
                with self.subTest(known=known, suffix=suffix), self.assertRaises(ValueError):
                    self.parse(changed(self.output, path, known + suffix))
        exact = base + "?lane=blue!"
        self.packet["evidence"][0]["source_url"] = exact
        self.parse(changed(self.output, path, exact))

    def test_parser_revalidates_packet(self):
        for path, value in ((("evidence", 0, "content_sha256"), "wrong"), (("is_demo",), False), (("evidence", 1, "evidence_id"), "Evidence0")):
            with self.subTest(path=path), self.assertRaises(ValueError):
                parse_stage_output(encode(self.output), "search", changed(self.packet, path, value))
        packet = deepcopy(self.packet)
        packet["gold"] = "隐藏答案"
        with self.assertRaises(ValueError):
            parse_stage_output(encode(self.output), "search", packet)

    def test_success_and_failure_leave_original_inputs_unchanged(self):
        packet_before = deepcopy(self.packet)
        output_before = deepcopy(self.output)
        raw = encode(self.output)
        result = parse_stage_output(raw, "search", self.packet)
        self.assertEqual(self.packet, packet_before)
        self.assertEqual(self.output, output_before)
        self.assertEqual(raw, encode(output_before))
        result["output"]["sections"][0]["items"][0]["citations"].clear()
        result["citation_audit"]["cited_ids"].append("Other")
        self.assertEqual(self.output, output_before)
        self.assertEqual(self.packet, packet_before)
        self.assertEqual(self.parse()["citation_audit"]["cited_ids"], ["Evidence0"])
        with self.assertRaises(ValueError):
            parse_stage_output(raw + " 尾部说明", "search", self.packet)
        self.assertEqual(self.packet, packet_before)

    def test_semantic_support_is_not_claimed_or_guessed(self):
        self.output["sections"][0]["items"][0]["text"] = "封闭虚构棋盘中黄色棋子先行。"
        result = self.parse()
        self.assertEqual(result["citation_audit"]["semantic_entailment"], "pending_independent_review")
        self.assertIs(result["citation_audit"]["formal_eligible"], False)


class MirrorTests(unittest.TestCase):
    def setUp(self):
        self.packet = make_packet()
        self.output = make_output(self.packet, "mirror")

    def parse(self, view=USER_VIEW):
        packet_before = deepcopy(self.packet)
        output_before = deepcopy(self.output)
        try:
            return parse_stage_output(encode(self.output), "mirror", self.packet, view)
        finally:
            self.assertEqual(self.packet, packet_before)
            self.assertEqual(self.output, output_before)

    def test_nonmirror_forbids_quote_for_every_status(self):
        for stage in ("search", "label", "blindspot", "integrate"):
            for status in ("evidence_based", "uncertainty", "suggestion"):
                output = make_output(self.packet, stage, status)
                item = next(section["items"][0] for section in output["sections"] if section["items"])
                item["user_quote"] = USER_QUOTE
                with self.subTest(stage=stage, status=status), self.assertRaises(ValueError):
                    parse_stage_output(encode(output), stage, self.packet, USER_VIEW)

    def test_view_required_nonempty_minimum_ten_characters(self):
        for view in (None, "", " " * 10, "路" * 9, True, 10, [], {}, "路" * 10 + "\x00", "<b>" + USER_VIEW + "</b>"):
            with self.subTest(view=view), self.assertRaises(ValueError):
                self.parse(view)
        self.output["sections"][0]["items"][0]["user_quote"] = "路" * 10
        self.parse("路" * 10)

    def test_evidence_based_requires_exact_nonempty_quote(self):
        item = self.output["sections"][0]["items"][0]
        self.parse()
        del item["user_quote"]
        with self.assertRaises(ValueError):
            self.parse()
        for quote in ("", " ", None, True, [], "我倾向让蓝色棋子先行", "我倾向于让蓝色棋子先行。", " " + USER_QUOTE, "<b>" + USER_QUOTE + "</b>", USER_QUOTE + "\x00"):
            item["user_quote"] = quote
            with self.subTest(quote=quote), self.assertRaises(ValueError):
                self.parse()

    def test_other_statuses_may_omit_quote_but_cannot_change_it(self):
        for status in ("uncertainty", "suggestion"):
            self.output = make_output(self.packet, "mirror", status)
            item = self.output["sections"][0]["items"][0]
            self.parse()
            item["user_quote"] = USER_QUOTE
            self.parse()
            for quote in ("蓝色棋子必须一直先行", "", None, False):
                item["user_quote"] = quote
                with self.subTest(status=status, quote=quote), self.assertRaises(ValueError):
                    self.parse()

    def test_quote_is_not_trimmed_or_unicode_normalized(self):
        item = self.output["sections"][0]["items"][0]
        view = "封闭虚构路口标记为 e\u0301 ，蓝色棋子先行。"
        item["user_quote"] = " e\u0301 "
        result = self.parse(view)
        self.assertEqual(result["output"]["sections"][0]["items"][0]["user_quote"], " e\u0301 ")
        item["user_quote"] = "é"
        with self.assertRaises(ValueError):
            self.parse(view)

    def test_quote_is_not_limited_by_item_text_length(self):
        item = self.output["sections"][0]["items"][0]
        for quote in ("路", "路" * 8000, "路" * 8001):
            item["user_quote"] = quote
            with self.subTest(length=len(quote)):
                self.parse("封闭虚构棋盘的路口参数 " + quote)

    def test_quote_external_link_rules(self):
        item = self.output["sections"][0]["items"][0]
        self.packet = make_packet(is_demo=False)
        known = self.packet["evidence"][0]["source_url"]
        item["user_quote"] = known
        self.parse("虚构规则 " + known)
        item["user_quote"] = "https://external-fixture.invalid/rule"
        with self.assertRaises(ValueError):
            self.parse("虚构规则 " + item["user_quote"])

    def test_quote_required_for_every_evidence_based_item(self):
        for index in range(len(STAGE_SECTIONS["mirror"])):
            self.output = make_output(self.packet, "mirror")
            self.output["sections"][index]["items"].append({
                "text": "蓝色棋子先行是虚构棋盘中的一种通行方案。",
                "citations": ["Evidence0"], "epistemic_status": "evidence_based",
            })
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.parse()

    def test_view_and_packet_are_unchanged(self):
        before = deepcopy(self.packet)
        view = "  " + USER_VIEW + "  "
        self.parse(view)
        self.assertEqual(self.packet, before)
        self.assertEqual(view, "  " + USER_VIEW + "  ")


class OutputContractTests(unittest.TestCase):
    def test_stable_json_serializable_constraints_for_every_stage(self):
        for stage, names in STAGE_SECTIONS.items():
            with self.subTest(stage=stage):
                contract = output_contract(stage)
                self.assertEqual(contract, output_contract(stage))
                self.assertEqual(json.loads(json.dumps(contract, allow_nan=False)), contract)
                self.assertEqual(contract["schema_version"], "1.0")
                self.assertEqual(contract["stage"], stage)
                self.assertEqual(contract["section_names"], list(names))
                self.assertEqual(contract["epistemic_statuses"], ["evidence_based", "uncertainty", "suggestion"])
                self.assertEqual(contract["allowed_item_fields"], ["text", "citations", "epistemic_status"] + (["user_quote"] if stage == "mirror" else []))
                self.assertEqual(contract["required_output_fields"], ["schema_version", "stage", "packet_id", "sections"])
                self.assertEqual(contract["required_section_fields"], ["name", "items"])
                self.assertEqual(contract["required_item_fields"], ["text", "citations", "epistemic_status"])
                self.assertNotIn("uncertainty_only_sections", contract)
                self.assertEqual(contract["semantic_entailment"], "pending_independent_review")
                self.assertIs(contract["formal_eligible"], False)
                self.assertIs(contract["additional_fields_allowed"], False)
                self.assertIn("不得声称", contract["retrieval_rule"])
                self.assertEqual(contract["limits"], {
                    "raw_text_max_characters": 200000,
                    "item_text_max_characters": 8000,
                    "items_per_section_max": 30,
                    "total_items_min": 1,
                    "empty_sections_allowed": True,
                })
                self.assertEqual(contract["citation_rules"], {
                    "known_evidence_ids_only": True,
                    "duplicates_allowed": False,
                    "evidence_based_min_citations": 1,
                    "uncertainty_min_citations": 0,
                    "suggestion_min_citations": 0,
                })
                self.assertEqual(contract["user_quote_rules"], {
                    "allowed": stage == "mirror",
                    "user_view_required": stage == "mirror",
                    "user_view_min_characters": 10 if stage == "mirror" else None,
                    "required_for_evidence_based": stage == "mirror",
                    "nonempty_exact_substring": True,
                    "optional_for_other_statuses": stage == "mirror",
                })
                self.assertIn("source_family_id 可以共享", contract["id_rule"])
                self.assertNotIn("example", contract)
                self.assertNotIn("output", contract)

    def test_contract_returns_fresh_nested_structures(self):
        before = output_contract("mirror")
        mutated = output_contract("mirror")
        mutated["section_names"].clear()
        mutated["allowed_item_fields"].clear()
        mutated["limits"]["items_per_section_max"] = 999
        mutated["citation_rules"]["known_evidence_ids_only"] = False
        self.assertEqual(output_contract("mirror"), before)
        self.assertEqual(STAGE_SECTIONS["mirror"], ("stance_analysis", "possible_biases", "blindspot_alert", "suggestions"))

    def test_unknown_stage_rejected(self):
        for stage in ("unknown", "SEARCH", "", True, 1, None, [], {}):
            with self.subTest(stage=stage), self.assertRaises(ValueError):
                output_contract(stage)


if __name__ == "__main__":
    unittest.main()
