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
check("出现逐任务对账", t.includes("逐任务对账"));
check("出现修复的代价", t.includes("修复的代价"));
check("出现评分矩阵", t.includes("逐项体检"));
check("出现成本画像", t.includes("成本画像"));
check("概览带复核状态", t.includes("复核队列") && /条置信度不足以直接采信/.test(t));

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

// ── 4. 实验对比 ──
console.log("\n[4] 实验对比");
await page.goto(base + "#/experiments", { waitUntil: "networkidle" });
await page.waitForTimeout(900);
t = await bodyText();
check("出现对账结论", t.includes("两版之间") || t.includes("真正修好了") || t.includes("对账结论"));
check("点明「一半概率失败」", t.includes("一半概率失败") || t.includes("抛硬币"));
check("有稳定性分层", t.includes("失败率看不见的那一层"));
check("矩阵列出全部任务", (await page.locator("tr.mrow").count()) === 12, `${await page.locator("tr.mrow").count()} 行`);
// 病名必须显示中文，不能把 over_escalation 这种内部 key 印在界面上
const mtxText = await page.locator("table.mtx").innerText();
check("矩阵里没有内部 kind 泄漏", !/over_escalation|promised_but_not_done|asked_instead_of_acted/.test(mtxText), mtxText.slice(0, 120));
// 展开一行，应出现它的轨迹入口
await page.locator("tr.mrow").first().click();
await page.waitForTimeout(500);
check("点行可展开轨迹", (await page.locator("a.drun").count()) > 0);
check("展开后给出死因变化", /死因从|换个病名/.test(await bodyText()));

// 并排对比：从「哪个任务坏了」下钻到「差在哪一步」
await page.locator(".dacts button").first().click();
await page.waitForTimeout(1100);
check("并排对齐出结果", (await page.locator(".cmprow").count()) >= 4, `${await page.locator(".cmprow").count()} 行`);
check("标出分叉步或说明结构一致", /两条路径不再一致|调用结构完全一致/.test(await bodyText()));
check("对比区标注了口径", (await bodyText()).includes("决策签名"));
await page.screenshot({ path: `${outDir}/experiments.png`, fullPage: true });

// ── 5. 复核队列（含真实签字流程） ──
console.log("\n[5] 复核队列");
const rq0 = await (await fetch(base.replace(/\/$/, "") + "/api/review?stats_only=1")).json();
await page.goto(base + "#/review", { waitUntil: "networkidle" });
await page.waitForTimeout(800);
t = await bodyText();
check("队列只收少数诊断", rq0.queued > 0 && rq0.queued < rq0.total_failed / 2, `${rq0.queued}/${rq0.total_failed}`);
check("每条都写明为何可疑", t.includes("为什么把它挑出来"));
check("给出同版本对照", t.includes("同版本的其他轨迹"));
check("待签字条目数正确", (await page.locator(".rv:not(.done)").count()) === rq0.pending, `${await page.locator(".rv:not(.done)").count()}`);

// 5.1 确认：待签字减一，分布不变
await page.locator('.rv:not(.done) button:has-text("确认机器判定")').first().click();
await page.waitForTimeout(1200);
const rq1 = await (await fetch(base.replace(/\/$/, "") + "/api/review?stats_only=1")).json();
check("确认后待签字减少", rq1.pending === rq0.pending - 1, `${rq0.pending} -> ${rq1.pending}`);
check("确认不改动病种分布", JSON.stringify(rq1.after) === JSON.stringify(rq1.before));
check("确认被计入动作统计", rq1.actions.confirm === 1, `${rq1.actions.confirm}`);

// 5.2 撤销，回到待签字
await page.locator('.rv.done button:has-text("撤销复核")').first().click();
await page.waitForTimeout(1200);
const rq2 = await (await fetch(base.replace(/\/$/, "") + "/api/review?stats_only=1")).json();
check("撤销后回到待签字", rq2.pending === rq0.pending, `${rq2.pending}`);

// 5.3 改判：分布必须真的变
await page.locator('.rv:not(.done) button:has-text("改判为另一种病")').first().click();
await page.waitForTimeout(500);
await page.selectOption("select.finput", { index: 0 });
await page.fill("input.finput.grow", "端到端验收：机器把「没有动作」判错了");
await page.locator('button:has-text("提交改判")').click();
await page.waitForTimeout(1400);
const rq3 = await (await fetch(base.replace(/\/$/, "") + "/api/review?stats_only=1")).json();
check("改判被记录", rq3.actions.override === 1, `${rq3.actions.override}`);
check("改判改变了病种分布", JSON.stringify(rq3.after) !== JSON.stringify(rq3.before));
t = await bodyText();
check("界面显示复核影响表", t.includes("复核对结论的影响"));
check("界面出现签章", t.includes("已签字") || t.includes("改判为"));
await page.screenshot({ path: `${outDir}/review-signed.png`, fullPage: true });

// 5.4 改判必须写理由 —— 前端拦不住时后端也要拦
const strict = await fetch(base.replace(/\/$/, "") + "/api/review/" + encodeURIComponent(rq3.pending >= 0 ? "T05-v1-2" : "x"), {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ action: "override", kind: "loop", note: "" }),
});
check("无理由改判被拒绝", strict.status === 400, `${strict.status}`);

// 收尾：把这条改判撤销，别把验收痕迹留在数据里
await page.locator('.rv.done button:has-text("撤销复核")').first().click();
await page.waitForTimeout(1000);
const rq4 = await (await fetch(base.replace(/\/$/, "") + "/api/review?stats_only=1")).json();
check("验收后队列复原", rq4.pending === rq0.pending && rq4.resolved === 0, `${rq4.pending}/${rq4.resolved}`);

// ── 6. 现场接诊 ──
console.log("\n[6] 现场接诊");
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

// ── 7. 失败模式 ──
console.log("\n[7] 失败模式");
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

// ── 8. 移动端 ──
console.log("\n[8] 移动端视口");
await page.setViewportSize({ width: 390, height: 844 });
for (const [label, hash] of [
  ["首页", "#/"],
  ["实验矩阵", "#/experiments"],
  ["轨迹浏览器", "#/runs"],
  ["失败模式", "#/patterns"],
  ["复核队列", "#/review"],
]) {
  await page.goto(base + hash, { waitUntil: "networkidle" });
  await page.waitForTimeout(700);
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth
  );
  check(`${label}无横向溢出`, overflow <= 2, `溢出 ${overflow}px`);
}
await page.goto(base + "#/", { waitUntil: "networkidle" });
await page.waitForTimeout(700);
await page.screenshot({ path: `${outDir}/mobile.png`, fullPage: false });

console.log("\n页面错误：" + (problems.length ? problems.length + " 条" : "无"));
for (const p of problems.slice(0, 8)) console.log("  " + p);
console.log(fails.length ? `\n❌ 未通过 ${fails.length} 项：${fails.join("、")}` : "\n✅ 全部通过");

await browser.close();
process.exit(fails.length ? 1 : 0);
