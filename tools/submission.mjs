/** 投稿截图：生成一组「比赛能直接交」的图。
 *
 * 和 shots.mjs 的区别：
 * - 这份要控制信息密度，长截图评委不会读完，所以关键是「一屏之内讲清一件事」
 * - 输出到 submission/（进仓库，随作品一起交付）
 * - 顺带出一张手机端，证明它不是只能在宽屏看
 */
import fs from "node:fs/promises";

const PW = "file:///C:/Users/86139/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js";
const pw = await import(PW);
const chromium = pw.chromium ?? pw.default?.chromium;

const base = process.argv[2] || "http://127.0.0.1:3131/";
const out = "submission";
await fs.mkdir(out, { recursive: true });

const browser = await chromium.launch({
  executablePath: "C:/Program Files/Google/Chrome/Application/chrome.exe",
  headless: true,
});
const ctx = await browser.newContext({
  viewport: { width: 1440, height: 1000 },
  deviceScaleFactor: 2,
});
const page = await ctx.newPage();
const problems = [];
page.on("pageerror", (e) => problems.push(String(e).slice(0, 160)));

// sticky 侧栏在整页截图里会停在中途，摊平它
await page.addInitScript(() => {
  document.addEventListener("DOMContentLoaded", () => {
    const s = document.createElement("style");
    s.textContent = ".side{position:static!important;height:auto!important;min-height:100vh}";
    document.head.appendChild(s);
  });
});

const go = async (hash) => {
  await page.goto(base + hash, { waitUntil: "networkidle" });
  await page.waitForTimeout(1500);
};

// 01 一屏结论：它到底在说什么
await go("#/");
await page.screenshot({ path: `${out}/01-会诊报告.png` });
await page.screenshot({ path: `${out}/备选-会诊报告-整页.png`, fullPage: true });

// 02 多轮会话：单轮测试漏掉的那一层（本轮新增的平台级能力）
// 它讲的是前面九项能力都讲不到的事：有些轮次的用户输入里根本没有订单号，
// 单轮测试连样本都造不出来；而「被历史害了」的轮次一旦漏检，给的是假绿灯。
await go("#/sessions");
await page.screenshot({ path: `${out}/02-多轮会话.png` });
await page.screenshot({ path: `${out}/备选-多轮会话-整页.png`, fullPage: true });

// 03 Prompt 版本墙：从诊断到演进的闭环（本轮新增）
await go("#/prompts");
await page.screenshot({ path: `${out}/03-Prompt版本墙.png` });
await page.locator(".tab", { hasText: "两版对比" }).click();
await page.waitForTimeout(1200);
await page.screenshot({ path: `${out}/备选-Prompt版本diff.png`, fullPage: true });

// 备选：实验对账 / 复核队列 / 多轮详情 / 实时流 / 失败模式 / 单条轨迹 / 现场接诊
await go("#/experiments");
await page.locator("tr.mrow").first().click();
await page.waitForTimeout(400);
await page.locator(".dacts button").first().click();
await page.waitForTimeout(1200);
await page.locator("section.block >> nth=1").screenshot({ path: `${out}/备选-实验对账.png` });
await page.screenshot({ path: `${out}/备选-实验对账-整页.png`, fullPage: true });

await go("#/review");
await page.screenshot({ path: `${out}/备选-复核队列.png` });
await page.screenshot({ path: `${out}/备选-复核队列-整页.png`, fullPage: true });

await go("#/sessions/S01-v1-1");
await page.screenshot({ path: `${out}/备选-多轮详情.png`, fullPage: true });

await go("#/live");
await page.locator("button", { hasText: "回放样本" }).click();
await page.waitForTimeout(2600);
await page.screenshot({ path: `${out}/备选-实时流.png` });

await go("#/patterns");
await page.screenshot({ path: `${out}/备选-失败模式.png`, fullPage: true });
await go("#/runs/T02-v1-1");
await page.screenshot({ path: `${out}/备选-单条轨迹.png`, fullPage: true });
await go("#/intake");
await page.screenshot({ path: `${out}/备选-现场接诊.png`, fullPage: true });

// 手机端
const m = await ctx.newPage();
await m.setViewportSize({ width: 390, height: 844 });
await m.goto(base + "#/", { waitUntil: "networkidle" });
await m.waitForTimeout(1200);
await m.screenshot({ path: `${out}/备选-手机端.png` });

const files = await fs.readdir(out);
for (const f of files.sort()) {
  const s = await fs.stat(`${out}/${f}`);
  console.log(`  ${f}  ${Math.round(s.size / 1024)}KB`);
}
console.log(problems.length ? `\n页面错误 ${problems.length} 条` : "\n没有页面错误");
await browser.close();
