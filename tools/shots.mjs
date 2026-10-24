/** 一次截完所有路由，用于自检与投稿材料。
 *
 * 用法：
 *   node tools/shots.mjs                       # 本地 3111
 *   node tools/shots.mjs https://xxx/?sharecode=xxx
 */
import fs from "node:fs/promises";

const PW = "file:///C:/Users/86139/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js";
const pw = await import(PW);
const chromium = pw.chromium ?? pw.default?.chromium;

const base = process.argv[2] || "http://127.0.0.1:3111/";
const outDir = process.argv[3] || "runtime/shots";

const ROUTES = [
  { name: "01-overview", hash: "#/" },
  { name: "02-explorer", hash: "#/runs" },
  { name: "03-run-detail", hash: "#/runs/T02-v1-1" },
  { name: "04-patterns", hash: "#/patterns" },
  { name: "05-intake", hash: "#/intake" },
];

await fs.mkdir(outDir, { recursive: true });

const browser = await chromium.launch({
  executablePath: "C:/Program Files/Google/Chrome/Application/chrome.exe",
  headless: true,
});
const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 1.6 });
const page = await ctx.newPage();

const problems = [];
page.on("pageerror", (e) => problems.push(`[pageerror] ${String(e).slice(0, 240)}`));
page.on("console", (m) => {
  if (m.type() === "error") problems.push(`[console] ${m.text().slice(0, 200)}`);
});
page.on("requestfailed", (r) => problems.push(`[requestfailed] ${r.url().slice(0, 120)}`));

for (const r of ROUTES) {
  await page.goto(base + r.hash, { waitUntil: "networkidle" });
  await page.waitForTimeout(1400);
  await page.screenshot({ path: `${outDir}/${r.name}.png`, fullPage: true });
  const t = await page.evaluate(() => document.body.innerText.replace(/\s+/g, " ").trim());
  console.log(`${r.name}: ${t.length} 字符 | ${t.slice(0, 110)}`);
}

console.log(problems.length ? `\n问题 ${problems.length} 条：\n` + problems.slice(0, 12).join("\n") : "\n没有页面错误");
await browser.close();
