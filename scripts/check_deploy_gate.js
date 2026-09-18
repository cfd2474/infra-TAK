/*
 * Drives the deployment form's validation and its confirmation modal (W216).
 *
 * ⚠️ Plain node, a hand-rolled DOM, a stubbed capacity — no jsdom, no
 * dependencies. Everything here is behaviour: a button that enables at the right
 * moment, a refusal that explains itself, and a modal that repeats the choices
 * back before anything is built. None of it is visible in a string search.
 *
 * ⚠️ **This is the only test in the repository that executes the page's own
 * logic**, and it earns its keep. Three source-text assertions passed while
 * mutations replaced their guards with `if (false)`: the message stayed in the
 * source, so the assertion matched while the rule did nothing. Running the code
 * cannot be fooled that way.
 *
 * It replaced a harness that drove the retired single-instance gate. The
 * behaviours it checks are the same ones; the controls they live on are not.
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

const start = scripts.indexOf("function currentMode");
const end = scripts.indexOf("function refreshCapacity");
if (start < 0 || end < 0) {
  console.log("  FAIL the deployment form's script is not on the page in the shape expected");
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

// Real proportions, read off the measured box.
const CAPACITY = {
  budget_gb: 354.6, committed_gb: 0, pool_gb: 354.6, remaining_gb: 354.6,
  min_gb: 2, max_gb: 319.6, may_deploy_plain: true, slugs: [],
};

function harness(capacity) {
  const els = {};
  const el = (id) => {
    if (!els[id]) {
      // ⚠️ `value` is a **string** on a real input, whatever is assigned to it.
      // A fake DOM that stores the raw number makes `.trim()` throw, which looks
      // exactly like a bug in the page. Faithful beats convenient.
      const node = {
        id, style: {}, textContent: "", innerHTML: "", _value: "",
        disabled: false, checked: false,
        classList: { _s: new Set(),
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

  const radios = { dynamic: el("radio-dynamic"), fixed: el("radio-fixed") };
  radios.dynamic.checked = true;

  global.document = {
    getElementById: el,
    querySelector(sel) {
      const m = /value="(\w+)"/.exec(sel);
      return m ? radios[m[1]] || null : null;
    },
  };
  // The page declares `capacityState` above this slice, so the extracted
  // functions resolve it up the scope chain to here.
  global.capacityState = capacity;

  const ctx = {};
  // eslint-disable-next-line no-eval
  eval(source
    + "\n;ctx.problem = addInstanceProblem; ctx.gate = refreshAddGate;"
    + "ctx.onMode = onModeChanged; ctx.preview = previewSlug;"
    + "ctx.open = openInstanceConfirm; ctx.close = closeInstanceConfirm;");

  return {
    el,
    problem: ctx.problem, open: ctx.open, close: ctx.close,
    pick(mode) {
      radios.dynamic.checked = mode === "dynamic";
      radios.fixed.checked = mode === "fixed";
      ctx.onMode();
    },
    size(v) { el("instanceSize").value = v; ctx.gate(); },
    slug(v) { el("agencySlug").value = v; ctx.preview(); },
  };
}

// --- the type decides what is asked for ------------------------------------ //
{
  const h = harness({ ...CAPACITY });
  h.pick("dynamic");
  check("dynamic hides the size field", h.el("sizeField").style.display === "none");
  check("dynamic says what it will share",
    h.el("dynamicNote").textContent.includes("354.6"),
    h.el("dynamicNote").textContent);

  h.pick("fixed");
  check("fixed shows the size field", h.el("sizeField").style.display === "");
}

// --- a blank slug means the box's general deployment ----------------------- //
{
  const h = harness({ ...CAPACITY, may_deploy_plain: true });
  h.pick("dynamic");
  h.slug("");
  check("a blank slug is allowed on an empty box", h.problem() === null,
    String(h.problem()));
  check("and the page says it becomes the general ATLAS",
    h.el("slugPreview").textContent.includes("general ATLAS"),
    h.el("slugPreview").textContent);
}

{
  const h = harness({ ...CAPACITY, may_deploy_plain: false });
  h.pick("dynamic");
  h.slug("");
  check("a blank slug is refused once a general ATLAS exists",
    (h.problem() || "").includes("already has a general ATLAS"),
    String(h.problem()));
  check("and the button is disabled for it",
    h.el("createInstanceBtn").disabled === true);
}

// --- slugs ------------------------------------------------------------------ //
{
  const h = harness({ ...CAPACITY, slugs: ["agency-a"] });
  h.pick("dynamic");

  h.slug("agency-a");
  check("a slug already in use is refused",
    (h.problem() || "").includes("already a deployment"), String(h.problem()));

  h.slug("Agency-B");
  check("an uppercase slug is accepted as its lowercase form",
    h.problem() === null, String(h.problem()));
  check("and is shown back normalised",
    h.el("slugPreview").textContent.includes("atlas.agency-b."),
    h.el("slugPreview").textContent);

  h.slug("AGENCY-A");
  check("a duplicate is caught whatever the case",
    (h.problem() || "").includes("already a deployment"), String(h.problem()));

  h.slug("two words");
  check("a slug with a space is refused",
    (h.problem() || "").includes("hostname"), String(h.problem()));

  h.slug("-lead");
  check("a slug starting with a hyphen is refused", h.problem() !== null);
}

// --- fixed sizes are bounded ------------------------------------------------ //
{
  const h = harness({ ...CAPACITY });
  h.pick("fixed");
  h.slug("pd");

  h.size("");
  check("fixed with no size is refused",
    (h.problem() || "").includes("Enter a size"), String(h.problem()));
  check("and the button is disabled",
    h.el("createInstanceBtn").disabled === true);

  h.size("1");
  check("below the floor is refused",
    (h.problem() || "").includes("at least 2 GB"), String(h.problem()));

  h.size("400");
  check("above the 85% ceiling is refused",
    (h.problem() || "").includes("at most 319.6 GB"), String(h.problem()));

  h.size("50");
  check("a workable size is accepted", h.problem() === null, String(h.problem()));
  check("and the button enables", h.el("createInstanceBtn").disabled === false);
}

// --- dynamic is never asked for a size -------------------------------------- //
{
  const h = harness({ ...CAPACITY });
  h.pick("dynamic");
  h.slug("pd");
  check("dynamic needs no size at all", h.problem() === null, String(h.problem()));
}

// --- the confirmation modal -------------------------------------------------- //
{
  const h = harness({ ...CAPACITY });
  h.pick("fixed");
  h.slug("pd");
  h.size("50");
  h.open();
  const summary = h.el("instanceConfirmSummary").textContent;
  check("the modal opens for a valid choice",
    h.el("instanceModal").classList.contains("open"));
  check("the summary names the agency", summary.includes("pd"), summary);
  check("the summary names the hostname", summary.includes("atlas.pd."), summary);
  check("the summary names the type", summary.includes("fixed"), summary);
  check("the summary names the size", summary.includes("50 GB"), summary);

  h.close();
  check("cancelling closes it",
    h.el("instanceModal").classList.contains("open") === false);
}

{
  const h = harness({ ...CAPACITY });
  h.pick("fixed");
  h.slug("pd");
  h.size("");                    // invalid
  h.open();
  check("the modal refuses to open on an invalid choice",
    h.el("instanceModal").classList.contains("open") === false);
}

{
  const h = harness({ ...CAPACITY });
  h.pick("dynamic");
  h.slug("");
  h.open();
  const summary = h.el("instanceConfirmSummary").textContent;
  check("a general dynamic deployment summarises as sharing the pool",
    summary.includes("shares the") && summary.includes("general ATLAS"), summary);
}

console.log(fails === 0
  ? "\n  all deployment-form checks passed"
  : "\n  " + fails + " check(s) failed");
process.exit(fails === 0 ? 0 : 1);
