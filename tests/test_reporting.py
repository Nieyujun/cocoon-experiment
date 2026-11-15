from html.parser import HTMLParser
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from cocoon.configuration import load_config, readiness
from cocoon.reporting import render_status, TEMPLATE
from cocoon.runtime import EventStore, canonical_hash, write_json_new

ROOT = Path(__file__).resolve().parents[1]


class Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.kind = None
        self.scripts = {}
        self.ids = []
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id"):
            self.ids.append(attrs["id"])
        if tag == "script":
            self.kind = attrs.get("id", "javascript")
            self.scripts[self.kind] = ""
    def handle_endtag(self, tag):
        if tag == "script":
            self.kind = None
    def handle_data(self, data):
        if self.kind:
            self.scripts[self.kind] += data


class ReportingTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="report-test-", dir=ROOT / "tests"))
        self.config = load_config(ROOT / "configs/core.json")

    def test_object_refs_and_unicode_record_boundaries(self):
        store = EventStore(self.root, {}, canonical_hash([]), True, "report-fixture")
        context = {"cards": [], "text": "字符\u2028不应\u2029断行\u0085"}
        blob = store.capture(context)
        event = store.append("fixture", input_ref=blob, payload={"text": "safe </script><script>bad()</script>\u2028"})
        run = {"run_directory": str(self.root)}
        target = self.root / "status.html"
        render_status(target, {}, run)
        parser = Scripts()
        parser.feed(target.read_text(encoding="utf-8"))
        embedded = json.loads(parser.scripts["data"])
        self.assertEqual(embedded["events"], [event])
        self.assertEqual(embedded["contexts"][blob["ref"]], context)
        self.assertNotIn("</script>", parser.scripts["data"])
        self.assertEqual(len(parser.ids), len(set(parser.ids)))

    def test_javascript_syntax(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node不可用，仅跳过JS解析")
        parser = Scripts()
        parser.feed(TEMPLATE.replace("__PAYLOAD__", "{}"))
        process = subprocess.run([node, "--check"], input=parser.scripts["javascript"],
                                 capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(process.returncode, 0, process.stderr)

    def test_corrupt_summary_is_not_passed(self):
        run = self.root / "experiment/demo/engineering_corrupt"
        run.mkdir(parents=True)
        (run / "summary.json").write_text("{", encoding="utf-8")
        state = readiness(self.config, self.root)
        self.assertEqual(state["previous_runs"][0]["status"], "incomplete")

    def test_missing_terminal_event_is_not_passed(self):
        run = self.root / "experiment/demo/engineering_incomplete"
        run.mkdir(parents=True)
        write_json_new(run / "summary.json", {"run_id": run.name, "status": "passed"})
        state = readiness(self.config, self.root)
        self.assertEqual(state["previous_runs"][0]["status"], "incomplete")

    def test_report_never_overwrites(self):
        path = self.root / "report.html"
        render_status(path, {})
        with self.assertRaises(FileExistsError):
            render_status(path, {})


if __name__ == "__main__":
    unittest.main()
