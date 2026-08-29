// The fleet page's expanded-state and ordering logic, run against the REAL
// hub/static/hub.js.
//
// WHY THIS EXISTS. hub.js is 189 lines of the only thing an operator looks at
// when something is wrong, and until this file nothing in the repository
// executed a single line of it. `worry()` alone decides the whole page - which
// node sorts to the top, and whether the headline says "All quiet" - and a
// silent change to it would be invisible until the day it mattered. That is the
// same shape as every other gap this repo has closed: the Go datapath, the
// bond-agent suite and the hub's own Python all appeared in no workflow at all
// until somebody checked.
//
// WHAT IT DOES NOT DO. It does not render HTML, and it is not a browser. The
// shim below implements only what render() and nodeRow() touch. Anything that
// depends on real layout, real CSS or real <details> semantics beyond the
// `open` property and its `toggle` event is out of reach here and needs a
// browser.
//
// WHY TESTING hub/static/hub.js IS TESTING WHAT SHIPS. The file is copied into
// the zippie-hub ConfigMap at deploy/oke/zippie-hub/hub-app/hub.js, and
// test_hub_app_copy_in_sync.py compares the two by sha and fails if they
// diverge. Without that guard this would be testing a file the cluster never
// serves.
//
// CALIBRATED AGAINST THE LIVE PAGE, 2026-08-10 (#56). The first six checks
// below assert behaviour that was measured in Chrome against the running hub
// before this file was written - expanding `the travel router` and sampling across four poll
// cycles gave open=true at t=5.3/11.3/17.3/23.3s, each time with
// sameNode=false. The shim reproduces that, which is the reason to believe it
// about the last three, where a browser cannot reach: `expanded` is module
// scope in an ES module, and an extension's injected JavaScript runs in an
// isolated world where patching window.fetch does not touch the page at all.
//
// Run: node hub/tests/hub_js.mjs   (exit 0 = pass; the CI job is `hub js`)
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SRC = path.join(HERE, '..', 'static', 'hub.js');

/** The smallest element that render() and nodeRow() can tell from a real one. */
function makeEl(tag) {
  const el = {
    tagName: tag, className: '', title: '', textContent: '',
    children: [], _l: {}, _open: false,
    append(...kids) { this.children.push(...kids); },
    addEventListener(type, fn) { (this._l[type] ||= []).push(fn); },
    // nodeRow only ever sets textContent on what it finds, so one generic stub
    // per lookup is enough and a selector engine would be scaffolding.
    querySelector() { return makeEl('span'); },
    set innerHTML(_v) {},
    get innerHTML() { return ''; },
  };
  Object.defineProperty(el, 'open', {
    get() { return this._open; },
    // A real <details> fires `toggle` only when the property CHANGES, and fires
    // it ASYNCHRONOUSLY. Both matter: hub.js assigns .open and attaches its
    // listener on the next line, so a synchronous shim would never deliver the
    // event and would hide the difference between "re-applied from the set" and
    // "never registered at all".
    set(v) {
      const changed = this._open !== !!v;
      this._open = !!v;
      if (changed) queueMicrotask(() => (this._l.toggle || []).forEach((f) => f()));
    },
  });
  return el;
}

const byId = {};
for (const id of ['headline', 'subhead', 'nodes', 'stamp']) {
  byId[id] = makeEl('div');
  byId[id].replaceChildren = function (...kids) { this.children = kids; };
}
globalThis.document = { createElement: makeEl, getElementById: (id) => byId[id] };
// hub.js calls tick() at import time and then every 5 s. Neither is under test
// here, and a live interval would keep the process alive forever.
globalThis.fetch = async () => ({ ok: true, json: async () => ({ nodes: [] }) });
globalThis.setInterval = () => 0;

// The source is imported UNMODIFIED, with one line appended to reach the module
// scope. Rewriting it to add exports would mean testing a file that is not the
// one the hub serves.
const src = fs.readFileSync(SRC, 'utf8');
const probe = '\nglobalThis.__probe = { render, expanded, worry, stateWord, legClass };\n';
await import('data:text/javascript;base64,'
  + Buffer.from(src + probe).toString('base64'));
const { render, expanded, worry, stateWord, legClass } = globalThis.__probe;

const settle = () => new Promise((r) => setTimeout(r, 0));
const node = (name, extra = {}) => ({
  name, label: name, kind: 'router', staleMs: 0, carrying: 1,
  degraded: false, unreachable: false, legs: [], ...extra,
});
const rows = () => byId.nodes.children.map((li) => li.children[0]);
const openNames = () => [...expanded].sort();

const results = [];
function check(name, got, want) {
  results.push({ name, ok: JSON.stringify(got) === JSON.stringify(want), got, want });
}

// ---------------------------------------------------- expanded state (#56)
render([node('travel-router')]);
await settle();
check('a row starts collapsed', rows()[0].open, false);

rows()[0].open = true;
await settle();
check('a click registers in the set', openNames(), ['travel-router']);

render([node('travel-router')]);
render([node('travel-router')]);
render([node('travel-router')]);
await settle();
check('it survives three polls (live: open=true at 23.3 s)', rows()[0].open, true);

rows()[0].open = false;
await settle();
render([node('travel-router')]);
await settle();
check('a collapse persists too, which is the same bug with the opposite sign',
      rows()[0].open, false);

// `ordered` sorts by worry, so a node changing state MOVES. Keying the set on
// position instead of name would hand one node's open state to whatever took
// its slot.
render([node('alpha'), node('beta')]);
await settle();
rows()[1].open = true;
await settle();
check('opening the second row opens only that one', openNames(), ['beta']);
render([node('alpha', { carrying: 0 }), node('beta')]);
await settle();
check('after a reorder the state stays with its own node',
      rows().map((d) => d.open), [false, true]);

// Departure. Without the prune the set grows forever on a hub that has been up
// for months and seen phones come and go - and a returning name springs open on
// its own.
rows()[0].open = true;
await settle();
check('both rows open before the departure', openNames(), ['alpha', 'beta']);
render([node('beta')]);
await settle();
check('a departed node is pruned from the set', openNames(), ['beta']);

render([]);
await settle();
check('an empty fleet empties the set', openNames(), []);

render([node('beta')]);
await settle();
check('a returning node comes back collapsed', rows()[0].open, false);

// --------------------------------------------------------- ordering (#41)
// worry() IS the design: it decides what an operator sees first. Lower sorts
// higher. Degraded-but-carrying is deliberately NOT urgent - legs on this bond
// read degraded routinely, and counting that as needing attention made the
// headline permanently alarming, which is how a number stops being read.
check('a node that cannot be seen sorts first', worry(node('x', { unreachable: true })), 0);
check('one that stopped reporting is next', worry(node('x', { staleMs: 60_000 })), 1);
check('carrying nothing is next', worry(node('x', { carrying: 0 })), 2);
check('degraded but carrying is NOT urgent', worry(node('x', { degraded: true })), 8);
check('quiet and fine sorts last', worry(node('x')), 9);
check('unreachable beats stale', worry(node('x', { unreachable: true, staleMs: 60_000 })), 0);

render([node('quiet'), node('dead', { unreachable: true }), node('degraded', { degraded: true })]);
await settle();
check('the headline counts only what needs attention',
      byId.headline.textContent, '1 needing attention');
render([node('quiet'), node('degraded', { degraded: true })]);
await settle();
check('degraded alone does not raise the headline', byId.headline.textContent, 'All quiet');

// A leg with weight but ejected from the bond is NOT carrying. Weight alone is
// not membership: a tier-gated leg keeps whatever weight it last had.
check('a weighted leg in the bond carries',
      legClass({ effective_weight: 5, state: 'up', in_bond: true }), 'carrying');
check('a weighted leg OUT of the bond does not',
      legClass({ effective_weight: 5, state: 'up', in_bond: false }), '');
check('a shed leg that is down still reads down',
      legClass({ effective_weight: 0, state: 'down', in_bond: false }), 'down');

for (const r of results) {
  console.log(`${r.ok ? 'pass' : 'FAIL'}  ${r.name}`
    + (r.ok ? '' : `\n        got ${JSON.stringify(r.got)}  want ${JSON.stringify(r.want)}`));
}
const failed = results.filter((r) => !r.ok).length;
console.log(`\n${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
