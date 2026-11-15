// Offline DOM contract check; not a browser rendering or screenshot test.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[2], 'utf8');
const scripts = [...source.matchAll(/<script([^>]*)>([\s\S]*?)<\/script>/g)];
const data = scripts.find(x => x[1].includes('application/json'))[2];
const program = scripts.find(x => !x[1].includes('application/json'))[2];
const elements = new Map();
function element(id) {
  if (elements.has(id)) return elements.get(id);
  const node = {id, innerHTML:'', textContent:'', value:'', disabled:false, children:[], dataset:{}, listeners:{},
    classList:{add(){}, remove(){}, toggle(){}},
    append(child){ this.children.push(child); }, replaceChildren(){this.innerHTML='';},
    addEventListener(kind,fn){this.listeners[kind]=fn;}, click(){this.onclick?.();}, scrollIntoView(){}};
  elements.set(id,node); return node;
}
for (const match of source.matchAll(/\bid="([^"]+)"/g)) element(match[1]);
element('data').textContent=data;
const nav = ['overview','design','replay','validation'].map(id=>Object.assign(element('nav-'+id),{dataset:{section:id}}));
let interval = null;
const sandbox = {console,Blob,URL,
  document:{getElementById:id=>element(id),createElement:tag=>element('dynamic-'+tag+'-'+elements.size),
    querySelectorAll:selector=>selector==='nav button'?nav:selector==='nav button,.section'?[...nav,...nav.map(n=>element(n.dataset.section))]:[],
    querySelector:()=>null},
  setInterval:fn=>{interval=fn;return 1;}, clearInterval:()=>{interval=null;}, setTimeout:fn=>fn()};
vm.runInNewContext(program,sandbox,{timeout:15000});
assert.ok(element('kpis').innerHTML.includes('25,344'));
assert.equal((element('worlds').innerHTML.match(/<tr>/g)||[]).length,20);
assert.equal((element('quota').innerHTML.match(/<tr>/g)||[]).length,4);
assert.ok(element('run-history').innerHTML.includes('失败已保留'));
assert.ok(element('packets').innerHTML.includes('32 / 32'));
assert.equal(JSON.parse(element('event-detail').textContent).logical_seq,1);
element('kind-filter').value='exposure_prepared';element('kind-filter').onchange();
assert.equal(JSON.parse(element('event-detail').textContent).event_type,'exposure_prepared');
assert.equal((element('context').innerHTML.match(/class="trace-card"/g)||[]).length,6);
const before=JSON.parse(element('event-detail').textContent).logical_seq;
element('next').onclick();assert.ok(JSON.parse(element('event-detail').textContent).logical_seq>before);
element('play').onclick();assert.ok(interval);interval();element('play').onclick();assert.equal(interval,null);
element('download').onclick();
for(const n of nav)n.listeners.click();
element('world-filter').value='no_such_world';element('world-filter').onchange();
assert.equal(element('cursor').textContent,'无匹配事件');assert.ok(element('download').disabled);
console.log(JSON.stringify({ok:true,checks:['initial_render','20_worlds','4_quotas','failure_visible','32_equal_packets','event_selection','6_card_context','step','play_pause','export','navigation','empty_filter'],scope:'Node offline DOM contract, not visual browser rendering'}));
