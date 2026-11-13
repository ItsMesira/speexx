// ponytail: self-check for optimized.js's pure regex logic only — the ordinal
// parser ("take the LAST /exercise/<n>"), the packet-root detector (negative
// lookahead + double condition that routes the whole null-ordinal branch), and
// the tryEval heal. These regexes MUST mirror optimized.js exactly; change one
// there, change it here. DOM/timer logic is not pure, so it stays uncovered.
// Run: node check.js
var assert = require("assert");

// --- getExerciseOrdinal's lastExerciseNumber (exact copy from optimized.js) ---
function lastExerciseNumber(str) {
  if (!str) return null;
  var matches = str.match(/\/exercise\/(\d+)/g);
  if (!matches) return null;
  var num = matches[matches.length - 1].match(/(\d+)/);
  return num ? parseInt(num[1], 10) : null;
}
assert.strictEqual(lastExerciseNumber("/standard-packet/3/exercise/7"), 7, "plain path ordinal");
assert.strictEqual(lastExerciseNumber("/standard-packet/3/exercise/21/speaking"), 21, "trailing segment after ordinal (speaking test)");
assert.strictEqual(lastExerciseNumber("/p/exercise/2#/exercise/5"), 5, "takes the LAST /exercise/<n>");
assert.strictEqual(lastExerciseNumber("/no-exercise-here"), null, "no match -> null");
assert.strictEqual(lastExerciseNumber(""), null, "empty -> null");
assert.strictEqual(lastExerciseNumber(null), null, "nullish -> null");

// --- null-ordinal packet-root detection (exact copy from optimized.js) ---
function onPacketRoot(nullPath) {
  return /\/standard-packet\/\d+(?!\/exercise\/)/.test(nullPath) && !/\/exercise\//.test(nullPath);
}
assert.strictEqual(onPacketRoot("/standard-packet/3"), true, "packet root, no exercise");
assert.strictEqual(onPacketRoot("/standard-packet/3/"), true, "trailing slash still root");
assert.strictEqual(onPacketRoot("/standard-packet/3/exercise/1"), false, "has /exercise/ -> not root");
assert.strictEqual(onPacketRoot("/standard-packet/3/exercise/1/speaking"), false, "exercise + segment -> not root");
assert.strictEqual(onPacketRoot("/some/other/path"), false, "unrelated path -> not root");

// --- tryEval heal: strip trailing "();" and wrap in parens (exact copy) ---
function heal(src) { return "(" + src.replace(/\(\)\s*;\s*$/, "") + ")();"; }
// The premise: the unhealed "function(){}();" form is a SyntaxError...
assert.throws(function () { (0, eval)("function f(){}();"); }, SyntaxError, "unhealed form must be a SyntaxError");
// ...and the healed form evaluates cleanly.
assert.doesNotThrow(function () { (0, eval)(heal("function f(){}();")); }, "healed form must eval without SyntaxError");

console.log("optimized.js self-check: OK");
