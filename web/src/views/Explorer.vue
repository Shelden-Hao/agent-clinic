<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { api, type Report, type RunRow } from "../api";
import { go } from "../router";

const report = ref<Report | null>(null);
const error = ref<string | null>(null);

const q = ref("");
const version = ref("");
const outcome = ref<"" | "ok" | "bad" | "unknown">("");
const kind = ref("");
const sortKey = ref<"run" | "tokens" | "score">("run");

onMounted(async () => {
  try {
    report.value = await api.overview();
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
  }
});

const rows = computed(() => report.value?.runs ?? []);

const kinds = computed(() => {
  const m = new Map<string, string>();
  for (const r of rows.value) if (r.kind !== "ok") m.set(r.kind, r.label);
  return [...m.entries()].map(([kind, label]) => ({ kind, label }));
});

const filtered = computed(() => {
  const needle = q.value.trim().toLowerCase();
  let out = rows.value;
  if (version.value) out = out.filter((r) => r.version === version.value);
  if (outcome.value === "ok") out = out.filter((r) => r.success);
  if (outcome.value === "bad") out = out.filter((r) => !r.success);
  if (outcome.value === "unknown") out = out.filter((r) => r.kind === "unknown");
  if (kind.value) out = out.filter((r) => r.kind === kind.value);
  if (needle)
    out = out.filter(
      (r) =>
        r.run_id.toLowerCase().includes(needle) ||
        r.task_id.toLowerCase().includes(needle) ||
        r.label.toLowerCase().includes(needle) ||
        r.tools.some((t) => t.toLowerCase().includes(needle))
    );
  const copy = [...out];
  if (sortKey.value === "tokens") copy.sort((a, b) => b.tokens - a.tokens);
  else if (sortKey.value === "score") copy.sort((a, b) => (a.overall ?? 9) - (b.overall ?? 9));
  return copy;
});

function reset() {
  q.value = "";
  version.value = "";
  outcome.value = "";
  kind.value = "";
  sortKey.value = "run";
}

function confClass(c: number | null): string {
  if (c == null) return "dim";
  if (c >= 0.85) return "c-ok";
  if (c >= 0.6) return "c-warn";
  return "c-bad";
}

function rowClass(r: RunRow): string {
  if (r.success) return "ok";
  if (r.kind === "unknown") return "unk";
  return "bad";
}

const fmt = (n: number) => n.toLocaleString("zh-CN");
</script>

<template>
  <p v-if="error" class="err">加载失败：{{ error }}</p>
  <p v-else-if="!report" class="loading">正在读取轨迹…</p>

  <template v-else>
    <header class="head">
      <h1>轨迹浏览器</h1>
      <p class="sub">
        每条轨迹都能下钻到「第几步走岔、这一步为什么被判成病、抽掉哪些步之后仍然复现」。
        列表里的<b>评分</b>是独立于归因规则算出来的，两条路径可以互相印证。
      </p>
    </header>

    <div class="filters card">
      <input v-model="q" class="inp" type="search" placeholder="搜索 run_id / 任务 / 工具 / 病名" />
      <select v-model="version" class="sel">
        <option value="">全部版本</option>
        <option v-for="v in report.catalog.versions" :key="v" :value="v">{{ v }}</option>
      </select>
      <select v-model="outcome" class="sel">
        <option value="">全部结局</option>
        <option value="bad">失败</option>
        <option value="ok">成功</option>
        <option value="unknown">未归类 / 未判定</option>
      </select>
      <select v-model="kind" class="sel">
        <option value="">全部病种</option>
        <option v-for="k in kinds" :key="k.kind" :value="k.kind">{{ k.label }}</option>
      </select>
      <select v-model="sortKey" class="sel">
        <option value="run">按编号</option>
        <option value="tokens">按 token 降序</option>
        <option value="score">按评分升序</option>
      </select>
      <button class="btn tiny" @click="reset">重置</button>
      <span class="count mono">{{ filtered.length }} / {{ rows.length }}</span>
    </div>

    <div class="tablewrap card">
      <table class="rt mono">
        <thead>
          <tr>
            <th>轨迹</th>
            <th>任务</th>
            <th class="r">版本</th>
            <th class="r">步</th>
            <th class="r">tokens</th>
            <th>会诊结论</th>
            <th class="r">置信</th>
            <th class="r">评分</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in filtered" :key="r.run_id" :class="rowClass(r)" @click="go('/runs/' + r.run_id)">
            <td class="rid">{{ r.run_id }}</td>
            <td class="dim">{{ r.task_id }}</td>
            <td class="r dim">{{ r.version }}</td>
            <td class="r">{{ r.steps }}</td>
            <td class="r">{{ fmt(r.tokens) }}</td>
            <td>
              <span v-if="r.success" class="tag ok">成功</span>
              <span v-else-if="r.kind === 'unknown'" class="tag warn">未归类</span>
              <span v-else class="tag bad">{{ r.label }}</span>
              <span v-if="r.step_index != null && r.step_index > 0 && !r.success" class="at dim">@{{ r.step_index }}</span>
            </td>
            <td class="r" :class="confClass(r.confidence)">
              {{ r.confidence == null ? "—" : r.confidence.toFixed(2) }}
            </td>
            <td class="r" :class="(r.overall ?? 1) < 0.7 ? 'c-bad' : (r.overall ?? 1) < 0.9 ? 'c-warn' : 'c-ok'">
              {{ r.overall == null ? "—" : r.overall.toFixed(2) }}
            </td>
          </tr>
          <tr v-if="!filtered.length">
            <td colspan="8" class="empty">没有符合条件的轨迹，试试放宽筛选。</td>
          </tr>
        </tbody>
      </table>
    </div>

    <p class="note">
      共 {{ rows.length }} 条，模型 {{ report.catalog.models.join(" / ") }}。
      点击任意一行进入单条会诊 —— 那里有逐步标注、因果链最小化和评分明细。
    </p>
  </template>
</template>

<style scoped>
.head { margin-bottom: 24px; }
h1 { font-size: 22px; margin: 0 0 8px; font-weight: 600; }
.sub { margin: 0; color: var(--muted); max-width: 66ch; font-size: 13.5px; }
.sub b { color: var(--text); }

.filters { display: flex; gap: 8px; padding: 12px 14px; align-items: center; flex-wrap: wrap; margin-bottom: 12px; }
.inp, .sel {
  font: 12px/1 var(--mono); padding: 8px 10px; border-radius: 7px;
  background: #0c1116; color: var(--text); border: 1px solid var(--line-2);
}
.inp { flex: 1; min-width: 200px; }
.inp:focus, .sel:focus { outline: none; border-color: #2f5d59; }
.count { margin-left: auto; font-size: 11px; color: var(--dim); }

.tablewrap { overflow-x: auto; }
.rt { width: 100%; border-collapse: collapse; font-size: 12.5px; min-width: 720px; }
.rt th {
  text-align: left; padding: 11px 14px; font-size: 10.5px; letter-spacing: .8px;
  color: var(--dim); font-weight: 500; border-bottom: 1px solid var(--line); white-space: nowrap;
}
.rt th.r, .rt td.r { text-align: right; }
.rt td { padding: 9px 14px; border-bottom: 1px solid #141a20; }
.rt tbody tr { cursor: pointer; transition: background .1s; }
.rt tbody tr:hover { background: #131a20; }
.rt tbody tr.bad { box-shadow: inset 2px 0 0 #5a262b; }
.rt tbody tr.unk { box-shadow: inset 2px 0 0 #4a3c1d; }
.rid { color: var(--text); white-space: nowrap; }
.at { font-size: 10px; margin-left: 6px; }
@media (max-width: 720px) { .inp { min-width: 140px; } }
</style>
