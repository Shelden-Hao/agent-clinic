/**
 * 驱动线上采集台跑一批真实轨迹，并把结果取回本地。
 *
 * 为什么不用 curl：模型调用发生在浏览器端（平台无密钥通道按 Origin 放行），
 * 所以必须用真实浏览器跑完，再把服务端收到的轨迹拉回来。
 *
 * 用法：
 *   node tools/drive-collect.mjs --version v1 --repeat 2 --only T01,T02 --out runtime/batch1.json
 */

// playwright-core 装在隔离工作区里，不能污染项目根目录（否则部署时会被当成 Node 项目）
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
const out = arg("out", "runtime/batch.json");
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
page.on("requestfailed", (r) => console.log("  [reqfail]", r.url().slice(0, 160), r.failure()?.errorText));

console.log(`→ 打开分享链接建立会话`);
await page.goto(BASE + "/" + SHARE, { waitUntil: "domcontentloaded" });
await page.waitForTimeout(1500);

if (reset) {
  await page.evaluate(() => fetch("/api/lab/reset", { method: "POST" }));
  console.log(`→ 已清空上一批轨迹`);
}

console.log(`→ 进入采集台 version=${version} repeat=${repeat} only=${only || "全部"}`);
await page.goto(`${BASE}/lab/collect.html?${params}`, { waitUntil: "domcontentloaded" });
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
    console.log(`  ${st.state}: ${st.done} 条已跑 / 失败 ${st.failed}`);
  }
  if (st.state === "done" || st.state === "aborted") break;
  await page.waitForTimeout(2000);
}

const corpus = await page.evaluate(async () => {
  const r = await fetch("/api/lab/dump");
  return r.json();
});

await fs.mkdir(path.dirname(out), { recursive: true }).catch(() => {});
await fs.writeFile(out, JSON.stringify(corpus, null, 2), "utf-8");
console.log(`✓ 取回 ${corpus.runs?.length ?? 0} 条轨迹 → ${out}`);

const tail = await page.evaluate(() => document.getElementById("log").innerText);
console.log("---- 采集台尾部日志 ----");
console.log(tail.split("\n").slice(-14).join("\n"));

await browser.close();
