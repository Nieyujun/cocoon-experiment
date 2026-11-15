"""剂量审阅页结构与交互测试；只读真实候选，输出写入保留测试目录。"""
from html.parser import HTMLParser
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from cocoon.dose_build import verify_dose_build
from cocoon.reporting import render_dose_report

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "experiment/dose_candidates/dose_20261108T081317_cf1bb3"


class Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.in_script = None
        self.scripts = {}
        self.external_scripts = []
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        if tag == "script":
            if "src" in attrs:
                self.external_scripts.append(attrs["src"])
            self.in_script = attrs.get("id", "program")
            self.scripts[self.in_script] = ""
    def handle_endtag(self, tag):
        if tag == "script":
            self.in_script = None
    def handle_data(self, value):
        if self.in_script:
            self.scripts[self.in_script] += value


class DoseReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="dose-page-", dir=ROOT / "tests"))
        cls.path = render_dose_report(cls.root / "report.html", BUILD)
        cls.parser = Scripts()
        cls.parser.feed(cls.path.read_text(encoding="utf-8"))
        cls.data = json.loads(cls.parser.scripts["dose-data"])

    def test_render_contains_only_verified_full_candidate(self):
        self.assertEqual(self.data["verification"], verify_dose_build(BUILD))
        self.assertEqual(len(self.data["rows"]), 6)
        self.assertFalse(self.data["manifest"]["personalized_mirror_completed"])
        self.assertEqual(len(set(self.parser.ids)), len(self.parser.ids))
        self.assertFalse(self.parser.external_scripts)

    def test_actual_payload_lengths_and_metadata_separation(self):
        for row in self.data["rows"]:
            for arm in ("I4", "I5"):
                delivery = row["pair"][arm]
                self.assertEqual(len(delivery["visible_text"]), delivery["char_count"])
                self.assertNotIn("https://", delivery["visible_text"])
                self.assertNotIn("source_map", delivery["visible_text"])
                self.assertNotIn("个体化镜像结果", delivery["visible_text"])
                self.assertFalse(delivery["formal_eligible"])

    def test_no_overwrite(self):
        with self.assertRaises(FileExistsError):
            render_dose_report(self.path, BUILD)

    def test_offline_dom_navigation_and_exact_text(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node未安装")
        harness = r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const input=JSON.parse(fs.readFileSync(0,'utf8')),elements=new Map();
function element(id){if(elements.has(id))return elements.get(id);const item={id,textContent:'',innerHTML:'',children:[],listeners:{},classList:{toggle(){}},append(x){this.children.push(x)},addEventListener(t,f){this.listeners[t]=f}};elements.set(id,item);return item}
const doc={getElementById:element,createElement:tag=>element('created-'+tag+'-'+elements.size),querySelectorAll:q=>q==='#tabs button'?element('tabs').children:[]};
element('dose-data').textContent=JSON.stringify(input.data);
vm.runInNewContext(input.program,{document:doc},{timeout:10000});
assert.equal(element('pair-stat').textContent,'6 / 6');assert.equal(element('range-stat').textContent,'690–796');
assert.equal(element('tabs').children.length,6);assert.equal((element('length-table').innerHTML.match(/<tr>/g)||[]).length,6);
for(let i=0;i<6;i++){element('tabs').children[i].listeners.click();const row=input.data.rows[i];assert.equal(element('shared-block').textContent,row.pair.I4.evidence_block);assert.equal(element('i4-full').textContent,row.pair.I4.visible_text);assert.equal(element('i5-full').textContent,row.pair.I5.visible_text);assert.equal(element('response-prompt').textContent,row.pair.I4.response_prompt);assert.ok(element('source-list').innerHTML.includes('https://'))}
console.log('dose_dom_ok');
'''
        result = subprocess.run([node, "-e", harness], input=json.dumps({"data": self.data, "program": self.parser.scripts["program"]}),
                                capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("dose_dom_ok", result.stdout)


if __name__ == "__main__":
    unittest.main()
