// Behavioral probe for the archive confirmation's running-worker warning in
// the shipped dashboard bundle (no build step — the bundle IS the source).
// Extracts the real confirmation-message builder (getDestructiveConfirm plus
// the FALLBACK_* constants and helpers it closes over) verbatim and drives it
// with a minimal tx() stub, then asserts the dialog description gains the
// warning line ONLY when the card being archived is currently running:
//
//   • running  + archived  → description ends with "Dette dræber den kørende worker."
//   • ready    + archived  → no warning
//   • done     + running    → no warning (warning is archive-specific)
//   • blocked  + running    → no warning
//   • bulk (n>1) archive of a running card → warning still present
//   • source status omitted → no warning (backward-compatible call sites)
//
// Run via: node kanban_card_archive_running_warning_probe.js <path-to-bundle>
const fs = require("fs");

const bundlePath = process.argv[2];
const src = fs.readFileSync(bundlePath, "utf8");

// The builder and everything it closes over live in one contiguous block:
// the FALLBACK_* dictionaries, DESTRUCTIVE_KEYS, and the get* helpers.
const blockStart = src.indexOf("const FALLBACK_DESTRUCTIVE = {");
if (blockStart === -1) { console.error("FAIL: FALLBACK_DESTRUCTIVE block not found in bundle"); process.exit(1); }
const fnStart = src.indexOf("function getDestructiveConfirm(");
if (fnStart === -1) { console.error("FAIL: getDestructiveConfirm not found in bundle"); process.exit(1); }
const bodyStart = src.indexOf("{", fnStart);
let depth = 0, end = bodyStart;
for (; end < src.length; end++) {
  if (src[end] === "{") depth++;
  else if (src[end] === "}") { depth--; if (depth === 0) break; }
}
const blockSrc = src.slice(blockStart, end + 1);

// tx() falls back to the provided default string when the i18n catalog has no
// entry — exactly the path the dialog takes for the new archiveRunningWarning key.
function tx(t, key, fallback, vars) {
  if (typeof fallback !== "string") return fallback;
  if (vars) Object.keys(vars).forEach(function (k) { fallback = fallback.split("{" + k + "}").join(String(vars[k])); });
  return fallback;
}
const WARNING = "Dette dræber den kørende worker.";
function fail(msg) { console.error("FAIL: " + msg); process.exit(1); }

// The extracted declarations become visible to this module's scope.
eval(blockSrc);

function desc(status, count, sourceStatus) {
  return getDestructiveConfirm({}, status, count, sourceStatus);
}

// Running card archived from the ⋯ menu / drawer: dialog is shown and warns.
const runningArchive = desc("archived", 1, "running");
if (!runningArchive) fail("archive confirm description is empty for a running card");
if (runningArchive.indexOf(WARNING) === -1) {
  fail("running card archive description missing warning: " + JSON.stringify(runningArchive));
}
if (runningArchive.indexOf("Archive this task?") === -1) {
  fail("warning replaced the base archive message: " + JSON.stringify(runningArchive));
}

// Non-running card archived: no warning.
["ready", "todo", "done", "blocked", "review", "triage", "scheduled"].forEach(function (st) {
  const d = desc("archived", 1, st);
  if (d.indexOf(WARNING) !== -1) fail("warning leaked for non-running source status: " + st + " -> " + JSON.stringify(d));
});

// Warning is archive-specific: completing/blocking a running card must not warn.
if (desc("done", 1, "running").indexOf(WARNING) !== -1) fail("done confirm warned about a running worker");
if (desc("blocked", 1, "running").indexOf(WARNING) !== -1) fail("blocked confirm warned about a running worker");

// Bulk archive that includes a running card still warns (plural copy).
const bulkRunning = desc("archived", 3, "running");
if (bulkRunning.indexOf(WARNING) === -1) fail("bulk archive of running card(s) missing warning: " + JSON.stringify(bulkRunning));
if (bulkRunning.indexOf("Archive 3 tasks?") === -1) fail("bulk archive message lost pluralization: " + JSON.stringify(bulkRunning));
// Bulk archive without a running card: no warning.
if (desc("archived", 3, null).indexOf(WARNING) !== -1) fail("bulk archive warned without a running card");

// Call sites that pass no source status (older paths) keep the plain message.
const plain = desc("archived", 1, undefined);
if (plain.indexOf(WARNING) !== -1) fail("warning appeared when no source status was supplied");

console.log("PASS");
