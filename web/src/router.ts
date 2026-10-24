/** 极简 hash 路由。
 *
 * 为什么不用 vue-router：整个应用只有 5 条路由、零嵌套、零守卫。
 * 为这点需求引入一个库，换来的是一个不在我控制内的版本风险和 20KB 体积。
 * 40 行手写路由能做到同样的事，而且它在这个环境里绝对不会构建失败 ——
 * 对一个要发布给人看的作品，能跑比工程化正确更重要。
 */

import { computed, ref } from "vue";

export interface Route {
  name: "overview" | "runs" | "run" | "patterns" | "experiments" | "review" | "intake" | "notfound";
  param?: string;
}

function parse(hash: string): Route {
  const path = (hash || "#/").replace(/^#/, "").split("?")[0];
  const seg = path.split("/").filter(Boolean);
  if (seg.length === 0) return { name: "overview" };
  if (seg[0] === "runs" && seg[1]) return { name: "run", param: decodeURIComponent(seg[1]) };
  if (seg[0] === "runs") return { name: "runs" };
  if (seg[0] === "patterns") return { name: "patterns" };
  if (seg[0] === "experiments") return { name: "experiments" };
  if (seg[0] === "review") return { name: "review" };
  if (seg[0] === "intake") return { name: "intake" };
  return { name: "notfound" };
}

export const route = ref<Route>(parse(location.hash));

window.addEventListener("hashchange", () => {
  route.value = parse(location.hash);
  window.scrollTo({ top: 0 });
});

export function go(path: string): void {
  location.hash = path.startsWith("#") ? path : `#${path}`;
}

export const activeNav = computed(() => {
  const r = route.value;
  if (r.name === "run") return "runs";
  return r.name;
});

export const NAV = [
  { key: "overview", path: "/", label: "会诊报告", hint: "结论与修复代价" },
  { key: "experiments", path: "/experiments", label: "实验对比", hint: "逐任务看翻转" },
  { key: "runs", path: "/runs", label: "轨迹浏览器", hint: "逐条下钻" },
  { key: "patterns", path: "/patterns", label: "失败模式", hint: "归纳与最小复现" },
  { key: "review", path: "/review", label: "复核队列", hint: "机器判不准的" },
  { key: "intake", path: "/intake", label: "现场接诊", hint: "接入你的轨迹" },
] as const;
