// Loads the built dashboard in jsdom and checks (1) the page's own parity check against the
// mart passed, (2) every view renders without script errors, (3) filtering changes the numbers
// and matches an independent count. Run: npm install jsdom && node tests/test_dashboard_parity.js
const fs = require("fs"), path = require("path");
const { JSDOM } = require("jsdom");
const file = path.join(__dirname, "..", "output", "credit_risk_dashboard.html");
const html = fs.readFileSync(file, "utf8");
const errors = [];
const dom = new JSDOM(html, { runScripts: "dangerously", pretendToBeVisual: true,
  beforeParse(w) { w.addEventListener("error", e => errors.push(e.message)); } });
const w = dom.window, doc = w.document;
let failed = 0;
const ok = (cond, msg) => { console.log(`  ${cond ? "ok  " : "FAIL"}  ${msg}`); if (!cond) failed++; };
setTimeout(() => {
  const P = w.__PARITY__;
  ok(errors.length === 0, `no script errors ${errors.length ? errors.join("; ") : ""}`);
  ok(P && P.ok, `dashboard figures match the mart (${P ? P.checks : 0} figures compared) ${P && !P.ok ? P.issues.join("; ") : ""}`);
  for (const v of ["risk", "fin", "dq"]) ok(doc.querySelector("#view-" + v).innerHTML.length > 1000, `${v} view renders`);
  ok(doc.querySelector("#count").textContent.includes("30,000"), "unfiltered count is 30,000");
  const sel = doc.querySelector("#f-lb");
  const lowBand = [...sel.options].find(o => o.textContent === "<= 50k");
  sel.value = lowBand.value; sel.dispatchEvent(new w.Event("change"));
  const D = JSON.parse(html.match(/const D = (\{.*?\});\n/s)[1]);
  const expect = D.data.lb.filter(x => x === 1).length;
  ok(doc.querySelector("#count").textContent.includes(expect.toLocaleString("en-US")), `limit filter shows ${expect} accounts`);
  console.log(failed ? `\n${failed} check(s) failed` : "\nall dashboard checks passed");
  process.exit(failed ? 1 : 0);
}, 300);
