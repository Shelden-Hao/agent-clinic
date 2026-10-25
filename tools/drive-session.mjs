/**
 * 驱动线上采集台跑一批真实的多轮会话，并把结果取回本地。
 *
 * 和 drive-collect.mjs 同构。分开写而不是加参数，是因为两者要盯的状态不同：
 * 单轮盯「几条轨迹跑完」，多轮要盯「几场会话、每场几轮、失败在第几轮」。
 *
 * 用法：
 *   node tools/drive-session.mjs --version v1 --repeat 1 --out runtime/sessions-v1.json
 *   node tools/drive-session.mjs --version both --repeat 2 --only S01,S02
 */

const PW = "file:///C:/Users/86139/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js";
const pw = await import(PW);
const chromium = pw.chromium ?? pw.default?.chromium;
import fs from "node:fs/promises";
import path from "node:path";

const BASE = "https://agent-clinic.app.workbuddy.host";
const SHARE = "?sharecode=HRSKWOV2YB";
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";

const argv = process.argv.slice(2);
const arg = (k, d) => {
  const i = argv.indexOf(`--${k}`);
  return i >= 0 && argv[i + 1] ? argv[i + 1] : d;
};

const version = arg("version", "v1");
const repeat = arg("repeat", "1");
const only = arg("only", "");
const model = arg("model", "");
const out = arg("out", "runtime/sessions.json");
const timeoutMin = Number(arg("timeout", "40"));
const reset = arg("reset", "") === "1";

const params = new URLSearchParams({ auto: "1", version, repeat });
if (only) params.set("only", only);
if (model) params.set("model", model);

const browser = await chromium.launch({ executablePath: CHROME, headless: true });
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });

page.on("console", (m) => {
  const t = m.text();
  if (/error|failed|异常|失败/i.test(t)) console.log("  [console]", t.slice(0, 300));
});
page.on("pageerror", (e) => console.log("  [pageerror]", String(e).slice(0, 400)));

console.log(`→ 打开分享链接建立会话`);
await page.goto(BASE + "/" + SHARE, { waitUntil: "domcontentloaded" });
await page.waitForTimeout(1500);

if (reset) {
  await page.evaluate(() => fetch("/api/lab/reset-session", { method: "POST" }));
  console.log(`→ 已清空上一批会话`);
}

console.log(`→ 进入多轮采集台 version=${version} repeat=${repeat} only=${only || "全部"}`);
await page.goto(`${BASE}/lab/session.html?${params}`, { waitUntil: "domcontentloaded" });
await page.waitForTimeout(4000);

const models = await page.evaluate(() => [...document.querySelectorAll("#model option")].map((o) => o.value));
console.log(`→ 可用模型: ${models.join(", ") || "无"}`);
if (!models.length) {
  const logs = await page.evaluate(() => document.getElementById("log").innerText);
  console.log("采集台日志:\n" + logs.slice(0, 1500));
  await browser.close();
  process.exit(2);
}

const deadline = Date.now() + timeoutMin * 60_000;
let last = -1;
while (Date.now() < deadline) {
  const st = await page.evaluate(() => window.__lab);
  if (st.done !== last) {
    last = st.done;
    console.log(`  ${st.state}: ${st.done} 场 / ${st.turns} 轮 / 失败轮 ${st.failed}`);
  }
  if (st.state === "done" || st.state === "aborted") break;
  await page.waitForTimeout(2000);
}

const dump = await page.evaluate(async () => {
  const r = await fetch("/api/lab/dump-sessions");
  return r.json();
});

await fs.mkdir(path.dirname(out), { recursive: true }).catch(() => {});
await fs.writeFile(out, JSON.stringify(dump, null, 2), "utf-8");
console.log(`✓ 取回 ${dump.sessions?.length ?? 0} 场会话 → ${out}`);

const tail = await page.evaluate(() => document.getElementById("log").innerText);
console.log("---- 采集台尾部日志 ----");
console.log(tail.split("\n").slice(-20).join("\n"));

await browser.close();
