/** 对线上页面截图，用于自检视觉效果与投稿材料。 */
import fs from "node:fs/promises";

const PW = "file:///C:/Users/86139/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.js";
const pw = await import(PW);
const chromium = pw.chromium ?? pw.default?.chromium;

const url = process.argv[2] || "https://agent-clinic.app.workbuddy.host/?sharecode=HRSKWOV2YB";
const out = process.argv[3] || "runtime/shot.png";
const full = process.argv[4] !== "viewport";

const browser = await chromium.launch({
  executablePath: "C:/Program Files/Google/Chrome/Application/chrome.exe",
  headless: true,
});
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 2 });
page.on("pageerror", (e) => console.log("[pageerror]", String(e).slice(0, 300)));
page.on("console", (m) => m.type() === "error" && console.log("[console]", m.text().slice(0, 200)));

await page.goto(url, { waitUntil: "networkidle" });
await page.waitForTimeout(2500);
await page.screenshot({ path: out, fullPage: full });
console.log(`shot -> ${out}`);

const text = await page.evaluate(() => document.body.innerText);
console.log("---- 页面文本（前 600 字）----");
console.log(text.slice(0, 600));
await browser.close();
await fs.access(out);
