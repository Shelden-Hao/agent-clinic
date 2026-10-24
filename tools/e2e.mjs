/** 端到端验收：在真实浏览器里把用户能点的路径走一遍。
 *
 * 为什么必须有这一步：接口 200 只证明后端活着，不证明点得动。
 * 这个脚本跑完，才算「用户能自己走通」。
 */
import fs from "node:fs/promises";

const PW = "file:///C:/Users/86139/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js";
const pw = await import(PW);
const chromium = pw.chromium ?? pw.default?.chromium;

const base = process.argv[2] || "http://127.0.0.1:3111/";
const outDir = "runtime/e2e";
await fs.mkdir(outDir, { recursive: true });

const browser = await chromium.launch({
  executablePath: "C:/Program Files/Google/Chrome/Application/chrome.exe",
  headless: true,
});
const page = await browser.newPage({ viewport: { width: 1440, height: 980 }, deviceScaleFactor: 1.5 });

const fails = [];
const problems = [];
page.on("pageerror", (e) => problems.push(`[pageerror] ${String(e).slice(0, 200)}`));
page.on("console", (m) => m.type() === "error" && problems.push(`[console] ${m.text().slice(0, 160)}`));

function check(name, cond, extra = "") {
  if (cond) console.log(`  ✓ ${name}`);
  else {
    console.log(`  ✗ ${name} ${extra}`);
    fails.push(name);
  }
}

const bodyText = () => page.evaluate(() => document.body.innerText);

// ── 1. 概览 ──
console.log("\n[1] 概览页");
await page.goto(base + "#/", { waitUntil: "networkidle" });
await page.waitForTimeout(900);
let t = await bodyText();
check("出现结论句", /真实轨迹里/.test(t));
check("出现修复的代价", t.includes("修复的代价"));
check("出现评分矩阵", t.includes("逐项体检"));
check("出现成本画像", t.includes("成本画像"));

// ── 2. 轨迹浏览器筛选 ──
console.log("\n[2] 轨迹浏览器");
await page.goto(base + "#/runs", { waitUntil: "networkidle" });
await page.waitForTimeout(700);
const rowsAll = await page.locator("table.rt tbody tr").count();
await page.selectOption("select.sel >> nth=1", "bad");
await page.waitForTimeout(350);
const rowsBad = await page.locator("table.rt tbody tr").count();
check("失败筛选收窄结果", rowsBad > 0 && rowsBad < rowsAll, `${rowsAll} -> ${rowsBad}`);
await page.fill("input.inp", "过度转人工");
await page.waitForTimeout(350);
const rowsQ = await page.locator("table.rt tbody tr").count();
check("搜索病名有效", rowsQ > 0 && rowsQ <= rowsBad, `${rowsQ}`);
await page.fill("input.inp", "");

// ── 3. 点进详情 ──
console.log("\n[3] 单条详情");
await page.selectOption("select.sel >> nth=1", "");
await page.waitForTimeout(300);
await page.locator("table.rt tbody tr >> nth=0").click();
await page.waitForTimeout(1200);
t = await bodyText();
check("进入详情路由", page.url().includes("#/runs/"), page.url());
check("显示会诊结论", t.includes("会诊结论"));
check("显示执行时间线", t.includes("执行时间线"));
check("出现最小复现", t.includes("最小复现"));
check("标注未重放验证", t.includes("未经重放"));
check("显示体检评分", t.includes("体检评分"));
await page.screenshot({ path: `${outDir}/detail.png`, fullPage: true });

// ── 4. 现场接诊 ──
console.log("\n[4] 现场接诊");
await page.goto(base + "#/intake", { waitUntil: "networkidle" });
await page.waitForTimeout(600);
await page.click('button:has-text("OpenAI")');
await page.waitForTimeout(1200);
const filled = await page.inputValue("textarea.ta");
check("示例已载入输入框", filled.length > 500, `${filled.length} 字符`);

await page.click('button:has-text("开始会诊")');
await page.waitForTimeout(3500);
t = await bodyText();
check("识别出格式", t.includes("OpenAI Chat Completions"));
check("给出了失败诊断", t.includes("只问不做") || t.includes("过度转人工"), t.slice(-300));
check("提示任务级归因可用", t.includes("任务级归因"));
await page.screenshot({ path: `${outDir}/intake-result.png`, fullPage: true });

// 展开第一条
const runHead = page.locator(".run .rhead").first();
if (await runHead.count()) {
  await runHead.click();
  await page.waitForTimeout(600);
  t = await bodyText();
  check("可展开单条诊断", t.includes("结构推断") || t.includes("这类失败没有出错的那一步"));
  await page.screenshot({ path: `${outDir}/intake-expanded.png`, fullPage: true });
}

// 存进回归集
const before = await (await fetch(base.replace(/\/$/, "") + "/api/regression")).json();
const saveBtn = page.locator('.minihead button:has-text("存进回归集")').first();
if (await saveBtn.count()) {
  await saveBtn.click();
  await page.waitForTimeout(900);
  const after = await (await fetch(base.replace(/\/$/, "") + "/api/regression")).json();
  check("存进回归集生效", after.total >= before.total, `${before.total} -> ${after.total}`);
} else {
  check("存在存进回归集按钮", false);
}

// 回归验收
const verifyBtn = page.locator('button:has-text("用内置轨迹验收")').first();
if (await verifyBtn.count()) {
  await verifyBtn.click();
  await page.waitForTimeout(2600);
  t = await bodyText();
  check("回归验收给出结论", /条被考到|无法判断修复效果/.test(t), t.slice(-200));
  check("验收结果区分状态", /已修复|仍存在|不稳定|变成别的病|未覆盖/.test(t));
  await page.screenshot({ path: `${outDir}/verify.png`, fullPage: true });
} else {
  check("存在回归验收入口", false);
}

// ── 5. 失败模式 ──
console.log("\n[5] 失败模式");
await page.goto(base + "#/patterns", { waitUntil: "networkidle" });
await page.waitForTimeout(900);
t = await bodyText();
check("显示四类模式", (t.match(/P[1-4]/g) || []).length >= 4);
check("显示分叉对比", t.includes("分叉点"));
check("给出可粘贴的规则草稿", t.includes("可直接粘贴的规则草稿"));
await page.locator(".pattern").nth(1).click();
await page.waitForTimeout(500);
t = await bodyText();
check("切换模式刷新详情", t.includes("修复方向"));
await page.screenshot({ path: `${outDir}/patterns.png`, fullPage: true });

// ── 6. 移动端 ──
console.log("\n[6] 移动端视口");
await page.setViewportSize({ width: 390, height: 844 });
await page.goto(base + "#/", { waitUntil: "networkidle" });
await page.waitForTimeout(900);
const overflow = await page.evaluate(
  () => document.documentElement.scrollWidth - document.documentElement.clientWidth
);
check("首页无横向溢出", overflow <= 2, `溢出 ${overflow}px`);
await page.screenshot({ path: `${outDir}/mobile.png`, fullPage: false });

console.log("\n页面错误：" + (problems.length ? problems.length + " 条" : "无"));
for (const p of problems.slice(0, 8)) console.log("  " + p);
console.log(fails.length ? `\n❌ 未通过 ${fails.length} 项：${fails.join("、")}` : "\n✅ 全部通过");

await browser.close();
process.exit(fails.length ? 1 : 0);
