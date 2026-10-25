/** 极简 hash 路由。
 *
 * 为什么不用 vue-router：整个应用只有 9 条路由、零嵌套、零守卫。
 * 为这点需求引入一个库，换来的是一个不在我控制内的版本风险和 20KB 体积。
 * 60 行手写路由能做到同样的事，而且它在这个环境里绝对不会构建失败 ——
 * 对一个要发布给人看的作品，能跑比工程化正确更重要。
 */

import { computed, ref } from "vue";

export interface Route {
  name:
    | "overview"
    | "runs"
    | "run"
    | "patterns"
    | "sessions"
    | "session"
    | "experiments"
    | "prompts"
    | "review"
    | "live"
    | "intake"
    | "notfound";
  param?: string;
}

function parse(hash: string): Route {
  const path = (hash || "#/").replace(/^#/, "").split("?")[0];
  const seg = path.split("/").filter(Boolean);
  if (seg.length === 0) return { name: "overview" };
  if (seg[0] === "runs" && seg[1]) return { name: "run", param: decodeURIComponent(seg[1]) };
  if (seg[0] === "runs") return { name: "runs" };
  if (seg[0] === "patterns") return { name: "patterns" };
  if (seg[0] === "sessions" && seg[1]) return { name: "session", param: decodeURIComponent(seg[1]) };
  if (seg[0] === "sessions") return { name: "sessions" };
  if (seg[0] === "experiments") return { name: "experiments" };
  if (seg[0] === "prompts") return { name: "prompts" };
  if (seg[0] === "review") return { name: "review" };
  if (seg[0] === "live") return { name: "live" };
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
  if (r.name === "session") return "sessions";
  return r.name;
});

/** 导航分三组。
 *
 * 分组依据是「你此刻在做什么事」，不是「功能分类」：
 * 诊断（这系统哪里坏了）→ 演进（改了什么、有没有变好）→ 接入（把数据弄进来）。
 * 九项平铺会让侧栏变成一条找不到东西的长列表。
 */
export const NAV_GROUPS = [
  {
    group: "诊断",
    items: [
      { key: "overview", path: "/", label: "会诊报告", hint: "结论与修复代价" },
      { key: "runs", path: "/runs", label: "轨迹浏览器", hint: "逐条下钻" },
      { key: "patterns", path: "/patterns", label: "失败模式", hint: "归纳与最小复现" },
      { key: "sessions", path: "/sessions", label: "多轮会话", hint: "单轮测不出的那层" },
    ],
  },
  {
    group: "演进",
    items: [
      { key: "experiments", path: "/experiments", label: "实验对比", hint: "逐任务看翻转" },
      { key: "prompts", path: "/prompts", label: "Prompt 版本", hint: "改了什么、为什么" },
      { key: "review", path: "/review", label: "复核队列", hint: "机器判不准的" },
    ],
  },
  {
    group: "接入",
    items: [
      { key: "live", path: "/live", label: "实时流", hint: "SDK 边跑边诊" },
      { key: "intake", path: "/intake", label: "现场接诊", hint: "导入你的轨迹" },
    ],
  },
] as const;
