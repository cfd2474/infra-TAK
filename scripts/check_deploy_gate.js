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

// ⚠️ Starts at `renderInstances` so the button wording is driven too: it
// is the one rule that depends on how many deployments came back.
const start = scripts.indexOf("function renderInstances");
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
  plain_host: "atlas.leckliter.net",
  host_template: "atlas.{slug}.leckliter.net",
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
    + "ctx.render = renderInstances;"
    + "ctx.onMode = onModeChanged; ctx.preview = previewSlug;"
    + "ctx.open = openInstanceConfirm; ctx.close = closeInstanceConfirm;");

  return {
    el,
    problem: ctx.problem, open: ctx.open, close: ctx.close,
    render: ctx.render,
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
  const h = harness({ ...CAPACITY, slugs: ["agencya"] });
  h.pick("dynamic");

  h.slug("agencya");
  check("a slug already in use is refused",
    (h.problem() || "").includes("already a deployment"), String(h.problem()));

  h.slug("AgencyB");
  check("an uppercase slug is accepted as its lowercase form",
    h.problem() === null, String(h.problem()));
  check("and is shown back normalised",
    h.el("slugPreview").textContent.includes("atlas.agencyb.leckliter.net"),
    h.el("slugPreview").textContent);

  h.slug("AGENCYA");
  check("a duplicate is caught whatever the case",
    (h.problem() || "").includes("already a deployment"), String(h.problem()));

  // ⚠️ The field **strips** what it will not accept rather than refusing the
  // keystroke, so an invalid slug cannot be held at all. The preview then shows
  // what was actually kept — the operator sees the name they will get, which is
  // the same rule that forces case.
  h.slug("two words");
  check("a space is stripped as it is typed",
    h.el("agencySlug").value === "twowords", h.el("agencySlug").value);
  check("and what is left is accepted", h.problem() === null, String(h.problem()));
  check("with the preview showing what was kept",
    h.el("slugPreview").textContent.includes("atlas.twowords.leckliter.net"),
    h.el("slugPreview").textContent);

  h.slug("county-1");
  check("hyphens and digits are stripped too",
    h.el("agencySlug").value === "county", h.el("agencySlug").value);

  h.slug("!!!");
  check("a slug of nothing but symbols empties the field",
    h.el("agencySlug").value === "", h.el("agencySlug").value);
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
  check("the summary names the real hostname",
    summary.includes("atlas.pd.leckliter.net"), summary);
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

// --- the button says what it can actually do -------------------------------- //
{
  const h = harness({ ...CAPACITY });
  h.render({ instances: [], capacity: { ...CAPACITY } });
  check("with nothing deployed the button says 'Deploy instance'",
    h.el("addInstanceBtn").textContent === "Deploy instance",
    h.el("addInstanceBtn").textContent);

  h.render({ instances: [{ slug: null, mode: "dynamic", size_gb: 50 }],
             capacity: { ...CAPACITY, may_deploy_plain: false } });
  check("once one exists it says 'Deploy additional instance'",
    h.el("addInstanceBtn").textContent === "Deploy additional instance",
    h.el("addInstanceBtn").textContent);

  h.render({ instances: [], capacity: { ...CAPACITY } });
  check("and it goes back when the last one is removed",
    h.el("addInstanceBtn").textContent === "Deploy instance",
    h.el("addInstanceBtn").textContent);
}

// --- the confirmation names the real domain --------------------------------- //
{
  const h = harness({ ...CAPACITY });
  h.pick("dynamic");
  h.slug("corona");
  check("the preview names the real domain",
    h.el("slugPreview").textContent.includes("atlas.corona.leckliter.net"),
    h.el("slugPreview").textContent);

  h.open();
  const summary = h.el("instanceConfirmSummary").textContent;
  check("and so does the confirmation",
    summary.includes("atlas.corona.leckliter.net")
    && !summary.includes("<your-domain>"), summary);
}

{
  // ⚠️ Before the box has an FQDN there is nothing truthful to show, and an
  // invented domain in a confirmation is worse than an obvious placeholder.
  const h = harness({ ...CAPACITY, plain_host: "", host_template: "" });
  h.pick("dynamic");
  h.slug("corona");
  h.open();
  check("without an FQDN it falls back to the placeholder rather than guessing",
    h.el("instanceConfirmSummary").textContent.includes("<your-domain>"),
    h.el("instanceConfirmSummary").textContent);
}

{
  const h = harness({ ...CAPACITY });
  h.pick("dynamic");
  h.slug("");
  h.open();
  check("a general deployment names the plain host",
    h.el("instanceConfirmSummary").textContent.includes("atlas.leckliter.net"),
    h.el("instanceConfirmSummary").textContent);
}

console.log(fails === 0
  ? "\n  all deployment-form checks passed"
  : "\n  " + fails + " check(s) failed");
process.exit(fails === 0 ? 0 : 1);
