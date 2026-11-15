"""把实际工程状态导出为无外部依赖的只读 HTML；不生成效果曲线。"""
from pathlib import Path
import html
import json

from .runtime import utc_now, verify_events


def render_status(path, readiness, run=None, tests=None):
    path = Path(path)
    if not path.parent.is_dir():
        raise FileNotFoundError(path.parent)
    events, contexts = [], {}
    if run:
        root = Path(run["run_directory"])
        verify_events(root)
        for line in (root / "events/events.jsonl").read_bytes().split(b"\n"):
            if not line:
                continue
            e = json.loads(line)
            events.append(e)
            if e.get("input_ref"):
                ref = e["input_ref"]
                path_ref = ref["ref"] if isinstance(ref, dict) else ref
                contexts[path_ref] = json.loads((root / path_ref).read_text(encoding="utf-8"))
    payload = {"generated_at": utc_now(), "readiness": readiness, "run": run, "tests": tests,
               "events": events, "contexts": contexts}
    data = json.dumps(payload, ensure_ascii=False, allow_nan=False).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    document = TEMPLATE.replace("__PAYLOAD__", data)
    with path.open("x", encoding="utf-8", newline="\n") as file:
        file.write(document)
    return path


def render_delivery_review(path, run_directory, semantic_path, previous_run_directory=None, tests=None):
    """展示独立短投递审计；被拒绝的原响应只出现在诊断栏，不能冒充已接受的I5。"""
    from .delivery_audit import prepare_delivery_review
    data = prepare_delivery_review(run_directory, semantic_path, previous_run_directory)
    audit, semantic = data["audit"], data["semantic_review"]
    esc = lambda value: html.escape(str(value), quote=True)
    modes = {"organized_reflection": "组织反思侧", "plain_restatement": "普通复述侧"}
    panels = []
    mode_rows = []
    for mode, name in modes.items():
        item = audit["by_mode"][mode]
        accepted = item["accepted"]
        mode_rows.append(f'<tr><td>{name}</td><td>{"已通过本阶段结构检查" if accepted else "未接纳，仅作诊断"}</td>'
                         f'<td>{item["diagnostic_additional_chars"]}</td><td>{item["diagnostic_visible_chars"]}</td>'
                         f'<td>{item["range"]["min"]}–{item["range"]["max"]}</td></tr>')
        lengths = ''.join(f'<tr><td>{row["index"]+1}</td><td>{row["text_chars"]}</td>'
                          f'<td>{row["text_min_chars"]}–{row["text_max_chars"]}</td>'
                          f'<td>{"符合" if row["within_range"] else "不足或超出"}</td></tr>' for row in item["sentence_lengths"])
        opinions = ''.join(f'<details><summary>第{row["index"]+1}句 · {"合理条件问题" if row["verdict"]=="acceptable_question" else "证据支持"}</summary>'
                           f'<p>{esc(row["reason"])}</p></details>' for row in semantic["items"] if row["mode"] == mode)
        panels.append(f'''<article class="side"><div class="side-head"><h3>{name}</h3><span class="pill {'warning' if not accepted else ''}">{'阶段通过' if accepted else '结构拒绝'}</span></div>
<p class="small">{'附加段来自本次真实模型回复，未修改。' if accepted else '下方是被拒绝的原回复逐句拼接诊断，不是有效投递产物。'}</p><pre class="raw">{esc(item["diagnostic_additional_text"])}</pre><div class="table-wrap"><table><thead><tr><th>句子</th><th>正文字符</th><th>预算</th><th>检查</th></tr></thead><tbody>{lengths}</tbody></table></div><details><summary>查看原始JSON（保留字段类型）</summary><pre class="raw">{esc(item["raw_text"])}</pre></details><details><summary>逐句AI语义意见（非人审）</summary>{opinions}</details></article>''')
    budget = audit["budget_after"]
    fixes = ''.join('<li>'+esc(text)+'</li>' for text in data['pending_fixes'])
    stats = '' if not tests else f'{tests["tests_run"]} 项全项目测试，失败 {tests["failures"]}，错误 {tests["errors"]}。'
    usage = data["reported_usage"]
    previous = data["previous_audit"]
    page = f'''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>短投递v2 · 阶段通过与剩余阻塞</title><style>
:root{{color-scheme:light}}*{{box-sizing:border-box}}body{{margin:0;background:#f5f7f1;color:#29392f;font:14px/1.8 "Microsoft YaHei",system-ui,sans-serif}}main{{max-width:1140px;margin:auto;padding:36px 26px 48px}}h1{{font-size:30px;margin:6px 0 12px;line-height:1.5}}h2{{font-size:20px;margin:0 0 18px}}h3{{font-size:16px;margin:0}}.eyebrow{{font-size:11px;letter-spacing:2px;color:#527048}}p{{margin:9px 0}}.small,.sub{{color:#76836c}}.small{{font-size:12px;overflow-wrap:anywhere}}.banner{{background:#fff2db;border-left:3px solid #b68c4a;border-radius:0 8px 8px 0;padding:16px 20px;margin:23px 0;color:#7e6136}}.stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}}.stat,.panel,.side{{background:white;border:1px solid #dde6d8;border-radius:11px}}.stat{{padding:18px 20px}}.stat strong{{display:block;font-size:27px;line-height:1.6}}.stat span{{font-size:12px;color:#728166}}.panel{{padding:23px;margin:22px 0}}.columns{{display:grid;grid-template-columns:1fr 1fr;gap:18px}}.side{{padding:21px;min-width:0}}.side-head{{display:flex;justify-content:space-between;align-items:center;gap:8px}}.pill{{padding:3px 8px;border-radius:5px;background:#e7f1df;color:#537c3f;font-size:11px;white-space:nowrap}}.pill.warning{{background:#fff1d7;color:#996b32}}table{{border-collapse:collapse;width:100%;font-size:12px}}th,td{{padding:10px 8px;text-align:left;border-bottom:1px solid #e6eddc;vertical-align:top}}th{{background:#f7f9f2;font-weight:500;color:#6c7b62}}.table-wrap{{overflow:auto}}pre.raw{{white-space:pre-wrap;overflow-wrap:anywhere;background:#f6f8f1;border-radius:7px;padding:14px;font:13px/1.9 "Microsoft YaHei",system-ui,sans-serif;max-height:390px;overflow:auto}}details{{margin:13px 0}}summary{{font-size:12px;cursor:pointer;color:#527447}}details p{{font-size:12px;color:#6b7a61}}code{{font:12px Consolas,monospace;overflow-wrap:anywhere}}.rule{{background:#f0f5e9;border-radius:8px;padding:13px 16px}}li{{margin:9px 0}}footer{{border-top:1px solid #dfe7d8;padding-top:17px;margin-top:27px;color:#829079;font-size:11px}}@media(max-width:830px){{.columns{{grid-template-columns:1fr}}.stats{{grid-template-columns:repeat(2,1fr)}}main{{padding:23px 17px}}h1{{font-size:25px}}.panel,.side{{padding:18px}}}}
</style></head><body><main><div class="eyebrow">COCOONBREAKER / DELIVERY V2</div><h1>长报告保留，短投递分开验收。</h1><p class="sub">修复冻结输入复核 · 四句预算新版本 · 一次真实试跑 · 原始结果全部保留</p><div class="banner"><b>组织反思侧通过，普通复述侧未接纳，整对尚未通过。</b> 次侧返回的schema_version是数字1.0，契约要求字符串"1.0"。提示词未明确字符串类型，这是本次提示写法的缺口；不能为此静默转换原输出。没有发给受试Agent，没有自动重试。</div>
<div class="stats"><div class="stat"><span>组织侧完整输入</span><strong>630</strong><span>173附加＋457固定，阶段结构通过</span></div><div class="stat"><span>普通侧长度诊断</span><strong>614</strong><span>仅诊断，不是已接纳消息</span></div><div class="stat"><span>本次真实调用</span><strong>{audit["actual_http_attempts"]}</strong><span>{usage["total_tokens"]} tokens，两次费用均有记录</span></div><div class="stat"><span>新增本机记账</span><strong>¥{audit["cost"]["total_cost_cny"]}</strong><span>累计¥{budget["total_cost_cny"]}，非最终账单</span></div></div>
<section class="panel"><h2>从v1到v2，改变了什么</h2><ul><li>修复了末次请求后只检查父报告的问题：现在发送前、保存原响应后和终态前都检查packet、manifest、policy、来源表及父运行。本轮检查全部通过。</li><li>原1594字符内部报告保留，最终600–800目标不变。首侧不再另外强制225–245，按实际剩余空间143–343检查；235仅为生成目标。</li><li>v1首段403字符被拒绝；v2将输出分成四句并计入32字符引用开销，首侧缩至173字符，完整材料达到630。</li><li>次侧只接收相同证据和匹配长度，不读取组织侧文本或内部报告，避免把对照变成同一份回答。</li></ul><p class="small">这些是不同版本的运行，不是无记录地重试。{esc(stats)}</p></section>
<section class="panel"><h2>为什么总长614仍不能通过</h2><div class="table-wrap"><table><thead><tr><th>侧别</th><th>状态</th><th>附加段</th><th>含证据与提示</th><th>要求的附加区间</th></tr></thead><tbody>{''.join(mode_rows)}</tbody></table></div><p>原方案同时要求结构正确与两侧长度匹配。组织侧附加173字符，所以普通侧应为158–190字符；实际诊断为157，少1字符，附加段差约10.19%，超过10%。前三句又低于32字符的分句下限。</p><p class="small">实际运行首先遇到版本字段类型错误而停止；后面这些长度问题是事后只读诊断，不能改写成原运行已经依次执行了全部检查，也不能因只差1字符就人工补字。</p></section>
<section><h2>两侧原回复及独立AI复核</h2><p class="small">8句复核：7句证据支持，1句合理条件问题，没有发现必须判为新增事实的内容；这是同屏原证据语境下的AI判断，不解除普通侧的结构和剂量问题，也不替代人审。</p><div class="columns">{''.join(panels)}</div></section>
<section class="panel"><h2>完整公共证据块没有修改</h2><details><summary>展开两侧使用的同一证据块</summary><pre class="raw">{esc(audit["evidence_block"])}</pre></details><div class="rule">原报告完整保留：是 · 冻结输入未变：是 · 自动裁剪/补字：无 · 个性化镜像：未执行 · 正式批准：否 · 人工审核：0</div></section>
<section class="panel"><h2>费用与版本记录</h2><table><tr><td>本轮输入 / 输出</td><td>{usage["prompt_tokens"]} / {usage["completion_tokens"]} tokens</td></tr><tr><td>累计已记账 / 剩余额度</td><td>¥{budget["total_cost_cny"]} / ¥{budget["available_cny"]}</td></tr><tr><td>未决预留</td><td>¥{budget["reserved_cny"]}</td></tr><tr><td>当前run</td><td><code>{esc(audit["run_id"])}</code></td></tr><tr><td>v1保留run</td><td><code>{esc(previous["run_id"] if previous else "未提供")}</code></td></tr><tr><td>新提示词SHA256</td><td><code>{esc(audit["policy_sha256"])}</code></td></tr></table><p class="small">价格按本机台账和供应商usage保守记录，缓存明细缺失按未命中。不是独立财务审计或供应商最终账单。</p></section>
<section class="panel"><h2>明确剩余工作，不在本轮继续收费抽样</h2><ol>{fixes}</ol><p class="small">后续需预先登记新版本，先用失败记录回归，再做一次明确试跑；不修改本次schema类型、不重新标记失败输出、不声称已生成完整I4或正式I5。项目接续入口为PROGRESS.md。</p></section><footer>审阅时间：{esc(data["created_at_utc"])} · 13项事件 / 27个blob已校验 · 原长报告与v1失败保留 · 本页不执行API请求</footer></main></body></html>'''
    path = Path(path)
    if not path.parent.is_dir():
        raise FileNotFoundError(path.parent)
    with path.open("x", encoding="utf-8", newline="\n") as file:
        file.write(page)
    return path


def render_extractive_review(path, alternative_path, semantic_path, run_directory, policy_path, expected_policy_sha, tests=None):
    """独立探索性备选评审，不覆盖此前真实模型对照失败，也不作为正式审批。"""
    import hashlib
    from .extractive_control import verify_extractive_alternative
    from .runtime import canonical_hash
    alternative_bytes = Path(alternative_path).read_bytes()
    data = json.loads(alternative_bytes)
    semantic = json.loads(Path(semantic_path).read_bytes())
    verify_extractive_alternative(data, run_directory, policy_path, expected_policy_sha)
    if (semantic.get("review_type") != "independent_ai_not_human"
            or semantic.get("alternative_file_sha256") != hashlib.sha256(alternative_bytes).hexdigest()
            or semantic.get("policy_sha256") != expected_policy_sha
            or semantic.get("status") != "pass_with_limitations"
            or semantic.get("human_reviews_completed") != 0 or semantic.get("formal_eligible") is not False):
        raise ValueError("摘录备选的审阅与当前版本不匹配")
    esc = lambda value: html.escape(str(value), quote=True)
    pair = data.get("pair")
    if pair is None or not data["checks"]["assembly_valid"]:
        raise ValueError("本页仅展示存在且通过结构的探索性备选")
    left, right, checks = pair["I4"], pair["I5"], data["checks"]
    rows = ''.join(f'<tr><td>{entry["combination_index"]}</td><td>{esc(entry["sentence_indices"])}</td>'
                   f'<td>{entry["additional_chars"]}</td><td>{entry["visible_chars"]}</td><td>{entry["additional_difference_ratio"]*100:.2f}%</td>'
                   f'<td>{"首个合格，已选" if entry["sentence_indices"] == data["selected_sentence_indices"] else "合格，未选" if entry["valid"] else "不合格"}</td></tr>'
                   for entry in data["candidate_log"])
    quotes = ''.join(f'<details><summary>{esc(s["evidence_id"])} · 原文偏移[{s["start"]}, {s["end"]})</summary>'
                     f'<p>{esc(s["text"])}</p><p class="small">来源：<a href="{esc(s["source_url"])}" target="_blank" rel="noopener">{esc(s["source_url"])}</a><br>来源家族：{esc(s["source_family_id"])}</p></details>'
                     for s in data["selected_sentences"])
    limitations = ''.join('<li>'+esc(v)+'</li>' for v in semantic.get("limits", []))
    test_note = "测试记录见交付清单" if not tests else f'{tests["tests_run"]} 项全项目测试通过；失败 {tests["failures"]}，错误 {tests["errors"]}。'
    page = f'''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>等证据短投递 · 整句摘录式对照备选</title><style>
:root{{color-scheme:light}}*{{box-sizing:border-box}}body{{margin:0;background:#f5f7f1;color:#28382d;font:14px/1.9 "Microsoft YaHei",system-ui,sans-serif}}main{{max-width:1100px;margin:auto;padding:34px 26px 50px}}h1{{font-size:30px;margin:7px 0;line-height:1.5}}h2{{font-size:20px;margin:0 0 16px}}h3{{font-size:16px;margin:0 0 12px}}.eyebrow{{font-size:11px;letter-spacing:2px;color:#58754c}}.sub,.small{{color:#718166}}.small{{font-size:12px;overflow-wrap:anywhere}}p{{margin:9px 0}}.notice{{margin:22px 0;padding:16px 20px;background:#fff2da;color:#816034;border-left:3px solid #b78d48;border-radius:0 8px 8px 0}}.panel,.stat,.side{{background:white;border:1px solid #dce5d4;border-radius:11px}}.panel{{padding:23px;margin:21px 0}}.stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}}.stat{{padding:18px 20px}}.stat strong{{display:block;font-size:28px;line-height:1.6}}.stat span{{font-size:12px;color:#6d805e}}.columns{{display:grid;grid-template-columns:1fr 1fr;gap:18px}}.side{{padding:20px}}.tag{{font-size:11px;padding:3px 8px;background:#eef4e5;color:#527640;border-radius:5px}}.raw{{white-space:pre-wrap;overflow-wrap:anywhere;font:14px/1.95 "Microsoft YaHei",sans-serif;margin:12px 0}}pre.raw{{padding:15px;background:#f5f8ef;border-radius:8px;max-height:500px;overflow:auto}}table{{border-collapse:collapse;width:100%;font-size:12px}}td,th{{padding:10px 8px;text-align:left;border-bottom:1px solid #e7eddc}}th{{background:#f6f9f0;font-weight:500;color:#657858}}.scroll{{overflow:auto}}summary{{cursor:pointer;font-size:12px;color:#57774b}}details{{margin:12px 0}}a{{color:#48724b}}code{{font:12px Consolas,monospace;overflow-wrap:anywhere}}li{{margin:8px 0}}footer{{margin-top:26px;border-top:1px solid #dce5d4;padding-top:16px;color:#7f9072;font-size:11px}}@media(max-width:800px){{.columns{{grid-template-columns:1fr}}.stats{{grid-template-columns:repeat(2,1fr)}}main{{padding:22px 17px}}h1{{font-size:25px}}.side,.panel{{padding:18px}}}}
</style></head><body><main><div class="eyebrow">COCOONBREAKER / EXTRACTIVE CONTROL ALTERNATIVE</div><h1>不再收费试错，普通对照直接摘录原证据。</h1><p class="sub">组织侧保留真实原回复 · 普通侧按固定规则选完整原句 · 新的离线探索性实现</p><div class="notice"><b>这个备选通过了结构与剂量检查，但不是把v3失败改成成功。</b> 原v3两模型运行仍为delivery_failed。当前普通侧换成确定性整句摘录，独立登记为{esc(data["alternative_version"])}；它不是模型生成对照，也未投入受试实验。</div>
<div class="stats"><div class="stat"><span>组织侧完整消息</span><strong>{left["char_count"]}</strong><span>{left["additional_chars"]}字符原模型附加段</span></div><div class="stat"><span>摘录对照完整消息</span><strong>{right["char_count"]}</strong><span>{right["additional_chars"]}字符原句摘录</span></div><div class="stat"><span>附加段长度差</span><strong>{checks["additional_difference_ratio"]*100:.2f}%</strong><span>原10%目标不变</span></div><div class="stat"><span>构建备选新增调用</span><strong>0</strong><span>不重新调用小米、不新增费用</span></div></div>
<section class="panel"><h2>两种结果必须分开看</h2><div class="scroll"><table><thead><tr><th>记录</th><th>结果</th><th>内容来源</th><th>实际状态</th></tr></thead><tbody><tr><td>v3真实双模型试跑</td><td>版本字段已正确；普通段155低于160，配对失败</td><td>两侧都是模型输出</td><td>原记录保留，未接纳</td></tr><tr><td>本次独立摘录备选</td><td>{left["char_count"]} / {right["char_count"]}，共同证据与回应提示一致</td><td>真实组织侧＋确定性原句对照</td><td>结构匹配，仅探索性候选</td></tr></tbody></table></div><p class="small">完整消息均在600–800；总长差{checks["total_difference_ratio"]*100:.2f}%，附加段差{checks["additional_difference_ratio"]*100:.2f}%。组织侧4句、普通侧3句的实现差异已登记，不能暗称仍是原v3四句模型对照。{esc(test_note)}</p></section>
<section><h2>完整共同证据块＋不同附加段</h2><div class="panel"><span class="tag">相同证据、相同来源</span><details><summary>展开完整共同证据块（原文未删改）</summary><pre class="raw">{esc(data["evidence_block"])}</pre></details></div><div class="columns"><article class="side"><h3>组织侧 · 已保存的模型原文</h3><span class="tag">recorded_assistant_output</span><p class="raw">{esc(left["additional_text"])}</p><details><summary>查看完整候选消息 · {left["char_count"]}字符</summary><pre class="raw">{esc(left["visible_text"])}</pre></details></article><article class="side"><h3>普通侧 · 原证据整句摘录</h3><span class="tag">deterministic_exact_source_sentences</span><p class="raw">{esc(right["additional_text"])}</p><details><summary>查看完整候选消息 · {right["char_count"]}字符</summary><pre class="raw">{esc(right["visible_text"])}</pre></details></article></div><p class="small">公共回应提示两侧相同，每轮一次。页面里的“组织侧/普通侧”及来源类型标签仅供审核，不进入上方候选消息；没有实际发送给受试Agent。</p></section>
<section class="panel"><h2>确定性选句与完整审计</h2><p>每条证据选一个完整原句，不改字、不新增结论。按原证据及句子顺序枚举，选择第一个满足长度条件的组合；不按态度、研究效果或语义得分择优。当前共{data["candidate_combination_count"]}个组合，选中原句索引{esc(data["selected_sentence_indices"])}（从0计）。</p><div class="scroll"><table><thead><tr><th>组合</th><th>原句索引</th><th>附加字符</th><th>完整字符</th><th>附加差异</th><th>选择</th></tr></thead><tbody>{rows}</tbody></table></div>{quotes}<p class="small">上面的完整链接仅供人工审核，不额外传入受试上下文。审计绑定的原失败run：<code>{esc(data["source_run_id"])}</code>。</p></section>
<section class="panel"><h2>仍需确认的边界</h2><ul><li>独立AI复核通过限定范围，但人工审核为0，不能称双人审核完成。</li><li>这是失败后构建的探索性实现，正式使用必须另行登记；不作为已完成的确认性结果。</li><li>原证据和长报告保持不变；无T1自然观点、无个性化镜像，因此不是完整I4。</li><li>其他议题和第二轮尚未用该备选逐项验证，不能外推为全实验已准备就绪。</li></ul><details><summary>展开独立AI审阅的全部限制</summary><ul>{limitations}</ul></details><p class="small">状态：{esc(data["status"])} · source_original_run_status：{esc(data["source_original_run_status"])} · formal_eligible=false · human_reviews_completed=0。</p></section><footer>本页无模型请求、无联网脚本；原始失败记录保留。算法可以重复重建本候选，不保证实验效果或真人认知改变。项目进度见PROGRESS.md。</footer></main></body></html>'''
    path = Path(path)
    if not path.parent.is_dir():
        raise FileNotFoundError(path.parent)
    with path.open("x", encoding="utf-8", newline="\n") as file:
        file.write(page)
    return path


def render_delivery_version_report(path, review: dict, tests=None) -> Path:
    """展示已审计版本的候选与失败诊断；只读核验，不重跑、不升级批准状态。"""
    import hashlib
    from collections import Counter

    def mapping(value):
        return value if isinstance(value, dict) else {}

    def esc(value):
        if value is None:
            value = "—"
        elif type(value) is bool:
            value = str(value).lower()
        return html.escape(str(value), quote=True)

    def raw(value):
        return esc(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)) if value is not None else esc(None)

    def table(headers, rows, caption=None):
        head = ''.join(f'<th scope="col">{esc(v)}</th>' for v in headers)
        body = ''.join('<tr>' + ''.join(f'<td>{esc(v)}</td>' for v in row) + '</tr>' for row in rows)
        if not body:
            body = '<tr>' + '<td>—</td>' * len(headers) + '</tr>'
        title = f'<caption>{esc(caption)}</caption>' if caption else ''
        return f'<div class="table-wrap"><table>{title}<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'

    def details(title, value):
        return f'<details><summary>{esc(title)}</summary><pre>{raw(value)}</pre></details>'

    def numbered(index):
        return index + 1 if type(index) is int else None

    def difference(key):
        values = [mapping(by_mode.get(mode)).get(key) for mode in names]
        if not all(type(v) is int and v >= 0 for v in values) or min(values) == 0:
            return None
        delta, denominator = max(values) - min(values), min(values)
        return f'{delta} / {denominator} ≈ {100 * delta / denominator:.2f}%'

    path = Path(path)
    if not path.parent.is_dir():
        raise FileNotFoundError(path.parent)
    if path.exists():
        raise FileExistsError(path)
    audit = mapping(review.get("audit"))
    runpath = audit.get("runpath") or audit.get("run_directory")
    if not runpath or audit.get("audit_status") != "passed":
        raise ValueError("delivery_report:audit_required")
    root = Path(runpath).resolve(strict=True)
    summary_bytes = (root / "summary.json").read_bytes()
    digest = hashlib.sha256(summary_bytes).hexdigest()
    if digest != audit.get("summary_sha256"):
        raise ValueError("delivery_report:summary_sha256_mismatch")
    summary = json.loads(summary_bytes)
    if not audit.get("run_id") or audit["run_id"] != summary.get("run_id") or root.name != audit["run_id"]:
        raise ValueError("delivery_report:run_identity_mismatch")
    verification = verify_events(root)
    if verification != audit.get("verification") or verification != summary.get("verification"):
        raise ValueError("delivery_report:verification_mismatch")
    if audit.get("events_sha256") is not None and hashlib.sha256(
            (root / "events/events.jsonl").read_bytes()).hexdigest() != audit["events_sha256"]:
        raise ValueError("delivery_report:events_sha256_mismatch")
    for record in (review, audit, summary):
        if (record.get("formal_eligible") is not False
                or type(record.get("human_reviews_completed")) is not int or record["human_reviews_completed"] != 0):
            raise ValueError("delivery_report:non_human_candidate_only")
    for record, fields in ((audit, ("full_I4_eligible", "subject_delivered", "received_actual_subject")),
                           (summary, ("full_I4_eligible", "received_actual_subject", "natural_view_used", "mirror_executed"))):
        if any(record.get(key) is not False for key in fields):
            raise ValueError("delivery_report:undelivered_without_T1_only")

    semantic = mapping(review.get("semantic_review"))
    if semantic and (semantic.get("review_type") != "independent_ai_not_human"
                     or semantic.get("run_id") != audit["run_id"]
                     or semantic.get("summary_sha256") != digest
                     or type(semantic.get("human_reviews_completed")) is not int
                     or semantic["human_reviews_completed"] != 0):
        raise ValueError("delivery_report:semantic_identity_mismatch")
    items = semantic.get("items") or []
    counts = Counter(item.get("verdict") for item in items)
    names = {"organized_reflection": "组织反思侧", "plain_restatement": "普通复述侧"}
    verdicts = {"supported": "证据支持", "acceptable_question": "合理条件问题", "overstated": "过度断言",
                "unsupported": "缺乏证据支持", "needs_review": "需进一步核查"}
    by_mode = mapping(audit.get("by_mode"))
    additions = mapping(summary.get("additions"))
    mode_rows, panels, schema_rows, sentence_counts = [], [], [], []
    for mode in dict.fromkeys([*names, *by_mode]):
        row = mapping(by_mode.get(mode))
        recorded = mapping(additions.get(mode))
        label = names.get(mode, mode)
        accepted = row.get("accepted")
        status = ("未尝试" if row.get("status") == "not_attempted" else
                  "已通过本阶段结构检查（仅候选）" if accepted is True else
                  "未接纳，仅作诊断" if accepted is False else None)
        bounds = mapping(row.get("range"))
        accepted_chars = mapping(recorded.get("rendered")).get("char_count") if accepted is True else None
        mode_rows.append((label, recorded.get("stage"), status, row.get("status"), accepted_chars,
                          row.get("diagnostic_additional_chars"), audit.get("fixed_chars"),
                          row.get("diagnostic_visible_chars"), bounds.get("min"), bounds.get("max")))
        parsed = mapping(row.get("parsed_output"))
        if isinstance(parsed.get("sentences"), list):
            sentence_counts.append(len(parsed["sentences"]))
        schema = parsed.get("schema_version")
        present = "schema_version" in parsed
        schema_rows.append((label, json.dumps(schema, ensure_ascii=False) if present else None,
                            type(schema).__name__ if present else None,
                            ("正确" if type(schema) is str else "不正确") if present else None))
        lengths = [(numbered(s.get("index")), s.get("text_chars"), s.get("citation_chars"),
                    s.get("rendered_chars"), s.get("text_min_chars"), s.get("text_max_chars"),
                    s.get("target_text_chars"), s.get("within_range")) for s in (row.get("sentence_lengths") or [])]
        opinions = []
        for item in items:
            if item.get("mode") != mode:
                continue
            opinions.append(f'<details><summary>第{esc(numbered(item.get("index")))}句 · '
                            f'{esc(verdicts.get(item.get("verdict"), item.get("verdict")))}</summary>'
                            f'<p>原句：{esc(item.get("original_text"))}</p><p>AI意见：{esc(item.get("reason"))}</p>'
                            f'<pre>引用：{raw(item.get("citations"))}\nkind：{esc(item.get("kind"))}</pre></details>')
        panels.append(f'<article class="panel"><h3>{esc(label)} · {esc(status)}</h3>'
                      '<p class="muted">原回复逐句拼接诊断；阶段接纳不等于配对接纳，更不等于已投递。</p>'
                      f'<pre>{esc(row.get("diagnostic_additional_text"))}</pre>'
                      + table(("句子", "正文字符", "引用字符", "拼接字符", "下限", "上限", "目标", "符合区间"), lengths)
                      + details("分句预算原记录", row.get("budget"))
                      + details("只读诊断 violations（不是原运行的检查执行顺序）", row.get("violations"))
                      + f'<details><summary>原始 JSON 文本（未改写）</summary><pre>{esc(row.get("raw_text"))}</pre></details>'
                      + details("parsed_output（保留字段类型）", row.get("parsed_output"))
                      + details("原请求 / 响应引用（仅作数据展示）", {"request": recorded.get("request"), "response": recorded.get("response")})
                      + '<h4>逐句 AI 意见 · 非人审</h4>' + (''.join(opinions) or '<p>—（未提供逐句意见）</p>')
                      + '</article>')

    pair = audit.get("pair_available")
    overall = "整对尚未通过" if pair is False else "配对结构通过，仅构建候选" if pair is True else "配对状态：—"
    error = mapping(audit.get("error"))
    violation_lists = [mapping(row).get("violations") for row in by_mode.values()]
    type_violations = (sum(v.get("code") == "schema_version_type" for values in violation_lists for v in values)
                       if violation_lists and all(isinstance(values, list) for values in violation_lists) else None)
    facts = table(("核验项目", "实际记录"), [
        ("policy version", audit.get("policy_version")), ("运行汇总状态", audit.get("run_status")),
        ("审阅汇总状态（不授予资格）", review.get("status")), ("本地记录审计", audit.get("audit_status")),
        ("配对可用", pair), ("已验证阶段数", audit.get("stage_count")),
        ("HTTP 尝试", audit.get("actual_http_attempts")), ("已完成调用", audit.get("completed_model_calls")),
        ("冻结输入一致", audit.get("inputs_match")), ("原报告完整保留", audit.get("internal_report_preserved")),
        ("原报告渲染字符", mapping(summary.get("original_internal_report")).get("rendered_chars")),
        ("formal_eligible", audit.get("formal_eligible")), ("human_reviews_completed（人工审核）", audit.get("human_reviews_completed")),
        ("full_I4_eligible（完整 I4）", audit.get("full_I4_eligible")), ("subject_delivered", audit.get("subject_delivered")),
        ("received_actual_subject", audit.get("received_actual_subject")), ("natural_view_used（T1）", summary.get("natural_view_used")),
        ("mirror_executed", summary.get("mirror_executed"))])
    fixed = audit.get("fixed_chars")
    allowed = mapping(summary.get("allowed_additional_char_range"))
    total_limits = {key: fixed + allowed[key] if type(fixed) is int and type(allowed.get(key)) is int else None
                    for key in ("min", "max")}
    length_table = table(("侧别", "阶段", "接纳范围", "阶段状态", "已接纳附加字符", "诊断附加字符", "固定字符",
                          "诊断总长", "附加下限", "附加上限"), mode_rows,
                         "仅构建候选，未发送给 Subject；总长来自 diagnostic_visible_chars，失败侧不是已投递长度。")
    schema_table = table(("侧别", "raw parsed schema_version", "实际类型", "是否为字符串类型"), schema_rows)
    reviewed = len(items) if isinstance(semantic.get("items"), list) else None
    semantic_table = table(("AI 复核项目", "实际条数"), [
        ("已提供逐句意见", reviewed), ("原回复句数", sum(sentence_counts) if sentence_counts else None),
        *[(label, counts[verdict] if items else None) for verdict, label in verdicts.items()]])
    budget, cost, usage = mapping(audit.get("budget_after")), mapping(audit.get("cost")), mapping(review.get("reported_usage"))
    cost_table = table(("费用 / 用量", "原记录"), [
        ("本轮输入 tokens", usage.get("prompt_tokens")), ("本轮输出 tokens", usage.get("completion_tokens")),
        ("本轮合计 tokens", usage.get("total_tokens")), ("本轮记账（CNY）", cost.get("total_cost_cny")),
        ("累计记账（CNY）", budget.get("total_cost_cny")), ("预算上限（CNY）", budget.get("limit_cny")),
        ("剩余额度（CNY）", budget.get("available_cny")), ("未决预留（CNY）", budget.get("reserved_cny")),
        ("计费口径", cost.get("basis"))])
    per_mode_usage = table(("侧别", "输入 tokens", "输出 tokens", "合计 tokens"), [
        (names.get(mode, mode), *(mapping(mapping(row).get("usage")).get(key)
                                for key in ("prompt_tokens", "completion_tokens", "total_tokens")))
        for mode, row in by_mode.items()])
    test_result = mapping(tests)
    test_table = table(("自动测试记录（仅调用方提供的范围）", "数量 / 状态"), [
        ("执行数", test_result.get("tests_run")), ("失败", test_result.get("failures")),
        ("错误", test_result.get("errors")), ("跳过", test_result.get("skipped")), ("success", test_result.get("success"))])
    source_inputs = mapping(audit.get("source_inputs"))
    identity = table(("追溯字段", "原记录（不是可执行链接）"), [
        ("run", audit.get("run_id")), ("运行目录", str(root)), ("summary SHA256", digest),
        ("policy SHA256", audit.get("policy_sha256")), ("semantic SHA256", review.get("semantic_file_sha256")),
        ("semantic 路径", review.get("semantic_source_path")), ("原报告父运行", source_inputs.get("parent_source_path")),
        ("父运行 summary SHA256", source_inputs.get("parent_summary_sha256"))])
    page = f'''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>短投递审阅 · {esc(audit.get("policy_version"))}</title><style>
:root{{color-scheme:light}}*{{box-sizing:border-box}}body{{margin:0;background:#f5f7f4;color:#29382f;font:14px/1.8 "Microsoft YaHei",system-ui,sans-serif}}
main{{max-width:1180px;margin:auto;padding:30px 22px 48px}}h1{{font-size:28px}}h2{{font-size:20px}}h3{{font-size:17px}}h4{{font-size:15px}}
.panel{{background:#fff;border:1px solid #dce5dc;border-radius:10px;padding:22px;margin:18px 0;min-width:0}}
.notice{{background:#fff2dc;border-left:3px solid #b48a46;padding:16px 20px}}.muted,footer,caption{{color:#617066}}
.columns{{display:grid;grid-template-columns:1fr 1fr;gap:18px}}.table-wrap{{overflow:auto}}table{{border-collapse:collapse;width:100%;font-size:13px}}
th,td{{padding:9px 8px;text-align:left;border-bottom:1px solid #e4ebe2;vertical-align:top;overflow-wrap:anywhere}}th{{background:#f2f6ef}}caption{{text-align:left;padding:8px 0}}
pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f7f1;border-radius:7px;padding:14px;font:13px/1.85 Consolas,"Microsoft YaHei",monospace;max-height:420px;overflow:auto}}
details{{margin:12px 0}}summary{{cursor:pointer;color:#3b6348}}p,summary{{overflow-wrap:anywhere}}footer{{margin-top:24px;font-size:12px}}
@media(max-width:850px){{.columns{{grid-template-columns:1fr}}main{{padding:20px 14px}}.panel{{padding:16px}}}}
</style></head><body><main>
<h1>短投递版本审阅 · {esc(audit.get("policy_version"))}</h1>
<div class="notice"><b>{overall}</b><p>仅构建候选，未发送给 Subject。AI 意见不是人工批准；无 T1 自然观点、未执行个体化镜像，不是完整 I4。</p>
<p>实际首错：<b>{esc(error.get("code"))}</b> · 阶段：{esc(audit.get("failed_stage"))}</p>{details("首错详情（audit.error 原记录）", audit.get("error"))}</div>
<section class="panel"><h2>版本与汇总状态</h2>{facts}</section>
<section class="panel"><h2>阶段接纳与长度诊断</h2>{length_table}
<p>最终候选总长下限 / 上限：{esc(total_limits["min"])} / {esc(total_limits["max"])}（固定字符＋汇总中允许的附加区间）。</p>
<p>附加段差异（差值 / 较短侧）：{esc(difference("diagnostic_additional_chars"))}</p>
<p>完整候选诊断差异（差值 / 较短侧）：{esc(difference("diagnostic_visible_chars"))}</p>
<p class="muted">长度符合不豁免结构或逐句限制；诊断不改写原运行首错，不补字、不裁剪、不自动重试。</p></section>
<section class="panel"><h2>两侧 schema 类型</h2>{schema_table}<p>schema_version_type 诊断条数：{esc(type_violations)}。类型检查不等于整个 schema 或配对通过。</p></section>
<section class="panel"><h2>逐句 AI 复核概况 · 非人审</h2>{semantic_table}
<p>语义审阅状态：{esc(semantic.get("status") if items else None)}。未提供的意见不视为通过，任何 AI 意见都不解除结构阻塞。</p>
{details("语义审阅适用限制", semantic.get("limits"))}</section>
<div class="columns">{''.join(panels)}</div>
<section class="panel"><h2>共同证据与原报告保留</h2><details><summary>完整证据块（原样展示）</summary><pre>{esc(audit.get("evidence_block"))}</pre></details>
{details("短引用来源映射（仅数据，不访问链接）", audit.get("source_map"))}
{details("原报告保留快照", summary.get("original_internal_report"))}
{details("冻结输入与原报告引用", audit.get("source_inputs"))}</section>
<section class="panel"><h2>费用与预算快照</h2>{cost_table}{per_mode_usage}<p class="muted">仅已保存的本机记账与供应商 usage，不是最终账单。此页不读取当前账本、不请求网络、不重新跑模型。</p></section>
<section class="panel"><h2>追溯与测试记录</h2>{identity}{test_table}<p class="muted">未提供的测试字段均为 —，不推断全项目测试通过。</p></section>
<footer>审阅生成时间：{esc(review.get("created_at_utc"))} · 本次核验 {esc(verification.get("event_count"))} 项事件 / {esc(verification.get("blob_count"))} 个 blob · 原报告和旧版报告保留 · 正式批准：false · 人工审核：0</footer>
</main></body></html>'''
    if (root / "summary.json").read_bytes() != summary_bytes:
        raise ValueError("delivery_report:summary_changed_during_render")
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(page)
    return path


def render_live_review(path, review, tests=None):
    """展示已绑定原始日志的真实输出和AI意见，不把成功HTTP当成干预验收通过。"""
    import hashlib
    from .live_audit import prepare_live_review
    from .dose_build import source_index_from_review
    run = review["audit"]
    if review.get("status") != "not_ready_for_intervention" or review.get("formal_eligible") is not False:
        raise ValueError("不能把待审真实输出展示为正式干预通过")
    root = Path(run["run_directory"])
    if hashlib.sha256((root / "summary.json").read_bytes()).hexdigest() != run["summary_sha256"]:
        raise ValueError("原运行已变动")
    verify_events(root)
    esc = lambda value: html.escape(str(value), quote=True)
    stages = {"search": "冻结证据梳理", "label": "信源标注", "blindspot": "盲区分析", "integrate": "报告整合"}
    verdict_names = {"supported": "证据支持", "acceptable_gap_or_question": "合理缺口／问题",
                     "unsupported_fact": "缺乏支持的事实", "overstated": "过度断言", "needs_review": "需进一步核查"}
    semantic = review["semantic_review"]
    section_names = {"evidence_overview": "证据概览", "coverage_gaps": "覆盖缺口", "source_assessment": "信源判断",
                     "missing_dimensions": "缺失维度", "possible_biases": "可能偏差", "counterpoints": "其他侧重点",
                     "consensus": "共识", "disagreements": "分歧", "blindspots": "盲区", "sources": "来源"}
    stage_rows, cards = [], []
    for stage, label in stages.items():
        row = run["stage_results"][stage]
        usage = row["usage"] or {}
        items = [item for item in semantic["items"] if item["stage"] == stage]
        problem_count = sum(item["verdict"] not in ("supported", "acceptable_gap_or_question") for item in items)
        stage_rows.append(f'<tr><td>{label}</td><td>{"通过" if row["structure_passed"] else "未完成"}</td>'
                          f'<td>{len(items)}</td><td>{problem_count}</td><td>{usage.get("prompt_tokens", "未知")}</td>'
                          f'<td>{usage.get("completion_tokens", "未知")}</td><td>{round(row["metadata"].get("latency_ms", 0)/1000, 2)}秒</td></tr>')
        for item in items:
            problem = item["verdict"] not in ("supported", "acceptable_gap_or_question")
            item_id = stage + "/" + item["section"] + "/" + str(item["item_index"])
            cards.append(f'''<article class="item {'problem' if problem else 'ok'}" data-stage="{stage}" data-problem="{'yes' if problem else 'no'}"><div class="item-head"><span>{label} · {esc(section_names.get(item["section"], item["section"]))} · 第{item["item_index"]+1}项</span><span class="verdict {'flag' if problem else ''}">{verdict_names[item["verdict"]]}</span></div><div class="model-label">小米原输出 · 未修改</div><p class="original">{esc(item["text"])}</p><div class="cite">原引用：{esc('、'.join(item['citations']) or '无')} · 原标记：{esc(item["epistemic_status"])}</div><div class="review-reason"><b>AI复核意见：</b>{esc(item["reason"])}</div><div class="small">定位：{esc(item_id)} · 非人工最终裁决</div></article>''')
    counts = semantic["counts"]
    budget = review["billing"]
    current = budget["cumulative"]
    fixes = ''.join(f'<li>{esc(line)}</li>' for line in review["next_changes_not_executed"])
    cost_rows = ''.join(f'<tr><td>{esc(k)}</td><td>{esc(v)}</td></tr>' for k,v in [
        ("本轮四次调用保守记账", "¥"+str(budget["run_cost_cny"])),
        ("含此前连通性测试的累计", "¥"+current["total_cost_cny"]),
        ("累计¥5额度剩余", "¥"+current["available_cny"]),
        ("未决预留", "¥"+current["reserved_cny"]),
        ("计费依据", budget["basis"])])
    data_summary = f'{tests["tests_run"]} 项自动测试通过，失败 {tests["failures"]}，错误 {tests["errors"]}' if tests else '测试结果见随附验收记录'
    page = f'''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>真实助手试跑 · 偏差与费用审阅</title><style>
:root{{color-scheme:light}}*{{box-sizing:border-box}}body{{margin:0;background:#f5f6f2;color:#2d392f;font:14px/1.85 "Microsoft YaHei",system-ui,sans-serif}}main{{max-width:1120px;margin:auto;padding:34px 26px 50px}}h1{{font-size:31px;line-height:1.5;margin:8px 0}}h2{{font-size:20px;margin:0 0 17px}}p{{margin:10px 0}}.eyebrow{{font-size:11px;letter-spacing:2px;color:#57704b;font-weight:600}}.small,.sub{{color:#75816e}}.small{{font-size:11px;overflow-wrap:anywhere}}.banner{{background:#fff0da;border-left:3px solid #b48048;padding:17px 20px;border-radius:0 8px 8px 0;margin:23px 0;color:#7a5b33}}.stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}}.stat,.panel,.item{{border:1px solid #dce4d5;background:white;border-radius:11px}}.stat{{padding:18px 20px}}.stat strong{{display:block;font-size:28px;line-height:1.6}}.stat span{{font-size:12px;color:#748066}}.panel{{padding:23px;margin:22px 0}}.table-wrap{{overflow:auto}}table{{border-collapse:collapse;width:100%;font-size:12px}}td,th{{padding:10px 8px;border-bottom:1px solid #e7ecdf;text-align:left;vertical-align:top}}th{{background:#f6f8f1;font-weight:500;color:#6c7a61}}.split{{display:grid;grid-template-columns:1fr 1fr;gap:23px}}.warning{{color:#9c6731}}.badge{{font-size:12px;padding:4px 9px;background:#edf3e6;border-radius:6px;color:#597343}}button{{border:1px solid #d0dac7;border-radius:7px;padding:8px 12px;background:white;color:#556e44;cursor:pointer;font:12px "Microsoft YaHei",sans-serif}}button.active{{background:#e7efdc;border-color:#aac391}}.bar{{display:flex;gap:9px;flex-wrap:wrap;align-items:center;margin:14px 0}}.bar label{{font-size:12px;color:#667357;margin-left:auto}}.item{{padding:21px;margin:14px 0}}.item.problem{{border-left:3px solid #be9362}}.item-head{{display:flex;justify-content:space-between;align-items:center;gap:12px;color:#6f7d60;font-size:12px}}.verdict{{background:#edf4e5;border-radius:5px;padding:3px 8px;white-space:nowrap;color:#526e3a}}.verdict.flag{{background:#fff0db;color:#996429}}.model-label{{font-size:11px;letter-spacing:1px;color:#8a957d;margin-top:14px}}.original{{font-size:14px;line-height:1.9;white-space:pre-wrap;overflow-wrap:anywhere}}.cite{{font-size:11px;color:#778768;margin-bottom:12px}}.review-reason{{background:#f6f7f1;border-radius:7px;padding:13px 15px;font-size:12px;color:#576647;margin-bottom:10px}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.85 "Microsoft YaHei",sans-serif;padding:16px;background:#f7f9f3;border-radius:8px;max-height:520px;overflow:auto}}summary{{cursor:pointer;color:#607c4a;font-size:12px}}li{{margin:9px 0}}footer{{font-size:11px;color:#8a947e;border-top:1px solid #dce4d5;padding-top:16px;margin-top:27px}}[hidden]{{display:none!important}}@media(max-width:800px){{.stats{{grid-template-columns:repeat(2,1fr)}}.split{{grid-template-columns:1fr}}main{{padding:24px 16px}}h1{{font-size:25px}}.panel,.item{{padding:17px}}}}
</style></head><body><main><div class="eyebrow">COCOONBREAKER / FIRST LIVE ASSISTANT RUN</div><h1>真实四阶段跑通，但干预验收未通过。</h1><p class="sub">碳与气候首轮 · {esc(run["model_settings"]["model"])} · 冻结证据3条 · 无重试、无模拟回退</p><div class="banner"><b>结构成功 ≠ 语义和剂量合格。</b> 实际模型完成四阶段，原输出完整保留。AI复核标出 {review["problem_items"]} 项语义问题；候选投递文本 {run["candidate_visible_chars"]} 字符，超过800上限。本次没有T1观点、镜像、I5或受试Agent运行。</div>
<div class="stats"><div class="stat"><span>实际模型调用</span><strong>{run["actual_http_attempts"]}</strong><span>四阶段结构均通过</span></div><div class="stat"><span>输入＋输出 tokens</span><strong>{run["known_reported_usage"]["total_tokens"]:,}</strong><span>{run["known_reported_usage"]["prompt_tokens"]:,} 输入 / {run["known_reported_usage"]["completion_tokens"]:,} 输出</span></div><div class="stat"><span>完整候选文本</span><strong>{run["candidate_visible_chars"]}</strong><span>实际计字，未截断到800</span></div><div class="stat"><span>AI标出问题项</span><strong>{review["problem_items"]} / {semantic["items_reviewed"]}</strong><span>逐项意见，待人工裁决</span></div></div>
<section class="panel"><h2>阶段与原始用量</h2><div class="table-wrap"><table><thead><tr><th>阶段</th><th>结构</th><th>输出项</th><th>AI问题项</th><th>输入tokens</th><th>输出tokens</th><th>调用耗时</th></tr></thead><tbody>{''.join(stage_rows)}</tbody></table></div><p class="small">按原始HTTP响应、请求ID、冻结提示词与阶段事件重新校验；结构检查不能证明引用在语义上充分。没有凭日志里的“成功”直接认定合格。</p></section>
<section class="panel"><div class="split"><div><h2>这次主要发现</h2><ul><li><b>来源能力被补充推断：</b>仅凭机构名称、域名和说明段落，就评价权威、可信或倡导立场，超过冻结包所给依据。</li><li><b>事实与偏好混在一起：</b>机制说明被归入行动立场；没有自然观点，也推断用户的采信倾向。</li><li><b>语气被强化：</b>“不能仅据此推断持续、等比例增加”被写成“不能等比例兑现”。</li><li><b>真实输出过长：</b>整合报告 {run["report_chars"]} 字符，加证据与提示后 {run["candidate_visible_chars"]} 字符；不能用上一轮手工短稿假装替代成功。</li></ul></div><div><h2>费用与预算</h2><table>{cost_rows}</table><p class="small">供应商响应未给出可用缓存明细，因此按未缓存输入保守计算。本次四阶段全部已记账，无未决预留；审计未再调用模型，密钥未写入项目配置或导出文件。</p></div></div></section>
<section><h2>逐条原输出与复核意见</h2><div class="bar"><button class="stage-filter active" data-stage="all">全部39项</button><button class="stage-filter" data-stage="search">证据梳理</button><button class="stage-filter" data-stage="label">信源标注</button><button class="stage-filter" data-stage="blindspot">盲区分析</button><button class="stage-filter" data-stage="integrate">报告整合</button><label><input type="checkbox" id="issues-only" checked> 仅显示AI问题项</label></div><p class="small" id="visible-count"></p><div id="items">{''.join(cards)}</div></section>
<section class="panel"><h2>长度诊断与完整候选消息</h2><p>共同证据块 {run["evidence_chars"]} 字符 ＋ 整合报告 {run["report_chars"]} 字符 ＋ 标签及回应提示39字符 ＝ <b class="warning">{run["candidate_visible_chars"]} 字符</b>。</p><p class="small">候选仅用于诊断，没有发给受试Agent。保留全部模型条目及引用标记，不删去不利内容，也不通过重复请求筛选更好结果。</p><details><summary>展开完整模型整合报告</summary><pre>{esc(run["rendered_report"])}</pre></details><details><summary>展开包含证据、报告和回应提示的完整候选消息</summary><pre>{esc(run["candidate_visible_text"])}</pre></details></section>
<section class="panel"><h2>下一版需要改什么（尚未执行）</h2><ol>{fixes}</ol><p class="small">任何新提示词或摘要契约都要升版，保留本次失败于语义／剂量验收的记录；人审仍为0，来源审核、正式I4和全量实验资格均未开启。</p><p><span class="badge">{esc(data_summary)}</span></p><p class="small">运行目录：{esc(run["run_directory"])}<br>summary SHA256：{esc(run["summary_sha256"])}<br>事件链：{run["verification"]["event_count"]}项事件 / {run["verification"]["blob_count"]}项原始blob，完整性通过。</p></section><footer>审核生成：{esc(review["created_at_utc"])} · AI审核，不是双人人工批准 · 四阶段调用成功；正式干预不合格 · 所有原始记录保留</footer></main>
<script>'use strict';let stage='all';const items=[...document.querySelectorAll('.item')],issues=document.getElementById('issues-only');function update(){{let n=0;for(const e of items){{const visible=(stage==='all'||e.dataset.stage===stage)&&(!issues.checked||e.dataset.problem==='yes');e.hidden=!visible;if(visible)n++}}document.getElementById('visible-count').textContent='当前显示 '+n+' 项；复核结论仅适用于本次冻结证据和原输出。'}}document.querySelectorAll('.stage-filter').forEach(b=>b.addEventListener('click',()=>{{stage=b.dataset.stage;document.querySelectorAll('.stage-filter').forEach(x=>x.classList.toggle('active',x===b));update()}}));issues.addEventListener('change',update);update();</script></body></html>'''
    path = Path(path)
    if not path.parent.is_dir():
        raise FileNotFoundError(path.parent)
    with path.open("x", encoding="utf-8", newline="\n") as file:
        file.write(page)
    return path


def render_dose_report(path, build_directory, tests=None):
    """只读评审视图，展示摘要及真正待发送消息；不把评审链接混入受试上下文。"""
    from .dose_build import verify_dose_build, read_json
    root = Path(build_directory)
    verification = verify_dose_build(root)
    manifest = read_json(root / "manifest.json")
    rows = [{"summary": row, "pair": read_json(root / row["pair_file"])} for row in manifest["pairs"]]
    data = json.dumps({"manifest": manifest, "verification": verification, "rows": rows, "tests": tests},
                      ensure_ascii=False, allow_nan=False).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    page = DOSE_TEMPLATE.replace("__DOSE_DATA__", data)
    path = Path(path)
    if not path.parent.is_dir():
        raise FileNotFoundError(path.parent)
    with path.open("x", encoding="utf-8", newline="\n") as file:
        file.write(page)
    return path


DOSE_TEMPLATE = r'''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>破茧助手 · 等证据剂量对照</title><style>
:root{color-scheme:light;--ink:#26372d;--muted:#6d7c70;--line:#dce5db;--accent:#396d4c}*{box-sizing:border-box}body{margin:0;background:#f4f6f0;color:var(--ink);font:14px/1.8 "Microsoft YaHei",system-ui,sans-serif}main{max-width:1200px;margin:auto;padding:36px 30px 50px}h1{font-size:31px;line-height:1.4;margin:7px 0 12px}h2{font-size:19px;margin:0 0 15px}h3{font-size:15px;margin:0 0 10px}p{margin:8px 0}.eyebrow{font-size:11px;letter-spacing:2px;color:var(--accent);font-weight:600}.sub,.small{color:var(--muted)}.small{font-size:12px}.top{display:flex;justify-content:space-between;align-items:center;gap:22px}.badge{padding:5px 12px;border:1px solid #cbdac7;border-radius:16px;white-space:nowrap;color:#59794b;font-size:12px}.notice{background:#fff2d7;border-left:3px solid #ba9652;color:#776032;padding:15px 20px;margin:22px 0;border-radius:0 8px 8px 0}.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}.panel,.stat{border:1px solid var(--line);background:white;border-radius:12px}.panel{padding:23px;margin-top:21px}.stat{padding:18px 21px}.stat strong{display:block;font-size:29px;line-height:1.55;font-weight:650}.stat span{font-size:12px;color:var(--muted)}table{width:100%;border-collapse:collapse;font-size:12px}td,th{text-align:left;padding:11px 9px;border-bottom:1px solid #e8ede2;vertical-align:top}th{font-weight:500;color:#6f7f68;background:#f6f9f1}.table-wrap{overflow:auto}.tabs{display:flex;gap:8px;flex-wrap:wrap;margin:18px 0}button{font:12px "Microsoft YaHei",sans-serif;padding:9px 13px;background:white;border:1px solid #ccdaca;border-radius:7px;color:#4e644c;cursor:pointer}button.active{background:#e6f0e0;color:#2f603a;border-color:#9ebc94}.selection{border-left:3px solid #6b9770;padding-left:14px;margin:15px 0}.shared{background:#f4f8ef;border:1px solid #d8e4d1;border-radius:10px;padding:20px}.shared-head{display:flex;justify-content:space-between;gap:15px;margin-bottom:12px}.pill{font-size:11px;background:#e3efd9;color:#476d3b;padding:3px 8px;border-radius:5px;white-space:nowrap}.raw{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.95 "Microsoft YaHei",system-ui,sans-serif;margin:0}.columns{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-top:18px}.addon{padding:20px;border:1px solid var(--line);border-radius:10px;background:#fff}.addon h3{display:flex;justify-content:space-between;gap:10px}.metrics{font-size:11px;color:#638151;margin-top:12px;padding-top:10px;border-top:1px solid #e6eddf}.prompt{margin-top:17px;padding:12px 16px;border:1px dashed #b5cbae;border-radius:8px;color:#5b7052}.facts{display:grid;grid-template-columns:1fr 1fr;gap:22px}.fact-number{font-size:12px;display:inline-block;padding:1px 7px;background:#edf4e6;border-radius:5px;margin-right:7px}details{margin-top:13px}summary{cursor:pointer;color:#487148;font-size:12px}details pre{padding:15px;background:#f6f8f2;border-radius:8px;max-height:400px;overflow:auto}.source-row{display:flex;gap:12px;font-size:12px;border-bottom:1px solid #e5ebdf;padding:8px 0;overflow-wrap:anywhere}.source-row a{color:#3c7654}code{font:12px Consolas,monospace;overflow-wrap:anywhere}li{margin-bottom:10px}footer{margin-top:28px;border-top:1px solid var(--line);padding-top:16px;color:#84917b;font-size:11px}.pass{color:#467444}.warn{color:#97712f}
@media(max-width:850px){.columns,.facts{grid-template-columns:1fr}.stats{grid-template-columns:repeat(2,1fr)}main{padding:24px 17px}.top{align-items:start}h1{font-size:25px}.panel{padding:18px}.source-row{display:block}}
</style></head><body><main><div class="top"><div><div class="eyebrow">COCOONBREAKER / MATCHED DOSE DESIGN</div><h1>相同证据，不同组织方式。</h1><p class="sub">三议题 · 两轮 · 十二份完整可见稿 · 不删证据、不隐藏额外输入</p></div><span class="badge">显示版本 source_id_inline_v1</span></div>
<div class="notice"><b>这是编辑设计稿，不是已运行的完整 I4。</b> 本轮只完成等证据显示和剂量匹配，没有 T1 自然观点、个体化镜像或小米模型新调用。AI语义复核不是双人人工批准，全部稿件仍为待审。</div>
<div class="stats"><div class="stat"><span>配对装配校验</span><strong id="pair-stat">—</strong><span>六轮证据、来源与回应机会相同</span></div><div class="stat"><span>完整可见稿长度</span><strong id="range-stat">—</strong><span>目标600–800，包含全部消息字符</span></div><div class="stat"><span>最大附加段长度差</span><strong id="diff-stat">—</strong><span>总长和附加段分别要求≤10%</span></div><div class="stat"><span>本轮收费模型调用</span><strong>0</strong><span>不把编辑稿当作模型输出</span></div></div>
<section class="panel"><h2>逐轮匹配结果</h2><div class="table-wrap"><table><thead><tr><th>议题 / 轮次</th><th>旧证据块</th><th>新证据块</th><th>I4侧完整稿</th><th>I5侧完整稿</th><th>附加段差异</th><th>装配</th></tr></thead><tbody id="length-table"></tbody></table></div><p class="small">旧列计正文＋完整URL＋家族ID；新列计原样正文＋来源编号＋机构短名＋文献日期。不是把同一条旧消息只在页面上折叠。英文、标点、换行及公共回应提示均按实际消息的 Unicode 码点计入，不等于 token 数或认知负担。</p></section>
<section class="panel"><h2>双栏原文对照</h2><div class="tabs" id="tabs"></div><div class="selection"><h3 id="selected-title"></h3><p class="small" id="selected-summary"></p></div><div class="shared"><div class="shared-head"><h3>共同证据块</h3><span class="pill">两侧逐字相同</span></div><pre class="raw" id="shared-block"></pre></div><div class="columns"><div class="addon"><h3><span>I4侧设计稿 · 组织与条件反思</span><span class="small" id="i4-size"></span></h3><pre class="raw" id="i4-addon"></pre><div class="metrics" id="i4-metrics"></div></div><div class="addon"><h3><span>I5侧设计稿 · 平铺复述</span><span class="small" id="i5-size"></span></h3><pre class="raw" id="i5-addon"></pre><div class="metrics" id="i5-metrics"></div></div></div><div class="prompt"><b>两侧相同的回应提示：</b><span id="response-prompt"></span><span class="small">（每轮一次回应机会）</span></div><p class="small">“I4 / I5”仅是本评审页标题，不进入受试消息。真实消息标题都为“阅读材料 / 阅读说明”。</p><details><summary>展开两侧实际待发送文本（计字以此为准）</summary><div class="columns"><div><h3>I4侧完整消息</h3><pre class="raw" id="i4-full"></pre></div><div><h3>I5侧完整消息</h3><pre class="raw" id="i5-full"></pre></div></div></details><details><summary>审核专用完整来源链接与家族映射</summary><div id="source-list"></div><p class="small">这些链接仅供审核，不传给受试Agent，也不提供额外浏览动作。若后续模型实际收到链接、更多来源说明或新提示，须计入消息长度并生成新版本。</p></details></section>
<section class="panel"><div class="facts"><div><h2>本轮确实完成了什么</h2><p><span class="fact-number">01</span>18条证据正文逐字保留，未缩写或丢弃。</p><p><span class="fact-number">02</span>短来源映射与原始URL、日期及家族逐项核对。</p><p><span class="fact-number">03</span>两侧总长与附加文字长度分别匹配，回应提示一致。</p><p><span class="fact-number">04</span>AI逐段检查12段新增文字，未发现必须修订的新增事实；保留适用限制。</p><p><span class="fact-number">05</span>验证器独立重建消息与汇总，拒绝重复轮次或伪造个性化完成声明。</p><p class="small" id="tests"></p></div><div><h2>正式使用前仍需完成</h2><ul><li>双人人工核对、来源与译写权利确认。</li><li>正式冻结新显示规则；不与旧版完整URL输入无记录混用。</li><li>用T1自然观点生成共同包，再获取真实四阶段报告和个性化镜像。当前I4侧不是完整助手产品干预。</li><li>真实输出重新接受剂量、引用与新增事实审核；偏差留痕，不裁剪后假称通过。</li><li>其他四个干预臂、受试测量和曝光材料库存继续单独验收。</li></ul></div></div></section>
<section class="panel"><h2>文件与复核</h2><p class="small">pairs/ 保存成对装配及审计信息；payloads/ 保存严格待发送消息；inputs/ 保存未改证据、来源映射和草稿；review/ 保存AI与人工待审记录。不要把整份pair或review文件当成受试消息。</p><p>可独立验证：<code>python -m cocoon.dose_build verify --build-dir &lt;本次目录&gt;</code></p><pre class="raw" id="path"></pre><p class="small" id="identity"></p></section><footer id="footer"></footer></main>
<script type="application/json" id="dose-data">__DOSE_DATA__</script><script>
'use strict';const D=JSON.parse(document.getElementById('dose-data').textContent),$=id=>document.getElementById(id);const names={TOPIC_01:'碳与气候',TOPIC_02:'能源转型',TOPIC_03:'中文信息核查'};const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));const sizes=D.rows.flatMap(r=>[r.summary.I4_visible_chars,r.summary.I5_visible_chars]);$('pair-stat').textContent=D.verification.pairs_passed+' / '+D.verification.pairs_checked;$('range-stat').textContent=Math.min(...sizes)+'–'+Math.max(...sizes);$('diff-stat').textContent=(100*Math.max(...D.rows.map(r=>r.summary.additional_difference_ratio))).toFixed(2)+'%';$('length-table').innerHTML=D.rows.map(({summary:s})=>`<tr><td>${esc(names[s.topic_id])} · 第${s.round}轮</td><td>${s.old_evidence_block_chars}</td><td>${s.new_evidence_block_chars}</td><td>${s.I4_visible_chars}</td><td>${s.I5_visible_chars}</td><td>${(100*s.additional_difference_ratio).toFixed(2)}%</td><td class="${s.assembly_valid?'pass':'warn'}">${s.assembly_valid?'通过':'偏差保留'}</td></tr>`).join('');
function select(i){const {summary:s,pair:p}=D.rows[i];$('selected-title').textContent=names[s.topic_id]+' · 第'+s.round+'轮';$('selected-summary').textContent='共同证据：'+s.evidence_ids.join('、')+'；完整稿长度差 '+(s.total_difference_ratio*100).toFixed(2)+'%。';$('shared-block').textContent=p.I4.evidence_block;for(const arm of ['I4','I5']){const id=arm.toLowerCase();$(id+'-addon').textContent=p[arm].additional_text;$(id+'-size').textContent=p[arm].additional_chars+'字符';$(id+'-metrics').textContent='完整消息 '+p[arm].char_count+'字符 · 1次回应机会';$(id+'-full').textContent=p[arm].visible_text}$('response-prompt').textContent=p.I4.response_prompt;$('source-list').innerHTML=p.I4.source_map.map(v=>`<div class="source-row"><code>${esc(v.source_id)}</code><div><a href="${esc(v.source_url)}" target="_blank" rel="noopener">${esc(v.publisher_short)} · ${esc(v.publication_date||'日期未明')}</a><br><span class="small">${esc(v.source_url)}<br>来源族：${esc(v.source_family_id)}</span></div></div>`).join('');document.querySelectorAll('#tabs button').forEach((b,j)=>b.classList.toggle('active',i===j))}
D.rows.forEach((r,i)=>{const b=document.createElement('button');b.textContent=names[r.summary.topic_id]+' · '+r.summary.round;b.addEventListener('click',()=>select(i));$('tabs').append(b)});select(0);$('tests').textContent=D.tests?'自动测试：'+D.tests.tests_run+'项；失败'+D.tests.failures+'，错误'+D.tests.errors+'。':'测试记录见交付清单。';$('path').textContent=D.manifest.build_id;$('identity').textContent='内容身份：'+D.manifest.content_origin+'；自然观点：未使用；人工审核：0；正式资格：否。';$('footer').textContent='构建时间：'+D.manifest.built_at_utc+' · source_id_inline_v1 · 旧证据版本保留 · 本评审页不是受试上下文';
</script></body></html>'''


def render_evidence_report(path, build_directory):
    """候选证据阅读页；审核信息仅存在评审页，不进入助手packet。"""
    from .evidence import verify_bundle
    directory = Path(build_directory)
    verification = verify_bundle(directory)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    review = json.loads((directory / "review/evidence_review.json").read_text(encoding="utf-8"))
    sources = json.loads((directory / "review/sources.json").read_text(encoding="utf-8"))
    esc = lambda value: html.escape(str(value), quote=True)
    level_names = {"scientific_explainer": "科学机制说明", "research_explainer": "研究介绍",
                   "first_party_report_summary": "报告发布方摘要", "first_party_joint_report_release": "联合报告发布方新闻",
                   "original_research": "原始研究论文", "secondary_report": "二手转引",
                   "official_historical_response": "历史公开答复", "official_historical_standard_explainer": "历史标准解释",
                   "science_explainer": "科普解释"}
    tabs = ['<button class="filter active" data-topic="all">全部证据 · 18</button>']
    topics = []
    for i, topic in enumerate(manifest["topics"], 1):
        tid = topic["topic_id"]
        short = ["碳与气候", "能源转型", "中文信息核查"][i - 1]
        tabs.append(f'<button class="filter" data-topic="{tid}">{short} · {topic["evidence_count"]}</button>')
        topics.append(f'<article class="topic-card"><span class="eyebrow">0{i} / {tid}</span><h3>{esc(short)}</h3>'
                      f'<p>{esc(topic["candidate_title"].split("：", 1)[-1])}</p><div class="topic-meta">'
                      f'{topic["evidence_count"]} 条证据 · {topic["source_count"]} 个页面 · {topic["source_family_count"]} 个家族</div>'
                      f'<details><summary>时间范围与使用边界</summary><p>{esc(topic["time_scope"])}</p>'
                      f'<p>{esc(topic["scope_note"])}</p></details></article>')
    cards = []
    for item in review["items"]:
        source = item["source"]
        secondary = source["source_level"] == "secondary_report"
        translation = "英文→中文释义" if item["transformation_type"] == "translated_adapted" else "中文摘编"
        date = source["publication_date"] or "首发日期未查明"
        search_content = item["evidence_id"] + " " + item["text"] + " " + source["publisher"]
        cards.append(f'''<article class="evidence-card" data-topic="{esc(item["topic_id"])}" data-search="{esc(search_content)}">
<div class="evidence-head"><span class="eid">{esc(item["evidence_id"])}</span><span class="tag">{translation}</span><span class="tag {'warning' if secondary else ''}">{esc(level_names[source["source_level"]])}</span><span class="review-tag">人审待完成</span></div>
<p class="evidence-text">{esc(item["text"])}</p><div class="source-line"><a href="{esc(source['url'])}" target="_blank" rel="noopener">{esc(source["title"])}</a><span>{esc(source["publisher"])} · {esc(date)}</span></div>
<details class="quote"><summary>查看原文、译写边界与取证信息</summary><div class="quote-label">来源连续原文 · 仅规范化空白</div><blockquote>{esc(item["original_quote"])}</blockquote><p><b>适用边界：</b>{esc(item["boundary_note"])}</p><p class="small"><b>候选复核维度：</b>{esc(item["review_dimension"])}（仅评审可见，未写入助手包）</p><div class="provenance"><div>来源ID：{esc(source["source_id"])} · 来源家族：{esc(source["source_family_id"])}</div><div>获取时间：{esc(source["retrieved_at_utc"])}</div><div>原文位置：[{item["quote_start"]}, {item["quote_end"]})，规范化文本字符偏移</div><div>网页SHA256：{esc(source["raw_sha256"])}</div><div>中文证据SHA256：{esc(item["content_sha256"])}</div></div><p class="small">{esc(source["license_note"])}</p></details></article>''')
    dose_rows = ''.join(f'<tr><td>{esc(topic["topic_id"])}</td><td>第{index + 1}轮</td><td>{dose["evidence_block_characters"]}</td>'
                         f'<td>{dose["characters_remaining_to_800"]}</td><td>{"证据块已超限" if dose["evidence_alone_exceeds_800"] else "空间不足，需重设计"}</td></tr>'
                         for topic in manifest["topics"] for index, dose in enumerate(topic["round_dose_diagnostics"]))
    source_rows = ''.join(f'<tr><td>{esc(source["source_id"])}</td><td><a href="{esc(source["url"])}" target="_blank" rel="noopener">{esc(source["title"])}</a><br><span class="small">{esc(source["publisher"])}</span></td><td>{esc(level_names[source["source_level"]])}</td><td>{esc(source["publication_date"] or "未知")}</td></tr>' for source in sources["sources"])
    exclusions = ''.join(f'<details><summary>{esc(item["url"])}</summary><p>{esc(item["reason"])}</p></details>' for item in sources["exclusions"])
    page = f'''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>破茧助手 · 三议题证据审阅</title><style>
:root{{color-scheme:light;--ink:#25352e;--muted:#6b7b70;--accent:#386b4f;--line:#dce5dc}}*{{box-sizing:border-box}}body{{margin:0;background:#f5f7f2;color:var(--ink);font:14px/1.8 "Microsoft YaHei",system-ui,sans-serif}}main{{max-width:1160px;margin:auto;padding:36px 30px 50px}}h1{{font-size:32px;margin:6px 0}}h2{{font-size:21px;margin:0 0 18px}}h3{{font-size:18px;margin:8px 0}}.eyebrow{{font-size:11px;font-weight:600;color:var(--accent);letter-spacing:2px}}p{{margin:10px 0}}a{{color:#366b53;text-decoration:none}}a:hover{{text-decoration:underline}}.subtitle,.small{{color:var(--muted)}}.small{{font-size:12px}}.banner{{background:#fff4dc;border-left:3px solid #bb9554;border-radius:0 8px 8px 0;padding:14px 20px;margin:23px 0;color:#796035}}.stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}}.stat,.panel,.topic-card,.evidence-card{{background:white;border:1px solid var(--line);border-radius:12px}}.stat{{padding:18px 22px}}.stat b{{font-size:29px;display:block;line-height:1.6}}.stat span{{font-size:12px;color:var(--muted)}}.topic-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin:24px 0}}.topic-card{{padding:21px}}.topic-card p{{font-size:12px;color:var(--muted)}}.topic-meta{{font-size:12px;color:var(--accent)}}.topic-card details p{{max-height:220px;overflow:auto}}details{{margin:10px 0}}summary{{color:var(--accent);font-size:12px;cursor:pointer;overflow-wrap:anywhere}}.panel{{padding:24px;margin:22px 0}}.bar{{display:flex;justify-content:space-between;gap:15px;align-items:center;flex-wrap:wrap;margin:12px 0 18px}}.filters{{display:flex;gap:7px;flex-wrap:wrap}}button,input{{font:inherit;border-radius:7px;border:1px solid #cedbcf;padding:8px 12px;background:white;color:var(--ink)}}button{{cursor:pointer;font-size:12px}}button.active{{background:#e7f0e4;border-color:#adc9ad;color:#2e5c3f}}input{{width:265px;font-size:12px}}.count{{font-size:12px;color:var(--muted)}}.evidence-card{{padding:22px;margin-bottom:14px}}.evidence-head{{display:flex;align-items:center;gap:8px;flex-wrap:wrap}}.eid{{font:600 12px Consolas,monospace;color:#477459;letter-spacing:1px}}.tag{{padding:2px 7px;border-radius:5px;background:#eef3e9;color:#587252;font-size:11px}}.tag.warning{{background:#fff0d6;color:#876324}}.review-tag{{font-size:11px;color:#9a7946;margin-left:auto}}.evidence-text{{font-size:15px;line-height:1.95;letter-spacing:.2px;margin:14px 0}}.source-line{{font-size:12px;color:var(--muted);display:flex;flex-direction:column;border-top:1px solid #eef2e9;padding-top:11px}}.quote{{margin-bottom:0}}.quote p{{font-size:12px}}.quote-label{{font-size:11px;color:var(--muted);margin-top:12px}}blockquote{{margin:10px 0;padding:12px 17px;background:#f3f6ee;border-left:2px solid #bdd0b8;font-size:13px;color:#4c614e;line-height:1.8}}.provenance{{font:11px/1.8 Consolas,"Microsoft YaHei",monospace;color:#7b8975;overflow-wrap:anywhere;background:#f8faf5;padding:10px;border-radius:6px}}table{{width:100%;border-collapse:collapse;font-size:12px}}td,th{{border-bottom:1px solid #e7ede3;padding:10px;text-align:left;vertical-align:top}}th{{font-weight:500;color:#6f7b68;background:#f7f9f3}}.table-wrap{{overflow:auto}}.limits{{display:grid;grid-template-columns:1fr 1fr;gap:22px}}li{{margin-bottom:10px}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;padding:15px;background:#f3f6ee;border-radius:8px;font:12px/1.8 Consolas,"Microsoft YaHei",monospace}}.empty{{padding:24px;text-align:center;color:var(--muted)}}footer{{font-size:11px;color:#84917b;border-top:1px solid var(--line);margin-top:30px;padding-top:17px}}[hidden]{{display:none!important}}@media(max-width:850px){{.topic-grid,.limits{{grid-template-columns:1fr}}.stats{{grid-template-columns:repeat(2,1fr)}}main{{padding:24px 18px}}h1{{font-size:26px}}.panel{{padding:18px}}}}
</style></head><body><main><div class="eyebrow">COCOONBREAKER / EVIDENCE CANDIDATES V1</div><h1>三议题证据包 · 原文可回查</h1><p class="subtitle">按原方案第4.3节整理：碳与气候、能源转型、中文公共信息核查。</p><div class="banner"><b>真实来源候选包，不是已批准的正式实验材料。</b> 机器完整性检查和独立AI复核已完成；双人人工审核为0。没有调用MiMo、没有受试结果，也没有用AI复核冒充人审。</div>
<div class="stats"><div class="stat"><span>候选议题</span><b>3</b><span>沿用原方案建议方向</span></div><div class="stat"><span>中文证据条目</span><b>18</b><span>每题6条 · 明确翻译/摘编</span></div><div class="stat"><span>入选来源页面</span><b>13</b><span>11个保守归并家族，非独立研究数</span></div><div class="stat"><span>引文与哈希校验</span><b>18/18</b><span>精确包含不等于语义证明</span></div></div>
<div class="topic-grid">{''.join(topics)}</div><section><h2>逐条阅读与核对</h2><div class="bar"><div class="filters">{''.join(tabs)}</div><input type="search" id="search" placeholder="搜索关键词、编号或来源机构" aria-label="搜索证据"></div><div class="bar"><span class="count" id="count">18 条证据</span><button id="toggle-quotes">展开全部原文与边界</button></div><div id="evidence-list">{''.join(cards)}</div><p id="empty" class="empty" hidden>没有匹配的证据。</p></section>
<section class="panel"><h2>重要发现与使用限制</h2><div class="limits"><div><h3>已解决的核查问题</h3><ul><li>酸化条目的原引文原先未直接覆盖“仍呈碱性”，现已换成同页连续原文，并保留首次问题记录。</li><li>天气历史答复明确写入二〇二〇年日期，不把旧网站做法当作当前规则。</li><li>能源成本条目标明二手转引；就业1620万是领域就业存量，不是当年新创岗位。</li></ul></div><div><h3>不能据此宣称完成</h3><ul><li>三个候选立场轴目前是可兼容的侧重点，未验证为合理对立两端，暂不用于正式U或立场极化结论。</li><li>没有经取证的历史错误卡，也没有每题108张曝光/保持材料、8道锚题及独立金标准。</li><li>两轮只是顺序切分草案，不是按T1自然观点检索生成的个性化共同包。</li><li>来源权利、译写及双人复核仍待完成；本包限本地审阅，不直接公开再分发。</li></ul></div></div></section>
<section class="panel"><h2>两轮剂量诊断：不能直接当最终干预文本</h2><p class="small">按现有渲染器计入证据正文、完整URL和来源族ID。总目标为每轮600–800字符，还必须给I4镜像/反思及I5复述留出空间。未裁剪证据或改动方案上限。</p><div class="table-wrap"><table><thead><tr><th>议题</th><th>候选轮次</th><th>仅证据块字符</th><th>至800剩余</th><th>当前判断</th></tr></thead><tbody>{dose_rows}</tbody></table></div><p class="small">后续应在正式冻结前确定更紧凑的来源显示规则或重新安排证据剂量；必须对I4/I5一致，不能只删减其中一组。</p></section>
<section class="panel"><h2>来源清单</h2><div class="table-wrap"><table><thead><tr><th>编号</th><th>来源</th><th>材料层级</th><th>发布日</th></tr></thead><tbody>{source_rows}</tbody></table></div><details><summary>查看未采用来源与获取失败记录</summary>{exclusions}</details></section>
<section class="panel"><h2>文件怎么使用</h2><pre>packets/TOPIC_01_full.json     碳与气候完整候选包
packets/TOPIC_02_full.json     能源转型完整候选包
packets/TOPIC_03_full.json     中文公共信息核查候选包
packets/*_round_1.json        第一轮切分草案
packets/*_round_2.json        第二轮切分草案
review/evidence_review.json   人工审核字段与原文（不可直接发给助手）
manifest.json                版本、来源、范围与文件哈希
verification.json            程序核验结果</pre><p class="small">is_demo=false仅说明这些条目基于真实来源，不代表人工批准。完整网页快照保留在项目intake目录，不含在可下载压缩包中；压缩包仅含必要短引、释义、元数据和审阅页。不要把整个review目录发给受试Agent或助手。</p><p class="small">原方案：{esc(manifest["protocol_path"])}<br>原方案SHA256：{esc(manifest["protocol_sha256"])}</p></section><footer>构建时间：{esc(manifest["built_at_utc"])} · 正式材料批准：否 · 人工复核：0 · 额外MiMo调用：0 · 本页为只读评审视图，不是助手输入</footer></main>
<script>'use strict';let topic='all';const cards=[...document.querySelectorAll('.evidence-card')],search=document.getElementById('search');function apply(){{let n=0;const q=search.value.toLocaleLowerCase().trim();for(const c of cards){{const shown=(topic==='all'||c.dataset.topic===topic)&&(!q||c.dataset.search.toLocaleLowerCase().includes(q));c.hidden=!shown;if(shown)n++}}document.getElementById('count').textContent=n+' 条证据';document.getElementById('empty').hidden=n!==0}}document.querySelectorAll('.filter').forEach(b=>b.addEventListener('click',()=>{{topic=b.dataset.topic;document.querySelectorAll('.filter').forEach(x=>x.classList.toggle('active',x===b));apply()}}));search.addEventListener('input',apply);let expanded=false;document.getElementById('toggle-quotes').addEventListener('click',()=>{{expanded=!expanded;document.querySelectorAll('.evidence-card .quote').forEach(d=>d.open=expanded);document.getElementById('toggle-quotes').textContent=expanded?'收起全部原文与边界':'展开全部原文与边界'}});apply();</script></body></html>'''
    path = Path(path)
    if not path.parent.is_dir():
        raise FileNotFoundError(path.parent)
    with path.open("x", encoding="utf-8", newline="\n") as file:
        file.write(page)
    return path


def render_mimo_report(path, status, tests, adapter_status=None):
    """MiMo本机接入状态；不把离线HTTP测试显示为真实连通。"""
    esc = lambda value: html.escape(str(value), quote=True)
    config = status.get("parameters") or {}
    billing = status["billing"]
    options = [("模型ID", status["model"]), ("接口", status["endpoint"]), ("鉴权", "api-key 请求头；从MIMO_API_KEY环境变量读取"),
               ("思考模式", config.get("thinking")), ("温度", config.get("temperature")),
               ("助手输出上限", str(config.get("max_tokens")) + " tokens"), ("连通性测试输出上限", "128 tokens"),
               ("流式 / 自动重试 / 联网搜索", "关闭 / 0次 / 关闭")]
    rows = ''.join(f'<tr><td>{esc(k)}</td><td>{esc(v)}</td></tr>' for k, v in options)
    ready = status["credential"]["usable_format"]
    subtitle = "密钥已存在，仍须真实请求验证" if ready else "代码已接入；等待本机配置密钥"
    html_text = f'''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>MiMo接入状态</title><style>
:root{{color-scheme:light}}*{{box-sizing:border-box}}body{{background:#f4f6f1;color:#23382c;font:14px/1.75 "Microsoft YaHei",system-ui,sans-serif;margin:0}}main{{max-width:1020px;margin:auto;padding:36px 25px}}h1{{font-size:29px;margin:8px 0}}h2{{font-size:18px;margin:0 0 16px}}p{{color:#637566}}.eyebrow{{color:#4e7b57;font-size:11px;letter-spacing:2px}}.banner{{padding:14px 18px;background:#fff3d8;border-left:3px solid #b29251;margin:23px 0;border-radius:0 8px 8px 0}}.stats{{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}}.card{{background:white;border:1px solid #dce5d8;border-radius:12px;padding:23px;margin-bottom:18px}}.big{{font-size:29px;font-weight:650}}.small{{font-size:12px;color:#74816e}}.grid{{display:grid;grid-template-columns:1.1fr 1fr;gap:18px}}table{{border-collapse:collapse;width:100%;font-size:12px}}td{{padding:10px 7px;border-bottom:1px solid #e4eadf;overflow-wrap:anywhere}}td:first-child{{width:33%;color:#71806b}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#f1f5ed;border-radius:8px;padding:14px;font:12px/1.8 Consolas,"Microsoft YaHei",monospace}}li{{margin-bottom:11px}}a{{color:#357451}}.tag{{border-radius:6px;background:#e8f2e4;color:#476c45;padding:4px 9px;font-size:12px}}footer{{font-size:11px;color:#83917b}}@media(max-width:760px){{.grid{{grid-template-columns:1fr}}.stats{{gap:8px}}.card{{padding:16px}}h1{{font-size:24px}}}}</style></head><body><main>
<div class="eyebrow">COCOONBREAKER / XIAOMI MIMO INTEGRATION</div><h1>MiMo-V2.6-Flash</h1><p>{subtitle}</p><div class="banner"><b>尚未证明真实连通。</b> 此页是本机配置与离线验收状态，不是正式实验结果。缺少密钥时不会发送请求，更不会自动切换为演示结果。</div>
<div class="stats"><div class="card"><div class="small">首次测试累计预算</div><div class="big">¥{esc(billing.get("limit_cny", "—"))}</div><div class="small">跨运行累计，不是每次重置</div></div><div class="card"><div class="small">已登记收费请求</div><div class="big">{esc(billing.get("request_count", "—"))}</div><div class="small">HTTP尝试不等于计费确认</div></div><div class="card"><div class="small">离线自动测试</div><div class="big">{tests["tests_run"]}</div><div class="small">失败 {tests["failures"]} / 错误 {tests["errors"]} / 跳过 {tests["skipped"]}</div></div></div>
<div class="grid"><section class="card"><h2>本次接入配置</h2><table>{rows}</table><p class="small">本轮接入助手链与连通性探测；受试/评估模型、正式实验参数另行冻结。thinking disabled使温度参数可生效，避免默认思考模式强制覆盖温度。</p></section><section class="card"><h2>当前需要你完成</h2><ol><li>到小米控制台创建普通按量API Key（sk-开头）。<br><a href="https://platform.xiaomimimo.com/#/console/api-keys" target="_blank" rel="noopener">打开API Keys控制台</a></li><li>在本机用户环境变量中添加 <b>MIMO_API_KEY</b>，值填入密钥。不要发到聊天、配置文件或日志。</li><li>重新启动读取环境变量的应用/终端后，先检查状态，再只发一个连通性请求。</li></ol><pre>python -m cocoon mimo-status
python -m cocoon mimo-smoke --execute</pre><p class="small">第二条命令会发送真实收费请求，单次上限128输出tokens。默认不加--execute不会收费。</p></section></div>
<section class="card"><h2>计费保护与价格依据</h2><p>已确认按量付费，首次累计上限¥5；所有调用共用持久台账。请求前保守预留，响应后按usage结算；超时或用量不明时保留预留并阻止后续调用，不会自动重试或换新台账。</p><table><tr><td>输入（未命中缓存）</td><td>¥1.00 / 百万tokens</td></tr><tr><td>输入（命中缓存）</td><td>¥0.02 / 百万tokens</td></tr><tr><td>输出</td><td>¥2.00 / 百万tokens</td></tr><tr><td>已结算 / 尚预留</td><td>¥{esc(billing.get("charged_cny", "未知"))} / ¥{esc(billing.get("reserved_cny", "未知"))}</td></tr></table><p class="small">价格为2026-11-08核对的官方国内实时价格，页面更新时间2026-10-21。记账按公开单价与供应商用量，不是发票；缺失缓存明细按未命中保守计算。预留采用1M输入上限而非实际token估计，因此可能提前停止，不能将预留额误认为实际花费。</p><a href="https://mimo.mi.com/docs/zh-CN/price/pay-as-you-go" target="_blank" rel="noopener">官方价格</a> · <a href="https://mimo.mi.com/docs/zh-CN/api/chat/openai-api" target="_blank" rel="noopener">官方API协议</a></section>
<section class="card"><h2>保护措施已接入</h2><span class="tag">官方地址白名单</span> <span class="tag">TLS校验</span> <span class="tag">禁止重定向携带密钥</span> <span class="tag">跨进程预算锁</span> <span class="tag">无模拟回退</span><p>正常响应保留原始HTTP正文、结构化消息与usage；若响应疑含凭据，或JSON不完整导致安全扫描无法完成，只保存哈希与字节数，不记录正文。此安全例外会明确留痕。</p><p class="small">完整助手探测须显式指定冻结证据包：python -m cocoon mimo-assistant --packet &lt;证据包JSON&gt; --execute。正式run入口仍关闭；此命令不能替代材料审核、OASIS接入及受试实验。</p></section><footer>生成：{esc(utc_now())} · 本页无脚本和外部依赖 · 不在网页收集密钥</footer></main></body></html>'''
    path = Path(path)
    if not path.parent.is_dir():
        raise FileNotFoundError(path.parent)
    with path.open("x", encoding="utf-8", newline="\n") as file:
        file.write(html_text)
    return path


def render_adapter_report(path, adapter_status, tests=None):
    """只交付通过原件/组件/事件校验的离线验收记录，不承诺真实模型效果。"""
    if adapter_status.get("offline_verified") is not True:
        raise ValueError("离线验收记录未核实，拒绝生成成功报告")
    path = Path(path)
    if not path.parent.is_dir():
        raise FileNotFoundError(path.parent)
    summary = adapter_status["summary"]
    escape = lambda value: html.escape(str(value), quote=True)
    cases = summary["scenarios"]
    sample = summary["successful_five_stage_example"]
    stage_names = {"search": "冻结证据梳理", "label": "信源标注", "blindspot": "盲区分析",
                   "integrate": "报告整合", "mirror": "自我认知镜"}
    parts = []
    from .prompts import STAGES
    for number, stage in enumerate(STAGES, 1):
        row = sample["stage_results"][stage]
        request_hash = row["input"]["sha256"]
        response_hash = row["response"]["sha256"]
        parts.append(f'<article class="stage"><div class="step-number">0{number}</div><div>'
            f'<h3>{escape(stage_names[stage])}<span class="ok">契约通过</span></h3>'
            f'<p>引用ID：{escape(", ".join(row["citation_audit"]["cited_ids"]))} · 语义审核待完成</p>'
            f'<details><summary>查看保存的原始夹具响应</summary><pre>{escape(row["raw_text"])}</pre></details>'
            f'<details><summary>查看输入 / 输出哈希</summary><pre>输入 {escape(request_hash)}\n输出 {escape(response_hash)}</pre></details>'
            '</div></article>')
    scenario_labels = {"five_stage_success":"五阶段正常执行", "four_stage_success":"关闭镜像的四阶段执行",
        "search_timeout":"检索阶段超时", "label_connection":"标注阶段连接失败",
        "blindspot_invalid_json":"盲区阶段非法JSON", "integrate_unknown_citation":"整合引用不存在证据",
        "mirror_quote_mismatch":"镜像引文不在用户原句", "integrate_truncated":"整合输出截断",
        "label_refused":"标注阶段拒答", "context_overflow":"上下文超长", "private_label_rejected":"混入金标准字段",
        "live_mode_blocked":"未接入的真实模式", "error_message_private":"异常文本含敏感测试标记"}
    rows = ''.join(f'<tr><td>{escape(scenario_labels.get(case["name"], case["name"]))}</td>'
        f'<td><span class="ok">{"通过" if case["passed"] else "未通过"}</span></td>'
        f'<td>{"成功完成" if case["actual_status"] == "succeeded" else "按预期停止"}</td>'
        f'<td>{escape(case["actual_error"] or "—")}</td><td>{case["fixture_calls"]}</td></tr>' for case in cases)
    fingerprints = ''.join(f'<tr><td>{escape(name)}</td><td class="hash">{escape(sha)}</td></tr>'
                           for name, sha in summary["component_hashes"].items())
    test_count = tests["tests_run"] if tests else "未附"
    test_note = f'失败 {tests["failures"]} / 错误 {tests["errors"]} / 跳过 {tests["skipped"]}' if tests else "单独运行 validate 获取测试记录"
    body = f'''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>破茧助手 · 实验适配验收</title><style>
:root{{color-scheme:light}}*{{box-sizing:border-box}}body{{margin:0;background:#f4f6f1;color:#23342c;font:14px/1.7 "Microsoft YaHei",system-ui,sans-serif}}main{{max-width:1180px;margin:auto;padding:38px 28px}}.eyebrow{{font-size:11px;letter-spacing:2px;color:#47715a;font-weight:700}}h1{{font-size:32px;margin:8px 0}}h2{{font-size:19px;margin:0 0 18px}}h3{{font-size:15px;margin:0 0 8px}}p{{margin:7px 0;color:#607165}}.notice{{background:#fff2d7;border-left:3px solid #b58c47;padding:15px 20px;margin:25px 0;border-radius:0 8px 8px 0;color:#795a25}}.metrics{{display:grid;grid-template-columns:repeat(4,1fr);gap:15px}}.card{{background:white;border:1px solid #dce6dc;border-radius:12px;padding:23px;margin-bottom:20px}}.value{{font-size:31px;font-weight:650;margin:4px 0}}.small{{font-size:12px;color:#758477}}.grid{{display:grid;grid-template-columns:1fr 1fr;gap:20px}}.ok{{font-size:11px;background:#e8f3e9;color:#326747;border-radius:5px;padding:3px 7px;margin-left:12px;white-space:nowrap}}.stage{{display:grid;grid-template-columns:32px 1fr;gap:12px;padding:15px 0;border-bottom:1px solid #e9eee6}}.stage:last-child{{border:0}}.stage p{{font-size:12px}}.step-number{{color:#598465;font-size:13px}}details{{margin-top:8px}}summary{{cursor:pointer;color:#47715a;font-size:12px}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word;background:#f5f8f1;padding:12px;border-radius:7px;font:12px/1.7 Consolas,"Microsoft YaHei",monospace;max-height:300px;overflow:auto}}.table-wrap{{overflow:auto}}table{{width:100%;border-collapse:collapse;font-size:12px}}th,td{{padding:11px 9px;text-align:left;border-bottom:1px solid #e5ece2}}th{{color:#71806f;font-weight:500;background:#f6f9f2}}.hash{{font-family:Consolas,monospace;word-break:break-all}}li{{margin-bottom:12px}}footer{{font-size:11px;color:#71806f;margin-top:20px}}.tag{{display:inline-block;border:1px solid #cddcca;padding:5px 10px;border-radius:15px;font-size:12px;margin:5px 7px 5px 0}}@media(max-width:800px){{.grid{{grid-template-columns:1fr}}.metrics{{grid-template-columns:repeat(2,1fr)}}main{{padding:22px 18px}}h1{{font-size:25px}}}}
</style></head><body><main><div class="eyebrow">COCOONBREAKER / EXPERIMENT ADAPTER V1</div><h1>原助手已接入离线实验执行层。</h1><p>保留原件，冻结提示词，失败即停止。现在验证的是执行契约，不是干预效果。</p><div class="notice"><b>本页全部阶段响应均为显式合成夹具。</b>没有真实API调用、联网检索或受试Agent认知测量。原助手的角色提示词与实验覆盖约束分别记录；这是实验适配变体，不是未修改产品效果验证。</div>
<div class="metrics"><div class="card"><div class="small">已冻结原版角色提示词</div><div class="value">5 <span class="small">条</span></div><div class="small">来自前端实际引用的内联模板</div></div><div class="card"><div class="small">离线集成验收</div><div class="value">{len(cases)} / {len(cases)}</div><div class="small">含失败路径；失败不继续后续阶段</div></div><div class="card"><div class="small">自动测试用例</div><div class="value">{test_count}</div><div class="small">{escape(test_note)}</div></div><div class="card"><div class="small">真实模型调用 / API支出</div><div class="value">0 <span class="small">/ ¥0</span></div><div class="small">工程调用 {summary["fixture_calls"]} 次单列，不冒充模型调用</div></div></div>
<div class="grid"><section class="card"><h2>五阶段链路 · 保存了什么</h2>{''.join(parts)}</section><section class="card"><h2>本轮完成的改造</h2><ul><li><b>去除失败回退路径</b><p>API接口异常、JSON错误、未知引用、输出截断都停止本任务，不返回预置分析结果。</p></li><li><b>固定证据输入</b><p>证据正文逐条SHA256核验；拒绝额外金标准字段；下游沿用同一证据包。没有真实搜索就不标成检索完成。</p></li><li><b>原文与结构化结果并存</b><p>每次请求、原始回复、解析结果、引用、模型标识、用量和错误写入追加日志。夹具未报告用量时保持未知。</p></li><li><b>镜像输入隔离</b><p>个人观点仅进入最后的镜像阶段；引文必须在输入原句中出现，不污染前四阶段。</p></li><li><b>输出仍需独立语义审核</b><p>已知引用ID并不证明句子被证据支持。全部产物formal_eligible=false，不直接当作正式I4交付。</p></li></ul><span class="tag">原ZIP未改动</span><span class="tag">不执行原网页</span><span class="tag">不隐式重试</span><span class="tag">正式实验仍关闭</span></section></div>
<section class="card"><h2>正常路径与失败路径验收</h2><div class="table-wrap"><table><thead><tr><th>场景</th><th>验收</th><th>实际行为</th><th>停止原因</th><th>夹具调用数</th></tr></thead><tbody>{rows}</tbody></table></div><p class="small">按预期失败代表防护生效，不代表真实API可用；全部日志保留，首次错误不会被重试掩盖。</p></section>
<div class="grid"><section class="card"><h2>下一步尚待满足</h2><ol><li>确定模型服务商、模型名、接口参数及预算上限；密钥仅配置在本机环境。</li><li>完成密钥配置及真实接口验收，再执行一个真实证据任务。</li><li>完成来源记录、双人审核、独立引用语义检查。</li><li>继续OASIS接入、I4/I5真实交付、权限隔离及小规模预实验。</li></ol></section><section class="card"><h2>复核入口</h2><pre>python -m cocoon assistant-status
python -m cocoon assistant-check
python -m cocoon validate
python -m cocoon preflight</pre><p class="small">assistant-check只运行离线验收，写入新目录，原记录不覆盖。preflight未就绪返回2是预期阻塞。</p><p class="hash small">{escape(summary["run_directory"])}</p></section></div>
<section class="card"><h2>版本与可追溯指纹</h2><p class="small">提示词包内容哈希（不是文件排版字节哈希）：</p><pre>{escape(summary["prompt_bundle_sha256"])}</pre><p class="small">实验变体指纹：</p><pre>{escape(summary["fingerprint"]["adapter_fingerprint"])}</pre><details><summary>展开当前源码组件哈希</summary><div class="table-wrap"><table><tbody>{fingerprints}</tbody></table></div></details></section><footer>验收完成时间：{escape(summary["completed_at_utc"])} · 生成时间：{escape(utc_now())} · JSONL单进程追加 · 本页无外部依赖，不执行网络请求</footer></main></body></html>'''
    with path.open("x", encoding="utf-8", newline="\n") as file:
        file.write(body)
    return path


TEMPLATE = r'''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>破茧助手 · 执行启动台</title>
<style>
:root{color-scheme:light;--bg:#f5f6f2;--paper:#fff;--ink:#202e2b;--muted:#687773;--line:#dce5df;--accent:#246b51;--green-bg:#e8f3ec;--amber:#805321;--amber-bg:#fff4dc}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.65 "Microsoft YaHei",system-ui,sans-serif}button,select,input{font:inherit}a{color:var(--accent)}.shell{display:grid;grid-template-columns:225px 1fr;min-height:100vh}aside{background:#edf2ec;border-right:1px solid var(--line);padding:32px 22px;position:sticky;top:0;height:100vh}.brand{font-size:23px;font-weight:750;letter-spacing:2px}.brand small{font-size:11px;display:block;color:var(--muted);letter-spacing:1.4px;margin-top:5px}.stage-label{margin:34px 0 14px;color:var(--muted);font-size:11px;letter-spacing:1px}nav button{display:block;width:100%;border:0;border-radius:9px;text-align:left;color:#4f625b;background:transparent;padding:11px 13px;margin:6px 0;cursor:pointer}nav button.on{background:white;color:var(--accent);box-shadow:0 2px 8px #293b2810;font-weight:700}.side-note{font-size:12px;color:var(--muted);margin-top:38px}.side-dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:#d99a3f;margin-right:6px}main{max-width:1400px;padding:34px 42px 50px;min-width:0}.top{display:flex;align-items:center;justify-content:space-between;gap:20px}.eyebrow{color:var(--accent);font-size:11px;letter-spacing:2px;font-weight:bold}h1{font-size:32px;margin:5px 0 7px;letter-spacing:1px}h2{font-size:19px;margin:0 0 16px}h3{font-size:14px;margin:0 0 8px}.sub{color:var(--muted);margin:0}.pill{background:var(--amber-bg);color:var(--amber);border:1px solid #ecd6ad;padding:6px 12px;border-radius:20px;font-size:12px;white-space:nowrap}.banner{border-left:3px solid #c99449;background:var(--amber-bg);padding:12px 16px;margin:26px 0 22px;color:#715426;border-radius:0 8px 8px 0}.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:15px}.card{background:var(--paper);border:1px solid var(--line);border-radius:12px;padding:22px;box-shadow:0 2px 3px #15221502}.label{font-size:12px;color:var(--muted)}.value{font-size:30px;line-height:1.5;font-weight:650;font-variant-numeric:tabular-nums}.value small{font-size:13px;font-weight:normal;color:var(--muted)}.grid{display:grid;grid-template-columns:1.2fr 1fr;gap:20px;margin-top:20px}.small{font-size:12px;color:var(--muted)}.section{display:none}.section.on{display:block}.step{display:grid;grid-template-columns:28px 1fr;gap:12px;padding:15px 0;border-bottom:1px solid #eef1ed}.step:last-child{border:0}.num{border:1px solid var(--line);border-radius:50%;height:26px;text-align:center;color:var(--accent);font-size:12px}.done{background:var(--green-bg);color:var(--accent);padding:2px 7px;border-radius:5px;font-size:11px}.blocked{background:var(--amber-bg);color:var(--amber);padding:2px 7px;border-radius:5px;font-size:11px}.issue{border-top:1px solid #edf0ec;padding:13px 0}.issue:first-of-type{border-top:0}.issue p{margin:5px 0 0;font-size:12px;color:var(--muted)}table{width:100%;border-collapse:collapse;font-size:12px}td,th{padding:10px 9px;border-bottom:1px solid #e9eeea;text-align:left;vertical-align:top}th{color:var(--muted);font-weight:500;background:#f6f8f4}.table-wrap{overflow:auto}.toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:14px 0}button.action,select,input{border:1px solid #d2ded6;border-radius:7px;background:white;padding:7px 10px;color:var(--ink)}button.action{cursor:pointer}button.primary{background:var(--accent);border-color:var(--accent);color:white}button:disabled{opacity:.4;cursor:default}select{max-width:310px}.events{max-height:500px;overflow:auto}.event{display:grid;grid-template-columns:45px 1fr;gap:10px;padding:11px 12px;border-bottom:1px solid #e8ede9;cursor:pointer}.event:hover,.event.active{background:#edf5ef}.event b{font-weight:550}.event .seq{color:var(--muted);font-size:11px}.code{white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word;background:#f7f9f5;padding:14px;border-radius:8px;font:12px/1.7 Consolas,"Microsoft YaHei",monospace;max-height:510px;overflow:auto}.empty{text-align:center;color:var(--muted);padding:35px 20px}.timeline{display:flex;margin:20px 0;gap:7px}.timeline div{flex:1;background:#eef3ec;border:1px solid #dce5d9;padding:10px;border-radius:7px;text-align:center;font-size:12px}.timeline b{display:block;color:var(--accent)}.meter{display:flex;height:22px;border-radius:5px;overflow:hidden}.meter span{text-align:center;font-size:10px;line-height:22px}.same{background:#b1d2bd;color:#214e37}.opposite{background:#d6d0eb;color:#504372}.neutral{background:#e8e7db;color:#676450}.fileline{overflow-wrap:anywhere;font-family:Consolas,"Microsoft YaHei",monospace;font-size:12px;color:#566e61}.footer{font-size:11px;color:var(--muted);margin-top:25px;border-top:1px solid var(--line);padding-top:14px}.full{margin-top:20px}.trace-card{padding:12px;border:1px solid #dfe7e0;border-radius:8px;margin:9px 0;background:white}.trace-card b{font-size:12px}.error{color:#9b493b}.teststat{font-size:26px;font-weight:600}.split{display:grid;grid-template-columns:1fr 1.2fr;gap:16px}
@media(max-width:1050px){main{padding:25px}.kpis{grid-template-columns:repeat(2,1fr)}.grid,.split{grid-template-columns:1fr}.shell{grid-template-columns:185px 1fr}aside{padding:25px 15px}}@media(max-width:700px){.shell{display:block}aside{height:auto;position:static;padding:20px}.brand small,.stage-label,.side-note{display:none}nav{display:flex;overflow:auto;margin-top:12px}nav button{white-space:nowrap;width:auto;font-size:12px}h1{font-size:25px}.top{align-items:start}.kpis{gap:9px}.card{padding:16px}.timeline{flex-wrap:wrap}.timeline div{min-width:70px}main{padding:20px}}
</style></head><body><div class="shell"><aside><div class="brand">破茧助手<small>RESEARCH OPERATIONS</small></div><div class="stage-label">实验方案 V1.0 · 执行启动</div><nav><button class="on" data-section="overview">01　启动总览</button><button data-section="design">02　配额与分支</button><button data-section="replay">03　工程事件回放</button><button data-section="validation">04　测试与待办</button></nav><div class="side-note"><span class="side-dot"></span>正式运行尚未启动<br>本地只读审计页<br>不会发送模型请求</div></aside>
<main><div class="top"><div><div class="eyebrow">FROM PROTOCOL TO EXECUTION</div><h1>先把实验做得可核查。</h1><p class="sub">启动工程底座 · 保留每一次输入、状态与失败 · 不把示例当结果</p></div><span class="pill">第一阶段 / 工程验证</span></div><div class="banner"><b>工程验证，非实验结果。</b> 本页没有真实 Agent 认知数据。合成夹具用于验证排程、日志、隔离和证据契约；不会显示虚构效果或显著性。</div>
<section class="section on" id="overview"><div class="kpis" id="kpis"></div><div class="grid"><div class="card"><h2>从共同起点到延迟测量</h2><p class="small">核心方案的设计量，不是已完成量</p><div class="timeline"><div><b>T0</b>共同起点</div><div><b>8轮</b>四环境曝光</div><div><b>T1</b>D整网分支</div><div><b>T2</b>2轮干预后</div><div><b>T3</b>4轮保持后</div></div><div id="progress"></div></div><div class="card"><h2>正式运行前的关键阻塞</h2><div id="issues-short"></div></div></div><div class="card full"><h2>工程运行记录 · 失败不隐藏</h2><div class="table-wrap"><table><thead><tr><th>运行ID</th><th>状态</th><th>说明</th></tr></thead><tbody id="run-history"></tbody></table></div></div><div class="card full"><h2>真实材料接入</h2><div id="intake"></div></div></section>
<section class="section" id="design"><div class="grid"><div class="card"><h2>曝光配额设计</h2><p class="small">同立场 / 异立场 / 中性，相对初始画像固定。下表来自离线实际排程，非认知观测。</p><div class="table-wrap"><table><thead><tr><th>环境</th><th>6卡构成</th><th>X</th><th>错误比例</th></tr></thead><tbody id="quota"></tbody></table></div><p class="small">中性材料不计入异见；讨论槽单独登记。准备好的上下文不等于已送达模型。</p></div><div class="card"><h2>I4 / I5 证据匹配</h2><div id="packets"></div><p class="small">当前两段文字都是等长工程夹具，并非原助手输出。字节一致不代表引用充分，也不能替代新增事实审计。</p></div></div><div class="card full"><h2>世界与分支清单</h2><div class="table-wrap"><table><thead><tr><th>世界</th><th>环境</th><th>干预臂</th><th>状态</th><th>共同画像与网络哈希</th></tr></thead><tbody id="worlds"></tbody></table></div></div></section>
<section class="section" id="replay"><div class="card"><h2>只读回放 · 已保存的工程事件</h2><p class="small">按 logical_seq 原序播放；不会重新请求模型。exposure_prepared 表示准备完成，不是 exposure_delivered。</p><div class="toolbar"><select id="world-filter" aria-label="世界筛选"><option value="">全部世界</option></select><select id="kind-filter" aria-label="事件类型筛选"><option value="">全部事件类型</option></select><button class="action primary" id="play">播放</button><button class="action" id="next">单步</button><button class="action" id="download">导出当前事件</button><span class="small" id="cursor"></span></div><div class="split"><div class="events" id="event-list"></div><div><h3 id="event-title">选择事件查看原始引用</h3><div id="context"></div><pre class="code" id="event-detail"></pre></div></div></div></section>
<section class="section" id="validation"><div class="grid"><div class="card"><h2>自动测试与完整性校验</h2><div id="test-results"></div></div><div class="card"><h2>下一步需要补充</h2><div class="step"><span class="num">1</span><div><h3>原助手源码与适配</h3><span class="small">源码已下载；以本页运行门槛为准核对适配验收，真实API与语义审核仍待完成。</span></div></div><div class="step"><span class="num">2</span><div><h3>模型服务与预算</h3><span class="small">受试 / 助手 / 评估器模型、参数及预实验费用上限。密钥仅放本机环境变量。</span></div></div><div class="step"><span class="num">3</span><div><h3>可读取数据与人工审核</h3><span class="small">恢复公开数据下载或提供合法下载的源文件；双人独立核验不能由自动测试替代。</span></div></div></div></div><div class="card full"><h2>全部运行门槛</h2><div id="all-issues"></div></div><div class="card full"><h2>复跑命令与产物</h2><pre class="code">python -m cocoon plan
python -m cocoon preflight
python -m cocoon validate
python -m cocoon dry-run
python -m cocoon verify --run-dir &lt;工程运行目录&gt;
python -m cocoon report --run-dir &lt;工程运行目录&gt;</pre><p class="small">从项目目录运行。preflight 未就绪返回退出码2；正式 run 入口保持关闭。每次验证保留独立产物，不覆盖既有记录。</p><p class="fileline" id="run-path"></p></div></section><div class="footer" id="footer"></div></main></div>
<script type="application/json" id="data">__PAYLOAD__</script><script>
'use strict';
const D=JSON.parse(document.getElementById('data').textContent),R=D.run,P=D.readiness.plan,E=D.events;
const $=id=>document.getElementById(id);const esc=s=>String(s??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const kpi=(label,value,note)=>`<div class="card"><div class="label">${label}</div><div class="value">${value}</div><div class="small">${note}</div></div>`;
$('kpis').innerHTML=kpi('核心方案 · 基础调用预算量',P.base_calls.toLocaleString(),'不含助手、盲评、检索及重试')+kpi('已执行真实模型调用','0','付费支出 ¥0；未做模型预实验')+kpi('工程事件已记录',R?R.verification.event_count.toLocaleString():'—','只读日志；内容哈希可重验')+kpi('正式认知观测','0','不推断真人效果或干预增益');
$('progress').innerHTML=[['01','配置与工程底座','已写入配置、排程、评分、快照与审计模块。',true],['02','离线集成验证',R?`${R.plan.exposure_worlds}个曝光世界、${R.plan.intervention_worlds}个分支，${R.prepared_contexts}个上下文已准备。`:'尚无集成运行记录。',!!R],['03','真实模型预实验','待接入源码、模型、预算与审核材料。',false],['04','冻结后正式运行',`${P.blocks}区组；T3 Brier误差为主要终点，两项比较按Holm校正。`,false]].map(([n,t,d,ok])=>`<div class="step"><span class="num">${n}</span><div><h3>${t} <span class="${ok?'done':'blocked'}">${ok?'工程就绪':'待验收'}</span></h3><span class="small">${d}</span></div></div>`).join('');
const issue=i=>`<div class="issue"><h3>${esc(i.title)}</h3><p>${esc(i.detail)}</p></div>`;
$('issues-short').innerHTML=D.readiness.blockers.filter(i=>['assistant_source','assistant_audit','assistant_live','pilot_budget','materials','oasis_integration'].includes(i.code)).map(issue).join('');$('all-issues').innerHTML=D.readiness.blockers.map(issue).join('');
$('run-history').innerHTML=(D.readiness.previous_runs||[]).map(r=>`<tr><td>${esc(r.run_id)}</td><td><span class="${r.status==='passed'?'done':'blocked'}">${({passed:'工程通过',failed:'失败已保留',incomplete:'未完成'})[r.status]}</span></td><td>${esc(r.detail)}</td></tr>`).join('');
const intake=D.readiness.intake;$('intake').innerHTML=intake?`<span class="blocked">${intake.raw_download_complete?'待人工核验':'下载未完成'}</span><p>已定位 AVeriTeC 原版来源；本机完整获取：<b>${intake.raw_download_complete?'是':'否'}</b>。候选数 ${intake.candidate_count}，正式材料 ${intake.formal_material_count}，人工审核 ${intake.human_reviews_completed}。</p><p class="small">${intake.error?'错误类型：'+esc(intake.error.type)+'。失败已保留，不替换为示例数据。':'底层证据权利与双人审核仍待完成。'}</p><p class="fileline">${esc(intake.url)}</p><p class="small">数据集声明：${esc(intake.license_declared)}；不代表所有底层网页都可重新分发。</p>`:'没有本机接入记录。';
if(R){const grouped={};for(const m of R.manipulation){if(m.phase==='exposure'&&!grouped[m.environment])grouped[m.environment]=m}for(const env of ['A','B','C','D']){const m=grouped[env],n=['A','B'].includes(env)?[2,2,2]:[4,1,1];$('quota').innerHTML+=`<tr><td>${env}</td><td><div class="meter">${n.map((v,i)=>`<span class="${['same','opposite','neutral'][i]}" style="width:${v/6*100}%">${v}</span>`).join('')}</div></td><td>${m.X.toFixed(1)}%</td><td>${(m.error_ratio*100).toFixed(1)}%</td></tr>`}$('worlds').innerHTML=R.worlds.map(w=>`<tr><td>${esc(w.world_id)}</td><td>${w.environment}</td><td>${w.arm||'—'}</td><td>仅准备</td><td title="${w.base_hash}">${w.base_hash.slice(0,16)}…</td></tr>`).join('');const good=R.packet_checks.filter(p=>p.evidence_equal&&p.dose_within_range&&p.length_matched).length;$('packets').innerHTML=`<div class="teststat">${good} / ${R.packet_checks.length} <small>包结构契约通过</small></div><p>证据ID、片段、来源及哈希一致；可见长度600–800字符，长度差不超过10%。</p><span class="blocked">语义审计仍待完成</span>`;$('run-path').textContent=R.run_directory}
const T=D.tests;$('test-results').innerHTML=T?`<div class="teststat">${T.tests_run-T.failures-T.errors} / ${T.tests_run}</div><p>失败 ${T.failures}，错误 ${T.errors}，跳过 ${T.skipped}。</p><span class="${T.success?'done':'blocked'}">${T.success?'本次验证通过':'存在未解决测试错误'}</span>`:'尚无测试报告。';if(R)$('test-results').innerHTML+=`<p>日志链：${R.verification.chain_valid?'通过':'未通过'}<br>原始内容文件：${R.verification.blob_count}<br>快照恢复检查：${R.snapshot_count}<br>各臂独立状态：通过工程断言</p><p class="small">只验证本地容器，不等于OASIS完整状态已经接入。</p>`;
$('footer').textContent='报告生成：'+D.generated_at+' · JSONL单进程追加 · 网络均值才是分析单位 · 原始运行不覆盖 · 示例不进入正式分析';
document.querySelectorAll('nav button').forEach(b=>b.addEventListener('click',()=>{document.querySelectorAll('nav button,.section').forEach(el=>el.classList.remove('on'));b.classList.add('on');$(b.dataset.section).classList.add('on')}));
for(const id of [...new Set(E.map(e=>e.world_id).filter(Boolean))]){const o=document.createElement('option');o.value=id;o.textContent=id;$('world-filter').append(o)}for(const k of [...new Set(E.map(e=>e.event_type))]){const o=document.createElement('option');o.value=k;o.textContent=k;$('kind-filter').append(o)}
let filtered=E,index=0,timer=null;
function stop(){if(timer)clearInterval(timer);timer=null;$('play').textContent='播放'}
function selectEvent(i){index=i;const e=filtered[index];$('cursor').textContent=filtered.length?`${index+1} / ${filtered.length}`:'无匹配事件';$('next').disabled=!e||index>=filtered.length-1;$('download').disabled=!e;if(!e){$('event-detail').textContent='无数据';$('context').replaceChildren();return}$('event-title').textContent=`#${e.logical_seq} · ${e.event_type}`;$('event-detail').textContent=JSON.stringify(e,null,2);const c=D.contexts[typeof e.input_ref==='object'?e.input_ref?.ref:e.input_ref];$('context').innerHTML=Array.isArray(c?.cards)?`<p class="small">本事件准备的上下文（未发送模型）</p>${c.cards.map(x=>`<div class="trace-card"><b>${esc(x.card_id)}</b><p>${esc(x.text)}</p><span class="small">${esc(x.source_url)}</span></div>`).join('')}`:'';document.querySelectorAll('.event').forEach(el=>el.classList.toggle('active',Number(el.dataset.index)===index));const active=document.querySelector('.event.active');if(active)active.scrollIntoView({block:'nearest',behavior:'auto'})}
function applyFilter(){stop();filtered=E.filter(e=>(!$('world-filter').value||e.world_id===$('world-filter').value)&&(!$('kind-filter').value||e.event_type===$('kind-filter').value));$('event-list').innerHTML=filtered.map((e,i)=>`<div class="event" data-index="${i}"><span class="seq">${e.logical_seq}</span><div><b>${esc(e.event_type)}</b><div class="small">${esc(e.world_id||'全局')} · ${esc(e.actor_id)} · ${esc(e.phase)}</div></div></div>`).join('');selectEvent(0)}
$('world-filter').onchange=applyFilter;$('kind-filter').onchange=applyFilter;$('event-list').onclick=evt=>{const el=evt.target.closest('.event');if(el){stop();selectEvent(Number(el.dataset.index))}};$('next').onclick=()=>{stop();selectEvent(Math.min(index+1,filtered.length-1))};$('play').onclick=()=>{if(timer){stop();return}if(!filtered.length)return;if(index>=filtered.length-1)index=-1;$('play').textContent='暂停';timer=setInterval(()=>{if(index>=filtered.length-1){stop();return}selectEvent(index+1)},850)};
$('download').onclick=()=>{const e=filtered[index];if(!e)return;const b=new Blob([JSON.stringify({event:e,prepared_context:D.contexts[typeof e.input_ref==='object'?e.input_ref?.ref:e.input_ref]||null},null,2)],{type:'application/json'}),u=URL.createObjectURL(b),a=document.createElement('a');a.href=u;a.download=`engineering_event_${e.logical_seq}.json`;a.click();setTimeout(()=>URL.revokeObjectURL(u),500)};applyFilter();
</script></body></html>'''


def render_cross_topic_review(path, feasibility_path, checklist_path, tests=None, *,
                              evidence_build, policy_path, expected_policy_sha,
                              recorded_alternative_path, recorded_run_directory,
                              live_semantic_path, independent_review_path=None) -> Path:
    """从源文件重验后新建静态待审页，不签署、不改写输入、不覆盖输出。

    evidence_build 可指定构建目录或其父根；实际构建目录只从 feasibility 的
    input_hashes.files 中该根下唯一的 manifest.json 定位，不扫描或猜测版本。
    可选独立意见须声明 review_type=independent_ai_not_human，支持两种字节绑定：
    顶层 feasibility_file_sha256 / checklist_file_sha256，或 input_sha256 中的
    feasibility.json / human_review_checklist.json。至少提供一种完整绑定；
    两种同时出现时必须全部匹配，不忽略冲突、缺项或无效结构。
    human_reviews_completed 必须为整数0，formal_eligible 必须为false。
    意见的状态仅展示，任何结论都不改变待审模板或正式资格。
    """
    import hashlib
    import os
    from urllib.parse import quote, urlsplit

    from .extractive_control import verify_extractive_feasibility
    from .human_review import validate_human_review_template
    from .runtime import _parse_json

    def require(condition, code):
        if not condition:
            raise ValueError("cross_topic_report:" + code)

    def local(value):
        text = str(value).replace("\\", "/")
        require(bool(text) and not text.startswith("//"), "local_path_required")
        return Path(text).resolve()

    def sha(value):
        return hashlib.sha256(value).hexdigest()

    path = local(path)
    if not path.parent.is_dir():
        raise FileNotFoundError(path.parent)
    if path.exists() or path.is_symlink():
        raise FileExistsError(path)
    feasibility_path, checklist_path = local(feasibility_path), local(checklist_path)
    feasibility_bytes, checklist_bytes = feasibility_path.read_bytes(), checklist_path.read_bytes()
    data, checklist = _parse_json(feasibility_bytes), _parse_json(checklist_bytes)
    require(type(data) is dict and type(checklist) is dict, "invalid_documents")
    tracked = {feasibility_path: sha(feasibility_bytes), checklist_path: sha(checklist_bytes)}
    independent = None
    if independent_review_path is not None:
        independent_review_path = local(independent_review_path)
        independent_bytes = independent_review_path.read_bytes()
        independent = _parse_json(independent_bytes)
        require(type(independent) is dict
                and independent.get("review_type") == "independent_ai_not_human"
                and type(independent.get("human_reviews_completed")) is int
                and independent["human_reviews_completed"] == 0
                and independent.get("formal_eligible") is False, "independent_review_binding_mismatch")
        flat_present = any(key in independent for key in (
            "feasibility_file_sha256", "checklist_file_sha256"))
        nested_present = "input_sha256" in independent
        require(flat_present or nested_present, "independent_review_binding_mismatch")
        # 不用二选一的 or 放行：同时声明的任一种绑定若错误，都必须拒绝。
        if flat_present:
            require(independent.get("feasibility_file_sha256") == tracked[feasibility_path]
                    and independent.get("checklist_file_sha256") == tracked[checklist_path],
                    "independent_review_binding_mismatch")
        if nested_present:
            bindings = independent["input_sha256"]
            require(type(bindings) is dict
                    and bindings.get("feasibility.json") == tracked[feasibility_path]
                    and bindings.get("human_review_checklist.json") == tracked[checklist_path],
                    "independent_review_binding_mismatch")
        tracked[independent_review_path] = sha(independent_bytes)

    root = local(evidence_build)
    manifests = []
    for name, digest in data["input_hashes"]["files"].items():
        candidate = Path(str(name).replace("\\", "/"))
        if candidate.name == "manifest.json" and candidate.is_relative_to(root):
            resolved = local(candidate)
            require(resolved.is_relative_to(root), "manifest_outside_root")
            manifests.append((resolved, digest))
    require(len(manifests) == 1, "unique_source_manifest_required")
    manifest, manifest_sha = manifests[0]
    require(manifest_sha == data["input_hashes"]["manifest_sha256"], "manifest_binding_mismatch")
    build = manifest.parent
    policy_path, live_semantic_path = local(policy_path), local(live_semantic_path)
    recorded_alternative_path = None if recorded_alternative_path is None else local(recorded_alternative_path)
    recorded_run_directory = None if recorded_run_directory is None else local(recorded_run_directory)
    require(validate_human_review_template(checklist, build, live_semantic_path) is True,
            "human_template_invalid")
    require(verify_extractive_feasibility(
        data, build, policy_path, expected_policy_sha,
        recorded_alternative_path=recorded_alternative_path,
        recorded_run_directory=recorded_run_directory,
    ) is True, "feasibility_invalid")
    for inputs in (data["input_hashes"]["files"], checklist["source_inputs"]):
        for name, digest in inputs.items():
            source = local(name)
            require(source not in tracked or tracked[source] == digest, "input_binding_conflict")
            tracked[source] = digest

    def esc(value):
        if value is None:
            value = "未记录"
        elif isinstance(value, (dict, list, bool)):
            value = json.dumps(value, ensure_ascii=False, allow_nan=False)
        return html.escape(str(value), quote=True)

    def paragraph(label, value):
        return f'<p><b>{esc(label)}</b> {esc(value)}</p>'

    def raw(value):
        return '<pre>' + esc(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)) + '</pre>'

    def fold(title, content):
        return f'<details><summary>{esc(title)}</summary><div>{content}</div></details>'

    def table(headers, rows):
        head = ''.join(f'<th scope="col">{esc(v)}</th>' for v in headers)
        body = ''.join('<tr>' + ''.join(f'<td>{esc(v)}</td>' for v in row) + '</tr>' for row in rows)
        return f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'

    def ranges(values):
        return '、'.join(f'{low}–{high}' for low, high in values) or '无'

    def source_link(value):
        text = str(value)
        try:
            parts = urlsplit(text)
            safe = (parts.scheme == "https" and bool(parts.hostname) and not parts.username
                    and not parts.password and parts.port in (None, 443)
                    and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 or c in '\\<>"\'' for c in text))
        except ValueError:
            safe = False
        if not safe:
            return f'<span>未启用的来源链接：{esc(text)}</span>'
        return f'<a href="{esc(text)}" target="_blank" rel="noopener noreferrer" referrerpolicy="no-referrer">{esc(text)}</a>'

    def download(source, label):
        relative = os.path.relpath(source, path.parent).replace("\\", "/")
        require(not Path(relative).is_absolute(), "relative_download_required")
        href = './' + quote(relative, safe="/")
        return f'<a href="{esc(href)}" download="{esc(source.name)}">{esc(label)} · {esc(source.name)}</a>'

    def evidence_refs(identifiers):
        return '、'.join(f'<a href="#{quote("EV_" + eid, safe="")}">{esc(eid)}</a>' for eid in identifiers)

    def writing_slots(item):
        columns = []
        for key, label in (("reviewer_1", "真实审核者 A"), ("reviewer_2", "真实审核者 B")):
            checks = ''.join(f'<li>{esc(check)}：________</li>' for check in item[key]["checks"])
            columns.append(f'''<section class="reviewer"><h4>{label}</h4>
<p>身份：________________　日期：________________</p><ul>{checks}</ul>
<p>decision／决定：________________</p><p>notes／独立意见：</p>
<div class="write-line">________________________________________________</div>
<div class="write-line">________________________________________________</div>
<div class="write-line">________________________________________________</div></section>''')
        return '<div class="review-columns">' + ''.join(columns) + '''</div>
<div class="adjudication"><p><b>分歧裁决（独立第三人）</b> 身份：________________　日期：________________</p>
<p>decision／裁决：________________　reason／理由：</p>
<div class="write-line">________________________________________________________________________________</div>
<div class="write-line">________________________________________________________________________________</div></div>'''

    cases = data["cases"]
    by_packet = {case["packet_sha256"]: case for case in cases}
    sources = {item["source_id"]: item for item in checklist["source_reviews"]}
    case_rows, case_panels = [], []
    for case in cases:
        actual = (f'{case["actual_organized_total_chars"]} / {case["actual_plain_total_chars"]}（旧探索性候选）'
                  if case["actual_pair_available"] else '待测：缺实际组织输出')
        case_rows.append((case["case_id"], case["title"], case["fixed_chars"],
                          ranges([case["allowed_org_range"]]) if case["allowed_org_range"] else '无',
                          ranges(case["feasible_organized_ranges"]),
                          f'{case["dose_eligible_combinations"]} / {case["total_combinations"]}', actual))
        sentences = []
        for pool in case["sentence_pools"]:
            entries = ''.join(
                '<article class="sentence">'
                + paragraph(f'原句索引 {sentence["sentence_index"]} · offset [{sentence["start"]}, {sentence["end"]})', sentence["text"])
                + paragraph('选择规则', '可枚举' if sentence["selector_eligible"] else '问句排除，不参与组合')
                + '</article>' for sentence in pool["sentences"])
            sentences.append(f'<h4>{esc(pool["evidence_id"])}</h4>{entries}'
                             + paragraph('来源家族', pool["source_family_id"])
                             + source_link(pool["source_url"]))
        combos = table(
            ('组合索引（从0起）', '原句索引', 'offset [start,end)', '摘录附加字符', '摘录整体字符',
             '整体剂量合格', 'matching 组织侧附加区间'),
            [(combo["combination_index"], combo["sentence_indices"],
              '；'.join(f'{o["evidence_id"]} [{o["start"]}, {o["end"]})' for o in combo["offsets"]),
              combo["plain_additional_chars"], combo["plain_visible_chars"],
              '是' if combo["dose_in_range"] else '否', ranges(combo["matching_organized_ranges"]))
             for combo in case["candidate_combinations"]])
        body = paragraph('实际状态', actual) + paragraph('议题时间边界', case["title_metadata"]["time_scope"])
        body += paragraph('已绑定运行', case["actual_source_run_id"]) + paragraph('已绑定事件', case["actual_source_event_ids"])
        body += '<p>offset 是冻结中文证据正文内 Python Unicode 码点偏移，左闭右开；不是英文来源全文偏移。每条证据取一整句，按冻结顺序选首个合格组合，不按态度择优。</p>'
        body += ''.join(sentences) + '<h4>完整组合表</h4>' + combos
        case_panels.append(f'<article id="{esc(case["case_id"])}">' + fold(case["case_id"] + ' · 原句与组合审计', body) + '</article>')

    groups = (("evidence_reviews", "证据"), ("source_reviews", "来源"),
              ("model_issue_reviews", "模型问题"), ("round_reviews", "轮次"), ("release_gates", "门槛"))
    group_panels = []
    for group, label in groups:
        cards = []
        for item in checklist[group]:
            body = ''
            if group == "evidence_reviews":
                source = sources[item["source_id"]]
                body += paragraph('中文释义', item["text"]) + paragraph('来源短引文', item["original_quote"])
                body += paragraph('引文边界', f'[{item["quote_start"]}, {item["quote_end"]}) · {item["quote_offset_unit"]}')
                body += paragraph('语境与解释边界', item["boundary_note"])
                body += paragraph('审核维度 / 转换类型', [item["review_dimension"], item["transformation_type"]])
                body += paragraph('来源 / 发布日期', [item["title"], item["publication_date"]])
                body += paragraph('取证时间（非发布或观测日期）', source["metadata"]["retrieved_at_utc"])
                body += source_link(item["url"]) + paragraph('引用预算', item["quote_budget"])
            elif group == "source_reviews":
                body += paragraph('来源', item["title"]) + source_link(item["url"])
                body += paragraph('来源级别 / 家族', [item["source_level"], item["source_family_id"]])
                body += paragraph('发布日期 / 取证时间', [item["publication_date"], item["metadata"]["retrieved_at_utc"]])
                body += paragraph('同家族来源（不证明独立）', item["same_family_source_ids"])
                body += paragraph('许可与再分发说明', item["license_note"])
                body += '<p>关联证据：' + evidence_refs(item["evidence_ids"]) + '</p>'
                body += fold('来源元数据与本地取证记录', raw(item["metadata"]) + raw(item["capture_metadata"]))
            elif group == "model_issue_reviews":
                body += paragraph('原模型条目位置', [item["stage"], item["section"], item["item_index"]])
                body += paragraph('原模型精确文本（未改写）', item["text"])
                body += '<p>原引用：' + evidence_refs(item["citations"]) + '</p>'
                body += paragraph('原认识状态', item["epistemic_status"])
                body += fold('初步AI意见，非人工裁决', paragraph('初步分类 / 严重程度', [item["verdict"], item["severity"]])
                             + paragraph('AI理由', item["reason"]))
                body += fold('原输出与请求绑定', raw({key: item[key] for key in (
                    "original_output_descriptor", "stage_input_descriptor", "stage_response_descriptor", "packet_descriptor")}))
            elif group == "round_reviews":
                case = by_packet[item["packet_sha256"]]
                body += paragraph('议题 / 轮次 / 立场轴', [item["topic_id"], item["round"], item["stance_axis"]])
                body += '<p>关联证据：' + evidence_refs(item["evidence_ids"]) + '</p>'
                body += f'<p>对应 <a href="#{esc(case["case_id"])}">{esc(case["case_id"])}</a> 的原句和组合审计；仅展示交叉映射，不回写模板。</p>'
                body += paragraph('实际输出', '仅绑定旧探索性候选' if case["actual_pair_available"] else '待测：缺实际组织输出')
                body += paragraph('剂量核查要求（不是通过结果）', item["dose"])
                body += paragraph('当前边界', item["note"])
                body += fold('完整本轮中文证据', ''.join(paragraph(entry["evidence_id"], entry["text"]) for entry in item["evidence"]))
            else:
                body += paragraph('门槛', item["title"]) + paragraph('必须执行的动作', item["required_action"])
                body += paragraph('范围', item["scope"]) + '<p>不自动完成；完成证据和实际负责人须由人另行记录。</p>'
            body += fold('本卡源输入绑定', raw(item["source_input_descriptor"]))
            cards.append(f'<article class="review-card" id="{esc(item["id"])}"><h3>{esc(item["id"])}</h3>'
                         + body + writing_slots(item) + '</article>')
        group_panels.append(f'<section class="review-group"><h2>{esc(label)} · {len(cards)} 项</h2>' + ''.join(cards) + '</section>')

    test_note = '本页生成时未附全项目结果'
    if tests is not None:
        for key in ("tests_run", "failures", "errors"):
            require(type(tests[key]) is int and tests[key] >= 0, "invalid_test_counts")
        skipped = tests.get("skipped", 0)
        require(type(skipped) is int and skipped >= 0
                and tests["failures"] + tests["errors"] + skipped <= tests["tests_run"], "invalid_test_counts")
        test_note = (f'随页提供的测试记录：{tests["tests_run"]} 项，失败 {tests["failures"]}，'
                     f'错误 {tests["errors"]}，跳过 {skipped}；本页不执行测试，不推定为全项目结果。')
    independent_panel = '<p>未附AI复核：未提供独立跨轮语境审核文件；已有模型条目的初步意见不等于本次跨轮复核。</p>'
    if independent is not None:
        independent_panel = '<p>已附独立AI语境意见，并核对两个输入文件的 SHA256；哈希绑定不是数字签名，不证明意见正确，不替代人工裁决。</p>'
        independent_panel += fold('独立AI语境审核全文（非人工裁决，不升级状态）', raw(independent))
    summary_table = table(('case', '议题', '固定字符', '允许组织侧附加区间', '可行组织侧附加区间',
                           '剂量合格 / 总组合', '实际整体字符：组织 / 摘录'), case_rows)
    total = sum(len(checklist[group]) for group, _ in groups)
    counts_note = ' / '.join(f'{len(checklist[group])} {label}' for group, label in groups)
    hash_rows = [(str(source), digest[:16] + '…') for source, digest in sorted(tracked.items(), key=lambda pair: str(pair[0]))]
    styles = '''
:root{color-scheme:light}*{box-sizing:border-box}body{margin:0;background:#fff;color:#202b38;font:15px/1.85 "Microsoft YaHei",system-ui,sans-serif}
main{max-width:1200px;margin:auto;padding:32px 28px}h1{font-size:30px;line-height:1.4}h2{font-size:22px;margin:30px 0 14px}h3{font-size:17px}h4{font-size:15px;margin:14px 0 7px}
p{margin:10px 0;white-space:pre-wrap;overflow-wrap:anywhere}a,summary{color:#28547b}a{overflow-wrap:anywhere}summary{cursor:pointer;font-weight:600;padding:10px 0}
.notice,.stats{padding:18px 22px;background:#f2f5f8;border-left:4px solid #627e99;margin:20px 0}.stats{font-size:18px}.small,footer{color:#4c5d70;font-size:13px}
.table-wrap{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:11px 9px;border:1px solid #ccd6df;text-align:left;vertical-align:top;overflow-wrap:anywhere}th{background:#edf2f6}
details{border-top:1px solid #d6dfe7;margin:12px 0}details>div{padding:8px 0 14px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.8 Consolas,"Microsoft YaHei",monospace;background:#f5f7fa;padding:16px}
.review-card{border:1px solid #cbd6df;border-radius:8px;margin:22px 0;padding:22px}.sentence{padding:8px 14px;border-left:2px solid #b7c8d8;margin:12px 0}
.review-columns{display:grid;grid-template-columns:1fr 1fr;gap:26px;margin-top:20px}.reviewer{min-width:0;border-top:2px solid #8ca3b7;padding-top:8px}.reviewer ul{padding-left:22px}
.write-line{min-height:36px;border-bottom:1px solid #8b9aa8;color:transparent;overflow:hidden;white-space:nowrap;user-select:text}.adjudication{margin-top:22px;border-top:1px solid #cbd6df;padding-top:12px}footer{border-top:1px solid #cbd6df;margin-top:30px;padding-top:18px}
@media screen and (max-width:760px){main{padding:18px 14px}.review-columns{grid-template-columns:1fr}.review-card{padding:16px}}
@media print{
@page{margin:14mm}body{background:#fff;color:#111;font-size:10pt}main{max-width:none;padding:0}h2,h3,h4,summary{break-after:avoid}
details,details[open]{display:block!important}details>*,details:not([open])>*{display:block!important}
details::details-content{display:block!important;content-visibility:visible!important;height:auto!important}
.table-wrap,pre{overflow:visible!important;max-height:none!important}table{font-size:9pt}thead{display:table-header-group}tr,.sentence,.reviewer,.adjudication{break-inside:avoid}
.review-card{break-inside:auto;border-radius:0;padding:12px}.review-columns{display:grid;grid-template-columns:1fr 1fr;gap:18px}.write-line{min-height:10mm}
a{color:#183a59;text-decoration:underline}summary{color:#111}footer{color:#333}
}
'''
    page = f'''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<meta name="referrer" content="no-referrer"><title>跨轮长度可行性与人审准备</title><style>{styles}</style></head>
<body><main><header><h1>跨轮长度可行性与人审准备</h1>
<div class="notice"><b>candidate source packet：冻结中文释义或改编正文的整句，不是英文 source 逐字原句。</b>
<p>人工完成 {esc(checklist["human_reviews_completed"])}；formal_eligible={esc(data["formal_eligible"])}，非正式批准。
无真实自然 T1，不是完整 I4。原 v3 失败没有被修复，旧运行仍保留失败。</p></div>
<p>本页不是签署系统，不会保存填写，不会持久化审核意见，也不会修改任何审核状态。请打印或复制到独立文档填写；浏览器 Ctrl+P 打印会展开全部折叠内容。</p>
<p>两名不同的真实审核者各自下载同一原模板，手动另存为 reviewerA.json 与 reviewerB.json 并独立填写；本页不会创建这两份文件。分歧另交独立第三人裁决，不能同人双签，AI不能充任人工审核者。已填写文件须另行核验，不能用初始模板校验冒充完成判定。</p>
<p>{download(feasibility_path, '下载可行性原JSON')}<br>{download(checklist_path, '下载人审原JSON（A、B使用同一模板，各自另存）')}</p>
<p class="small">仅提供两个现有 JSON 文件链接；原始来源网页和全文不打包。来源URL是审核者审计入口，不计入受试消息；只有主动点击才访问，本页不自动获取外部资源。</p></header>
<section id="cross-topic"><h2>A · 跨 {len(cases)} 轮结论摘要</h2>
<div class="stats">{sum(c["conditional_length_feasible"] for c in cases)} 有条件可行 / {sum(c["actual_pair_available"] for c in cases)} 已绑定旧探索性候选 / {sum(not c["actual_organized_output_available"] for c in cases)} 缺实际组织输出 / 新增请求 {esc(data["new_model_calls"])}</div>
<p>有条件可行只指存在可匹配的整数长度，不代表已生成或已通过语境审核。实际字符只展示绑定旧候选的轮次，其他轮次保持待测。</p>
{summary_table}
<p>range 是附加段字符区间，包含引文标记字符，不是 600–800 的完整消息区间；固定字符另含共同证据、标签及回应提示。组合表的 matching ranges 同样指组织侧附加段。
剂量合格 / 总组合只列完整消息剂量枚举数量，不是模型概率或语义通过结论。可行区间还同时约束两侧整体及附加段的长度匹配。</p>
{''.join(case_panels)}<h3>独立AI语境审核</h3>{independent_panel}</section>
<section id="human-review"><h2>B · 人审清单 {total} 项</h2><p>{esc(counts_note)}。所有决定、意见和裁决均留白；表内检查项不是已签署意见。</p>
{''.join(group_panels)}</section><footer><p>{esc(test_note)}</p>
<p>输入指纹（SHA256短显示）：可行性 {sha(feasibility_bytes)[:16]}…；人审模板 {sha(checklist_bytes)[:16]}…；源 manifest {esc(manifest_sha[:16])}…。</p>
{fold('全部本地输入指纹（非数字签名）', table(('本地输入', 'SHA256前16位'), hash_rows))}
<p>已从源路径重建可行性与未填写模板，不仅信任JSON摘要或passed字段。哈希只绑定本地版本，不证明来源真实、语义正确或人工批准；来源URL仅供审核，不是受试材料。本页不调用模型、不联网、不保存填写。</p></footer></main></body></html>'''
    for source, digest in tracked.items():
        require(sha(source.read_bytes()) == digest, "inputs_changed_during_render")
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(page)
    return path
