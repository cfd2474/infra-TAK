/*
 * Drives the reserved-storage field on the ATLAS module page (W205).
 *
 * ⚠️ Plain node with a hand-rolled DOM and a stubbed fetch — no jsdom, no
 * dependencies. The field does three things and all three are behaviour rather
 * than markup: it reports the box's free space, it sets its own ceiling to the
 * 85% line, and it prefills an existing reservation so a re-deploy does not
 * arrive blank. A source-level check would catch none of that.
 *
 *   node scripts/check_store_field.js
 */

const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const PAGE = path.join(ROOT, "templates", "atlas.html");

// The page is a Jinja template: strip the tags so node can parse what is left.
const html = fs.readFileSync(PAGE, "utf8");
const scripts = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)]
  .map((m) => m[1])
  .join("\n")
  .replace(/\{%[\s\S]*?%\}/g, "")
  .replace(/\{\{[\s\S]*?\}\}/g, "null");

const start = scripts.indexOf("function loadStoreFacts");
const end = scripts.indexOf("function startDeploy");
if (start < 0 || end < 0 || end < start) {
  console.log("  FAIL the reserved-storage script is not on the page any more");
  process.exit(1);
}

let fails = 0;
function check(name, ok, detail) {
  if (ok) console.log("  ok   " + name);
  else {
    fails += 1;
    console.log("  FAIL " + name + (detail ? "  |  " + detail : ""));
  }
}

function fakeDom() {
  const els = {};
  return {
    el(id) {
      if (!els[id]) {
        // `value` is a string on a real input whatever is assigned to it.
        const node = { id, style: {}, textContent: "", innerHTML: "", _value: "" };
        Object.defineProperty(node, "value", {
          get() { return this._value; },
          set(v) { this._value = String(v); },
        });
        els[id] = node;
      }
      return els[id];
    },
  };
}

function run(facts) {
  const dom = fakeDom();
  // The page wires an input listener for the deploy gate (W206); a fake
  // document without one throws before anything under test runs.
  global.document = { getElementById: dom.el, addEventListener: () => {} };
  global.fetch = () => Promise.resolve({ json: () => Promise.resolve(facts) });
  eval(scripts.slice(start, end));
  loadStoreFacts();
  return new Promise((resolve) => setTimeout(() => resolve(dom), 20));
}

(async () => {
  // A box with a reservation already in place.
  let dom = await run({
    disk_total_gb: 473.0, disk_free_gb: 376.0, reserved_gb: 40.0,
    usable_gb: 39.2, used_gb: 0.6, max_gb: 319.6, min_gb: 2, mounted: true,
  });

  check(
    "it reports the box's free space",
    dom.el("storeHelp").textContent.includes("376 GB free"),
    dom.el("storeHelp").textContent,
  );
  check(
    "it names the 85% ceiling, not the whole disk",
    dom.el("storeHelp").textContent.includes("319.6") &&
      !dom.el("storeHelp").textContent.includes("473"),
    dom.el("storeHelp").textContent,
  );
  check(
    "the field refuses above the ceiling",
    String(dom.el("storeGb").max) === "319.6",
    "max=" + dom.el("storeGb").max,
  );
  check(
    "an existing reservation prefills the field",
    dom.el("storeGb").value === "40",
    "value=" + dom.el("storeGb").value,
  );
  check(
    "usable space is shown as measured, not as asked for",
    dom.el("storeState").innerHTML.includes("39.2"),
    dom.el("storeState").innerHTML,
  );

  // A box with no reservation yet: nothing to prefill, nothing to report as in use.
  dom = await run({
    disk_total_gb: 473.0, disk_free_gb: 376.0, reserved_gb: 0,
    usable_gb: 0, used_gb: 0, max_gb: 319.6, min_gb: 2, mounted: false,
  });
  check(
    "an unreserved box leaves the field empty",
    dom.el("storeGb").value === "",
    "value=" + dom.el("storeGb").value,
  );
  check(
    "and says nothing about usage it does not have",
    dom.el("storeState").style.display !== "",
    "display=" + dom.el("storeState").style.display,
  );

  // The disk could not be read.
  dom = await run({ error: "boom" });
  check(
    "a disk it cannot read says so rather than offering a number",
    dom.el("storeHelp").textContent.includes("Could not read"),
    dom.el("storeHelp").textContent,
  );

  console.log("\n" + (fails ? fails + " failure(s)" : "all checks passed"));
  process.exit(fails ? 1 : 0);
})();
