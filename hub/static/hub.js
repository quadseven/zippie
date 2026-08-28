// The hub's only job is ORDERING. Everything else is the zippie system applied.
//
// A fleet dashboard is where "absence of bad news is good news" is easiest to
// get wrong: one stale card among twenty green ones is invisible. So a node
// that has not reported sorts to the TOP with the failures, never into the
// quiet tail.

const STALE_AFTER_MS = 30_000;

// WHICH NODES THE READER HAS OPENED, kept OUTSIDE the DOM on purpose.
//
// render() rebuilds the whole list every poll with replaceChildren, and
// nodeRow() returns a fresh <details> each time. So a node the reader had
// opened was not being collapsed - it was being DESTROYED and replaced by a
// closed one, within 5 seconds, every time. Measured in the live page
// 2026-08-08: t=0 open=true, t=3s open=false with sameNode=false. Reported as
// "try clicking, it auto closes" (#56), which is exactly what it looked like.
//
// Keyed on node.name and NOT on position: `ordered` sorts by worry, so a node
// changing state moves in the list, and an index-keyed set would hand one
// node's open state to whichever node took its slot.
const expanded = new Set();

/** Worry, high first. The whole design is this function.
 *
 * DEGRADED-BUT-CARRYING IS NOT WORRY. Legs on this bond read degraded
 * routinely - it is the anti-flap gate and a wireless uplink doing its job -
 * so counting it as needing attention made the headline permanently alarming.
 * That is the crying-wolf failure the design system forbids by name: a person
 * who sees "3 needing attention" every time they look stops reading the
 * number. It still sorts ABOVE the fully quiet, because it is the first thing
 * to look at when something does go wrong.
 */
function worry(node) {
  if (node.unreachable) return 0;              // cannot be seen at all
  if (node.staleMs > STALE_AFTER_MS) return 1; // reporting stopped
  if (node.carrying === 0) return 2;           // present and carrying nothing
  if (node.degraded) return 8;                 // holding on - notable, not urgent
  return 9;                                    // fine, and quiet about it
}

/** What the headline counts. Deliberately narrower than the sort order. */
const NEEDS_ATTENTION = 3;

function node_bad(n) { return worry(n) < NEEDS_ATTENTION; }

function stateWord(node) {
  if (node.unreachable) return 'not answering';
  if (node.staleMs > STALE_AFTER_MS) return 'stopped reporting';
  if (node.carrying === 0) return 'carrying nothing';
  if (node.degraded) return 'carrying, degraded';
  return 'carrying';
}

/** A leg's tick class. Weight alone is NOT membership - a tier-gated leg keeps
 *  whatever weight it last had and carries nothing. */
function legClass(leg) {
  const carrying = (leg.effective_weight || 0) > 0 && (leg.in_bond !== false);
  if (carrying) return leg.state === 'degraded' ? 'degraded' : 'carrying';
  if (leg.state === 'down') return 'down';
  return '';
}

function age(ms) {
  if (ms == null) return 'never';
  const s = Math.round(ms / 1000);
  if (s < 2) return 'just now';
  if (s < 90) return `${s}s ago`;
  return `${Math.round(s / 60)}m ago`;
}

function render(nodes) {
  const ordered = [...nodes].sort((a, b) => worry(a) - worry(b) || a.name.localeCompare(b.name));
  const needing = ordered.filter((n) => worry(n) < NEEDS_ATTENTION).length;

  document.getElementById('headline').textContent =
    needing === 0 ? 'All quiet' : `${needing} needing attention`;
  const degraded = ordered.filter((n) => !node_bad(n) && n.degraded).length;
  document.getElementById('subhead').textContent =
    needing > 0
      ? 'Sorted by how much attention each one needs.'
      : degraded > 0
        ? `${ordered.length} node${ordered.length === 1 ? '' : 's'} carrying. `
          + `${degraded} holding on, which is normal for a wireless uplink.`
        : `${ordered.length} node${ordered.length === 1 ? '' : 's'} carrying.`;

  // Forget nodes that have left the fleet, or the set grows forever on a box
  // that has been up for months and has seen phones come and go.
  const present = new Set(ordered.map((n) => n.name));
  for (const name of expanded) if (!present.has(name)) expanded.delete(name);

  const list = document.getElementById('nodes');
  list.replaceChildren(...ordered.map(nodeRow));
  document.getElementById('stamp').textContent = `Updated ${age(0)}`;
}

function nodeRow(node) {
  const li = document.createElement('li');
  const d = document.createElement('details');
  d.className = 'node';
  // Re-apply what the reader chose, BEFORE the element is in the document, so
  // the row never renders closed for a frame and then pop open.
  d.open = expanded.has(node.name);
  // `toggle` fires for both directions, so collapsing persists too - without
  // the delete, a node closed by the reader would spring back open on the next
  // poll, which is the same bug wearing the opposite sign.
  d.addEventListener('toggle', () => {
    if (d.open) expanded.add(node.name);
    else expanded.delete(node.name);
  });

  const s = document.createElement('summary');
  const word = stateWord(node);
  s.innerHTML =
    `<span><span class="name"></span><span class="kind"></span></span>` +
    `<span class="word${word === 'carrying' ? ' carrying' : ''}"></span>` +
    `<span class="age"></span>` +
    `<span class="legs"></span>`;
  s.querySelector('.name').textContent = node.label || node.name;
  // A router is a PLACE, a client is a PERSON. Naming the kind is what stops
  // the two being read as the same thing at different sizes.
  s.querySelector('.kind').textContent = node.kind === 'client' ? 'phone' : 'router';
  s.querySelector('.word').textContent = word;
  s.querySelector('.age').textContent = age(node.staleMs);
  const legs = s.querySelector('.legs');
  for (const leg of node.legs || []) {
    const t = document.createElement('span');
    t.className = `tick ${legClass(leg)}`.trim();
    t.title = leg.label || leg.name;
    legs.append(t);
  }
  d.append(s);

  const detail = document.createElement('div');
  detail.className = 'detail';
  for (const leg of node.legs || []) detail.append(legRow(leg));
  d.append(detail);

  li.append(d);
  return li;
}

function legRow(leg) {
  const row = document.createElement('div');
  row.className = 'leg';
  const carrying = (leg.effective_weight || 0) > 0 && (leg.in_bond !== false);
  row.innerHTML = `<span class="name"></span><span class="word"></span><span class="age"></span>`;
  row.querySelector('.name').textContent = leg.label || leg.name;
  // A LEG WITH NOTHING AT ITS ADDRESS IS NOT "UP". A companion leg whose
  // phone has left still has an interface and still passes the shallow state
  // check, so the page read "up, not carrying" directly above the router's own
  // "nothing is answering at this leg's address" - two lines contradicting
  // each other about the same leg. The iOS app already made this distinction;
  // the hub did not, which is exactly the drift the shared vocabulary exists
  // to stop.
  const neverAnswered = !!leg.relay_endpoint
    && (leg.link_rx_bytes ?? leg.rx_bytes ?? 0) === 0
    && leg.rtt_ms == null;
  row.querySelector('.word').textContent =
    carrying ? (leg.state === 'degraded' ? 'carrying, degraded' : 'carrying')
    : neverAnswered ? 'not connected'
    : leg.in_bond === false ? 'not in the bond'
    : leg.state === 'down' ? 'down' : 'up, not carrying';
  // ABSENT, not a dash. An unmeasured round trip is not a zero.
  row.querySelector('.age').textContent =
    leg.rtt_ms == null ? '' : `${Math.round(leg.rtt_ms)} ms`;
  if (leg.last_error) {
    const n = document.createElement('p');
    n.className = 'note' + (leg.in_bond === false ? '' : ' bad');
    n.textContent = leg.last_error;
    row.append(n);
  }
  return row;
}

/** Poll every node this hub knows about. */
async function tick() {
  const res = await fetch('/api/nodes', { cache: 'no-store' }).catch(() => null);
  if (!res || !res.ok) {
    document.getElementById('headline').textContent = 'Hub not answering';
    document.getElementById('subhead').textContent =
      'The hub itself could not be reached, so nothing below can be trusted.';
    return;
  }
  render((await res.json()).nodes || []);
}

tick();
setInterval(tick, 5000);
