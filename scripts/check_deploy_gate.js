/*
 * Drives the deploy gate and its confirmation modal (W206).
 *
 * ⚠️ Plain node, a hand-rolled DOM, a stubbed fetch — no jsdom, no dependencies.
 * Everything here is behaviour: a button that enables at the right moment, a
 * refusal that explains itself, and a modal that repeats the number back before
 * anything is allocated. None of it is visible in a string search.
 *
 *   node scripts/check_deploy_gate.js
 */

const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(ROOT, "templates", "atlas.html"), "utf8");

const scripts = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)]
  .map((m) => m[1])
  .join("\n")
  .replace(/\{%[\s\S]*?%\}/g, "")
  .replace(/\{\{[\s\S]*?\}\}/g, "null");

const start = scripts.indexOf("function loadStoreFacts");
const end = scripts.indexOf("function pollDeploy");
if (start < 0 || end < 0) {
  console.log("  FAIL the deploy script is not on the page in the shape expected");
  process.exit(1);
}
const source = scripts.slice(start, end);

let fails = 0;
function check(name, ok, detail) {
  if (ok) console.log("  ok   " + name);
  else {
    fails += 1;
    console.log("  FAIL " + name + (detail ? "  |  " + detail : ""));
  }
}

const FACTS = {
  disk_total_gb: 473.0, disk_free_gb: 376.0, reserved_gb: 0,
  usable_gb: 0, used_gb: 0, max_gb: 319.6, min_gb: 2, mounted: false,
};

function harness(facts) {
  const els = {};
  const listeners = [];
  const el = (id) => {
    if (!els[id]) {
      // ⚠️ `value` is a **string** on a real input, whatever is assigned to
      // it. A fake DOM that stores the raw number makes `.trim()` throw, which
      // looks exactly like a bug in the page. Faithful beats convenient.
      const node = {
        id, style: {}, textContent: "", innerHTML: "", _value: "",
        disabled: false, classList: { _s: new Set(),
          add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
          contains(c) { return this._s.has(c); } },
      };
      Object.defineProperty(node, "value", {
        get() { return this._value; },
        set(v) { this._value = String(v); },
      });
      els[id] = node;
    }
    return els[id];
  };
  global.document = {
    getElementById: el,
    addEventListener: (type, fn) => listeners.push({ type, fn }),
  };
  global.fetch = () => Promise.resolve({ json: () => Promise.resolve(facts) });
  const ctx = {};
  // eslint-disable-next-line no-eval
  eval(source + "\n;ctx.gate = refreshDeployGate; ctx.open = openDeployConfirm;"
     + "ctx.close = closeDeployConfirm; ctx.problem = storeSizeProblem;");
  return {
    el,
    type(v) { el("storeGb").value = v; ctx.gate(); },
    open: ctx.open, close: ctx.close, problem: ctx.problem,
    settled: () => new Promise((r) => setTimeout(r, 20)),
  };
}

(async () => {
  let h = harness(FACTS);
  await h.settled();

  // --- the gate ----------------------------------------------------------- //
  check(
    "the button is disabled with nothing typed",
    h.el("deployBtn").disabled === true,
  );
  check(
    "and says why, rather than greying out silently",
    /Enter a reserved storage size/.test(h.el("deployGate").textContent),
    h.el("deployGate").textContent,
  );

  h.type("40");
  check("a valid size enables it", h.el("deployBtn").disabled === false);
  check("and the reason disappears", h.el("deployGate").textContent === "");

  h.type("");
  check("clearing the field disables it again", h.el("deployBtn").disabled === true);

  h.type("1");
  check(
    "below the floor is refused, in the server's own words",
    h.el("deployBtn").disabled === true &&
      /at least 2 GB/.test(h.el("deployGate").textContent),
    h.el("deployGate").textContent,
  );

  h.type("400");
  check(
    "above the 85% ceiling is refused, naming the ceiling",
    h.el("deployBtn").disabled === true &&
      /at most 319.6 GB/.test(h.el("deployGate").textContent),
    h.el("deployGate").textContent,
  );

  h.type("abc");
  check(
    "nonsense is refused without enabling anything",
    h.el("deployBtn").disabled === true,
    h.el("deployGate").textContent,
  );

  // --- the modal ----------------------------------------------------------- //
  h.type("40");
  h.open();
  check(
    "confirming shows the size that was typed",
    h.el("deployConfirmSize").textContent === "40 GB",
    h.el("deployConfirmSize").textContent,
  );
  check(
    "and what will be left on the box",
    /336 GB will remain/.test(h.el("deployConfirmRoom").textContent),
    h.el("deployConfirmRoom").textContent,
  );
  check("the modal opens", h.el("deployModal").classList.contains("open"));

  h.close();
  check("cancel closes it", h.el("deployModal").classList.contains("open") === false);

  // ⚠️ The button is already disabled for a bad size, but a modal that could be
  // opened another way must not offer to reserve one.
  h.type("400");
  h.open();
  check(
    "a refused size cannot open the confirmation at all",
    h.el("deployModal").classList.contains("open") === false,
  );

  // --- a box that already has a store -------------------------------------- //
  h = harness({ ...FACTS, reserved_gb: 40, usable_gb: 39.2, used_gb: 0.6, mounted: true });
  await h.settled();
  check(
    "an existing reservation prefills and enables the button",
    h.el("storeGb").value === "40" && h.el("deployBtn").disabled === false,
    "value=" + h.el("storeGb").value + " disabled=" + h.el("deployBtn").disabled,
  );

  console.log("\n" + (fails ? fails + " failure(s)" : "all checks passed"));
  process.exit(fails ? 1 : 0);
})();
