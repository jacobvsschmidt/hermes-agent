// Behavioral probe for the TaskCard "Arkiver" (archive) action in the shipped
// dashboard bundle (no build step — the bundle IS the source). Extracts the real
// `function TaskCard(props)` verbatim and renders it against a minimal React/SDK
// stub, then drives the kebab menu with real click handlers:
//
//   1. A card renders a "⋯" actions trigger (aria-haspopup).
//   2. Clicking it opens a menu containing exactly one "Arkiver" item.
//   3. Clicking "Arkiver" calls props.onMove(taskId, "archived") — the same
//      call signature the board uses for a status move (so the PATCH + list
//      refresh path is the existing, tested one) — and closes the menu.
//   4. An already-archived card exposes no archive trigger (canArchive gate).
//
// Run via: node kanban_card_archive_probe.js <path-to-bundle>
const fs = require("fs");

const bundlePath = process.argv[2];
const src = fs.readFileSync(bundlePath, "utf8");
const start = src.indexOf("function TaskCard(");
if (start === -1) { console.error("TaskCard not found in bundle"); process.exit(1); }
const bodyStart = src.indexOf("{", start);
let depth = 0, end = bodyStart;
for (; end < src.length; end++) {
  if (src[end] === "{") depth++;
  else if (src[end] === "}") { depth--; if (depth === 0) break; }
}
const fnSrc = src.slice(start, end + 1);

// ---- Minimal React/SDK stub ------------------------------------------------
function h(type, props, ...children) {
  const p = Object.assign({}, props || {});
  if (children.length === 1) p.children = children[0];
  else if (children.length > 1) p.children = children;
  return { type: type, props: p };
}
function Dummy(props) { return h("dummy", props, props && props.children); }
const Card = Dummy, CardContent = Dummy, Badge = Dummy, Checkbox = Dummy, Button = Dummy;

let store = {};
let hookIdx = 0;
function resetHooks() { hookIdx = 0; }
function useState(init) {
  const k = hookIdx++;
  if (!(k in store)) store[k] = init;
  return [store[k], function (v) { store[k] = (typeof v === "function") ? v(store[k]) : v; }];
}
const useEffect = function () {};
const useRef = function () { return { current: null }; };
const useI18n = function () { return { t: { kanban: null }, locale: "en" }; };
const tx = function (t, key, fallback) { return fallback; };
const cn = function () { return Array.prototype.slice.call(arguments).filter(Boolean).join(" "); };
const timeAgo = function () { return ""; };
const attachTouchDrag = function () { return function () {}; };
const stalenessClass = function () { return ""; };
const MIME_TASK = "application/x-hermes-task";

// The extracted function declaration becomes visible to its own closure here.
eval(fnSrc);

// ---- Tree helpers ----------------------------------------------------------
function walk(node, out) {
  if (!node || typeof node !== "object") return;
  out.push(node);
  if (node.props) {
    const c = node.props.children;
    if (Array.isArray(c)) c.forEach(function (x) { walk(x, out); });
    else walk(c, out);
  }
}
function render(props) { resetHooks(); const out = []; walk(TaskCard(props), out); return out; }

function makeProps(task, onMove) {
  return {
    task: task,
    onMove: onMove,
    onOpen: function () {},
    toggleSelected: function () {},
    toggleRange: function () {},
    selected: false,
    failed: false,
    draggingTaskId: null,
    draggingSource: false,
  };
}

// 1 + 2 + 3: active card → trigger opens a menu → Arkiver calls onMove(id,"archived").
const calls = [];
const onMove = function (id, status) { calls.push([id, status]); };

let nodes = render(makeProps({ id: "t_probe", status: "ready", title: "Hej" }, onMove));
const trigger = nodes.find(function (n) { return n.type === Button && n.props && n.props["aria-haspopup"]; });
if (!trigger) { console.error("FAIL: active card has no archive trigger (⋯ button)"); process.exit(1); }
if (trigger.props["aria-expanded"] !== false) { console.error("FAIL: menu should start collapsed"); process.exit(1); }

trigger.props.onClick({ stopPropagation: function () {} });

nodes = render(makeProps({ id: "t_probe", status: "ready", title: "Hej" }, onMove));
const item = nodes.find(function (n) {
  return n.type === Button && n.props && n.props.className === "hermes-kanban-card-menu-item";
});
if (!item) { console.error("FAIL: clicking the trigger did not open the actions menu"); process.exit(1); }
if (item.props.children !== "Arkiver") {
  console.error("FAIL: archive menu item label is not 'Arkiver': " + JSON.stringify(item.props.children));
  process.exit(1);
}

item.props.onClick({ stopPropagation: function () {} });
if (calls.length !== 1 || calls[0][0] !== "t_probe" || calls[0][1] !== "archived") {
  console.error("FAIL: 'Arkiver' did not request a move to archived: " + JSON.stringify(calls));
  process.exit(1);
}

// Menu must close after the action.
nodes = render(makeProps({ id: "t_probe", status: "ready", title: "Hej" }, onMove));
if (nodes.some(function (n) { return n.props && n.props.className === "hermes-kanban-card-menu-item"; })) {
  console.error("FAIL: menu stayed open after 'Arkiver'");
  process.exit(1);
}

// 4: an already-archived card must not expose the archive affordance.
nodes = render(makeProps({ id: "t_done", status: "archived", title: "Gammel" }, onMove));
if (nodes.some(function (n) { return n.type === Button && n.props && n.props["aria-haspopup"]; })) {
  console.error("FAIL: archived card still exposes an archive trigger");
  process.exit(1);
}

console.log("PASS");
