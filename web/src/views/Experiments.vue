<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { api, type CompareResult, type Experiments as ExpData, type ExpRow } from "../api";
import { go } from "../router";

const data = ref<ExpData | null>(null);
const error = ref<string | null>(null);
const open = ref<string | null>(null);

/** 并排对比：当前正在看的那一对轨迹 */
const cmp = ref<CompareResult | null>(null);
const cmpTask = ref<string | null>(null);
const cmpBusy = ref(false);

onMounted(async () => {
  try {
    data.value = await api.experiments();
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
  }
});

const versions = computed(() => data.value?.versions ?? []);
const base = computed(() => data.value?.baseline ?? "");
const cand = computed(() => data.value?.candidate ?? "");

/** 堆叠条：稳定通过 / 抛硬币 / 稳定失败。三段宽度按任务数占比算。 */
function stack(v: string) {
  const s = data.value?.stability?.[v];
  const total = (s?.stable_ok ?? 0) + (s?.flaky ?? 0) + (s?.stable_bad ?? 0);
  const pct = (n: number) => (total ? (n / total) * 100 : 0);
  return {
    ok: pct(s?.stable_ok ?? 0),
    flaky: pct(s?.flaky ?? 0),
    bad: pct(s?.stable_bad ?? 0),
    counts: s ?? { stable_ok: 0, flaky: 0, stable_bad: 0 },
    total,
  };
}

const n = computed(() => data.value?.narrative ?? null);

/** 判定标签的配色。负值（弄坏了/更糟了）红，正值绿，抛硬币类琥珀色。 */
function tone(v: string): string {
  if (v === "regressed" || v === "worse") return "bad";
  if (v === "fixed") return "ok";
  if (v === "flaky_improved" || v === "flaky") return "warn";
  if (v === "stable_ok") return "plain";
  return "dim";
}

function cell(row: ExpRow, v: string) {
  return row.cells[v] ?? { ok: 0, total: 0, kinds: [], kind_labels: [], runs: [] };
}

function toggle(id: string) {
  open.value = open.value === id ? null : id;
  if (open.value !== id) {
    cmp.value = null;
    cmpTask.value = null;
  }
}

/** 从一格里挑一条轨迹：优先要成功/失败的，没有再退而求其次 */
function pick(row: ExpRow, v: string, wantSuccess: boolean): string | null {
  const runs = cell(row, v).runs ?? [];
  return (runs.find((r) => r.success === wantSuccess) ?? runs[0])?.run_id ?? null;
}

/** 对比默认这样挑：基线里一条成功的，对候选里一条失败的。
 *  反过来的情况（这版反而修好了）也支持，选不到就退而求其次。 */
async function runCompare(row: ExpRow) {
  const b = pick(row, base.value, true) ?? pick(row, base.value, false);
  const c = pick(row, cand.value || base.value, false) ?? pick(row, cand.value || base.value, true);
  if (!b || !c || b === c) return;
  cmpBusy.value = true;
  cmpTask.value = row.task_id;
  try {
    cmp.value = await api.compare(b, c);
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
  } finally {
    cmpBusy.value = false;
  }
}

/** 分叉点用「第一条两边不一致的行」，而不是「第一条只出现在右边的行」。
 *  后者在 LCS 把中间某步对齐到前面时，会落到一个看起来不太像分叉的位置上。 */
const diverge = computed(() => {
  const rows = cmp.value?.rows ?? [];
  const i = rows.findIndex((r) => r.relation !== "same");
  if (i < 0) return null;
  const r = rows[i];
  return { index: i, step: r.left?.step_index ?? r.right?.step_index ?? null };
});

function brief(s: { tool?: string | null; args?: Record<string, unknown>; content?: string; kind: string } | null) {
  if (!s) return "—";
  if (s.kind === "tool_call") {
    const keys = Object.keys(s.args || {});
    return `${s.tool}(${keys.join(", ")})`;
  }
  return (s.content || "").slice(0, 60) || s.kind;
}
</script>

<template>
  <p v-if="error" class="err">加载失败：{{ error }}</p>
  <p v-else-if="!data" class="loading">正在读取实验矩阵…</p>

  <template v-else>
    <header class="head">
      <h1>同一批任务，两次实验，逐条对账</h1>
      <p class="sub">
        版本汇总只有一行失败率。它把一个致命事实整个盖住了：同一批任务里，
        <b>有的被修好了，有的被弄坏了，还有的从「稳定失败」变成了「一半概率失败」</b>。
        这三件事在总分里长一个样。
      </p>
      <div class="meta mono">
        <span>{{ base }} → {{ cand || base }}</span>
        <span class="dot">·</span>
        <span>{{ data.rows.length }} 个任务</span>
        <span class="dot">·</span>
        <span v-if="data.generated_at">{{ data.generated_at.slice(0, 16).replace("T", " ") }}</span>
      </div>
    </header>

    <!-- 结论 -->
    <section v-if="n" class="verdict card">
      <div class="eyebrow">对账结论</div>
      <p class="verdict-text">{{ n.headline }}</p>
      <p v-if="n.detail" class="verdict-sub">{{ n.detail }}</p>
      <p v-if="n.stability" class="verdict-sub">{{ n.stability }}</p>
      <p v-if="n.migration" class="verdict-sub">{{ n.migration }}</p>
      <div class="stats">
        <div>
          <b class="c-ok">{{ n.counts.fixed ?? 0 }}</b>
          <span>修好了</span>
        </div>
        <div>
          <b class="c-warn">{{ n.counts.flaky_improved ?? 0 }}</b>
          <span>变好了但不稳</span>
        </div>
        <div>
          <b class="c-bad">{{ (n.counts.regressed ?? 0) + (n.counts.worse ?? 0) }}</b>
          <span>弄坏了</span>
        </div>
        <div>
          <b class="dim">{{ (n.counts.stable_bad ?? 0) + (n.counts.stable_ok ?? 0) }}</b>
          <span>没动</span>
        </div>
      </div>
    </section>

    <!-- 稳定性：失败率看不见的那一层 -->
    <section v-if="versions.length > 1" class="block">
      <div class="eyebrow">稳定性 · 失败率看不见的那一层</div>
      <div class="card stab">
        <div v-for="v in versions" :key="v" class="stabrow">
          <div class="stabname mono">{{ v }}</div>
          <div class="stabbar">
            <i class="s-ok" :style="{ width: stack(v).ok + '%' }" :title="`稳定通过 ${stack(v).counts.stable_ok}`"></i>
            <i class="s-fl" :style="{ width: stack(v).flaky + '%' }" :title="`抛硬币 ${stack(v).counts.flaky}`"></i>
            <i class="s-bad" :style="{ width: stack(v).bad + '%' }" :title="`稳定失败 ${stack(v).counts.stable_bad}`"></i>
          </div>
          <div class="stabnum mono">
            <span class="c-ok">{{ stack(v).counts.stable_ok }}</span> /
            <span class="c-warn">{{ stack(v).counts.flaky }}</span> /
            <span class="c-bad">{{ stack(v).counts.stable_bad }}</span>
          </div>
        </div>
        <div class="legend mono">
          <span><i class="dot s-ok"></i>稳定通过</span>
          <span><i class="dot s-fl"></i>抛硬币（跑两次结果不同）</span>
          <span><i class="dot s-bad"></i>稳定失败</span>
        </div>
      </div>
      <p class="note">
        「抛硬币」指同一个任务、同一份 prompt、跑两次结果不一样。
        这种任务会稀释失败率，也会让逐条归因失去代表性 ——
        所以它们还会被自动送进
        <a href="#/review" class="linky">复核队列</a>。
      </p>
    </section>

    <!-- 矩阵 -->
    <section class="block">
      <div class="eyebrow">逐任务矩阵 · 点任意一行展开它的轨迹</div>
      <div class="card">
        <table class="mtx">
          <thead>
            <tr>
              <th class="c-task">任务</th>
              <th v-for="v in versions" :key="v">{{ v }}</th>
              <th class="c-verdict">判定</th>
            </tr>
          </thead>
          <tbody>
            <template v-for="row in data.rows" :key="row.task_id">
              <tr class="mrow" :class="{ op: open === row.task_id }" @click="toggle(row.task_id)">
                <td class="c-task">
                  <span class="tid mono">{{ row.task_id }}</span>
                  <span v-if="row.fragile" class="flag" title="该任务在某个版本内跑两次结果不同">抛硬币</span>
                </td>
                <td v-for="v in versions" :key="v" class="c-cell">
                  <div class="ratio mono" :class="{ all: cell(row, v).ok === cell(row, v).total && cell(row, v).total > 0, none: cell(row, v).ok === 0 && cell(row, v).total > 0 }">
                    <i
                      v-for="k in cell(row, v).total"
                      :key="k"
                      class="pip"
                      :class="{ on: k <= cell(row, v).ok }"
                    ></i>
                    <span>{{ cell(row, v).ok }}/{{ cell(row, v).total }}</span>
                  </div>
                  <div v-if="cell(row, v).kind_labels.length" class="kinds mono">
                    {{ cell(row, v).kind_labels.join(" · ") }}
                  </div>
                </td>
                <td class="c-verdict">
                  <span class="vtag" :class="tone(row.verdict)">{{ row.verdict_label }}</span>
                </td>
              </tr>
              <tr v-if="open === row.task_id" :key="row.task_id + '-d'" class="detail">
                <td :colspan="versions.length + 2">
                  <p class="dprompt">{{ row.prompt }}</p>
                  <div class="druns">
                    <div v-for="v in versions" :key="v" class="dgroup">
                      <div class="dlabel mono">{{ v }}</div>
                      <a
                        v-for="r in cell(row, v).runs"
                        :key="r.run_id"
                        class="drun mono"
                        :class="{ bad: !r.success }"
                        :href="'#/runs/' + encodeURIComponent(r.run_id)"
                        @click.stop="go('/runs/' + r.run_id)"
                      >
                        <i class="spip" :class="{ on: r.success }"></i>{{ r.run_id }}
                      </a>
                      <div v-if="!cell(row, v).runs.length" class="empty">没有轨迹</div>
                    </div>
                  </div>
                  <p v-if="row.from_kinds.length && row.to_kinds.length && row.from_kinds[0] !== row.to_kinds[0]" class="dnote">
                    死因从「{{ row.from_labels[0] }}」换成了「{{ row.to_labels[0] }}」——
                    它始终是失败，只是失败的理由变了。
                  </p>
                  <div class="dacts">
                    <button class="btn tiny" :disabled="cmpBusy" @click.stop="runCompare(row)">
                      {{ cmpBusy && cmpTask === row.task_id ? "正在对齐…" : "对比这两版差在哪一步" }}
                    </button>
                  </div>

                  <!-- 并排对齐结果 -->
                  <div v-if="cmp && cmpTask === row.task_id" class="cmpbox">
                    <div class="cmphead">
                      <div class="cmpcol">
                        <span class="cmptag mono">{{ cmp.a.version }}</span>
                        <span class="cmprun mono">{{ cmp.a.run_id }}</span>
                        <span class="tag" :class="cmp.a.success ? 'ok' : 'bad'">
                          {{ cmp.a.success ? "成功" : cmp.a.kind_label }}
                        </span>
                      </div>
                      <div class="cmpvs mono">对齐</div>
                      <div class="cmpcol right">
                        <span class="cmptag mono">{{ cmp.b.version }}</span>
                        <span class="cmprun mono">{{ cmp.b.run_id }}</span>
                        <span class="tag" :class="cmp.b.success ? 'ok' : 'bad'">
                          {{ cmp.b.success ? "成功" : cmp.b.kind_label }}
                        </span>
                      </div>
                    </div>

                    <div v-for="(r, i) in cmp.rows" :key="i" class="cmprow" :class="[r.relation, { fork: diverge && diverge.index === i }]">
                      <div class="cs">
                        <span v-if="r.left" class="cskill mono">{{ brief(r.left) }}</span>
                        <span v-else class="csnone mono">—</span>
                      </div>
                      <div class="cgap mono">{{ r.relation === "same" ? "=" : r.relation === "only_left" ? "◀" : "▶" }}</div>
                      <div class="cs">
                        <span v-if="r.right" class="cskill mono">{{ brief(r.right) }}</span>
                        <span v-else class="csnone mono">—</span>
                      </div>
                    </div>

                    <p class="cmpfoot">
                      <template v-if="diverge">
                        从第 <b>{{ diverge.step }}</b> 步开始，两条路径不再一致（高亮行）。
                        之后每一处单边动作，都是两个版本做出的不同选择。
                      </template>
                      <template v-else>
                        两条路径的调用结构完全一致 —— 差别只在参数或返回结果里，不在「做了什么」。
                      </template>
                      {{ cmp.note }}
                    </p>
                  </div>
                </td>
              </tr>
            </template>
          </tbody>
        </table>
      </div>
      <p class="note">{{ data.caveat }}</p>
    </section>

    <!-- 病种迁移 -->
    <section v-if="data.migration.changed" class="block">
      <div class="eyebrow">病种迁移 · 换了死法，没换结局</div>
      <div class="card mig">
        <div v-for="m in data.migration.items" :key="m.task_id" class="migrow">
          <span class="mtask mono">{{ m.task_id }}</span>
          <span class="mk mono old">{{ m.from_label }}</span>
          <span class="marrow">→</span>
          <span class="mk mono new">{{ m.to_label }}</span>
        </div>
      </div>
      <p v-if="data.migration.top" class="note">
        新死因最集中的是
        <b class="c-warn">{{ data.migration.top.label }}</b>
        （{{ data.migration.top.tasks.join("、") }}）。
        换个病名不叫修复，只是把问题挪到了更难被发现的地方。
      </p>
    </section>

    <!-- 版本指标 -->
    <section class="block">
      <div class="eyebrow">版本指标 · 总分与它的局限</div>
      <div class="card">
        <table class="vt mono">
          <thead>
            <tr>
              <th>版本</th>
              <th>轨迹</th>
              <th>失败</th>
              <th>失败率</th>
              <th>均步数</th>
              <th>tokens</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="v in data.version_rows" :key="v.version">
              <td>{{ v.version }}</td>
              <td>{{ v.total }}</td>
              <td>{{ v.failed }}</td>
              <td :class="v.fail_rate > 0.5 ? 'c-bad' : ''">{{ (v.fail_rate * 100).toFixed(1) }}%</td>
              <td>{{ v.avg_steps }}</td>
              <td>{{ v.tokens.toLocaleString("zh-CN") }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p class="note">
        这两行就是多数工具给你的全部内容。上面的矩阵才是它的下半句 ——
        <b>同一个失败率，可以由完全不同的任务构成。</b>
      </p>
    </section>

    <div class="cta">
      <button class="btn" @click="go('/review')">看机器判不准的那几条 →</button>
      <button class="btn" @click="go('/patterns')">看失败模式与最小复现 →</button>
      <button class="btn primary" @click="go('/intake')">接入你自己的轨迹 →</button>
    </div>
  </template>
</template>

<style scoped>
.head { margin-bottom: 30px; }
h1 { font-size: 23px; margin: 0 0 8px; letter-spacing: 0.3px; font-weight: 600; }
.sub { margin: 0; color: var(--muted); max-width: 66ch; font-size: 14px; }
.sub b { color: var(--text); font-weight: 600; }
.meta { color: var(--dim); font-size: 11.5px; display: flex; gap: 8px; align-items: center; margin-top: 12px; }
.meta .dot { color: var(--line-2); }

.verdict { padding: 28px 30px; background: linear-gradient(180deg, #101a1e 0%, #10151a 60%); }
.verdict-text { font-size: 20px; line-height: 1.6; margin: 12px 0 10px; letter-spacing: 0.2px; }
.verdict-sub { color: var(--muted); margin: 0 0 8px; font-size: 13.5px; line-height: 1.7; }
.stats { display: flex; gap: 40px; margin-top: 20px; flex-wrap: wrap; }
.stats div { display: flex; flex-direction: column; }
.stats b { font: 600 24px/1.2 var(--mono); }
.stats span { font-size: 11.5px; color: var(--dim); }

/* 稳定性堆叠条 */
.stab { padding: 20px 22px; }
.stabrow { display: flex; align-items: center; gap: 16px; padding: 7px 0; }
.stabname { width: 34px; color: var(--muted); font-size: 13px; }
.stabbar { flex: 1; height: 14px; display: flex; border-radius: 4px; overflow: hidden; background: #161c22; }
.stabbar i { display: block; height: 100%; }
.s-ok { background: #2f7d55; }
.s-fl { background: #8a6420; }
.s-bad { background: #7d2f34; }
.stabnum { width: 90px; text-align: right; font-size: 12.5px; }
.legend { display: flex; gap: 20px; margin-top: 14px; padding-top: 14px; border-top: 1px solid #161c22; font-size: 10.5px; color: var(--dim); flex-wrap: wrap; }
.legend span { display: flex; align-items: center; gap: 6px; }
.legend .dot { width: 8px; height: 8px; border-radius: 2px; display: block; }

/* 矩阵 */
.mtx { width: 100%; border-collapse: collapse; font-size: 13px; }
.mtx th {
  text-align: left; padding: 12px 16px; font: 500 10.5px/1 var(--mono);
  letter-spacing: .8px; color: var(--dim); border-bottom: 1px solid var(--line);
}
.mtx th.c-verdict, .mtx th.c-cell { text-align: right; }
.mtx td { padding: 10px 16px; border-bottom: 1px solid #151b21; vertical-align: middle; }
.mtx tr:last-child td { border-bottom: 0; }
.mrow { cursor: pointer; }
.mrow:hover { background: #0e1418; }
.mrow.op { background: #101a1e; }
.c-task { white-space: nowrap; }
.tid { font-size: 13px; color: var(--text); }
.flag {
  margin-left: 8px; font: 9.5px/1 var(--mono); padding: 3px 5px; border-radius: 3px;
  color: #f0cd93; border: 1px solid #45351d; background: #1c1609;
}
.c-cell { text-align: right; }
.ratio { display: inline-flex; align-items: center; gap: 3px; font-size: 12.5px; color: var(--muted); }
.ratio.all { color: var(--green); }
.ratio.none { color: var(--red); }
.ratio span { margin-left: 5px; }
.pip { width: 6px; height: 6px; border-radius: 999px; background: #2a333d; display: block; }
.pip.on { background: currentColor; }
.kinds { font-size: 10px; color: var(--dim); margin-top: 3px; }
.c-verdict { text-align: right; white-space: nowrap; }
.vtag { font: 11px/1 var(--mono); padding: 4px 8px; border-radius: 4px; border: 1px solid var(--line-2); color: var(--muted); }
.vtag.bad { color: #ffb3b7; border-color: #4a2226; background: #1d1012; }
.vtag.ok { color: #a8e8c6; border-color: #1e4131; background: #0f1d16; }
.vtag.warn { color: #f0cd93; border-color: #45351d; background: #1c1609; }
.vtag.dim { color: var(--dim); }

.detail td { background: #0b1013; padding: 16px 20px 20px; border-bottom: 1px solid var(--line); }
.dprompt { margin: 0 0 14px; color: var(--muted); font-size: 13px; line-height: 1.6; }
.druns { display: flex; gap: 32px; flex-wrap: wrap; }
.dlabel { font-size: 10px; color: var(--dim); letter-spacing: .6px; margin-bottom: 6px; }
.drun {
  display: block; font-size: 12px; color: var(--cyan); text-decoration: none;
  padding: 2px 0; border-bottom: 1px dotted #243038;
}
.drun:hover { color: #9de6df; }
.dnote { margin: 14px 0 0; font-size: 12.5px; color: var(--c-warn, #e0a34a); }
.drun.bad { color: #c9757a; }
.spip { width: 5px; height: 5px; border-radius: 999px; background: var(--red); display: inline-block; margin-right: 6px; }
.spip.on { background: var(--green); }
.dacts { margin-top: 14px; }

/* 并排对齐 */
.cmpbox { margin-top: 16px; padding-top: 16px; border-top: 1px dashed #1e262d; }
.cmphead { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
.cmpcol { display: flex; align-items: center; gap: 8px; flex: 1; flex-wrap: wrap; }
.cmpcol.right { justify-content: flex-end; }
.cmptag { font-size: 10px; color: var(--dim); border: 1px solid var(--line-2); padding: 3px 6px; border-radius: 3px; }
.cmprun { font-size: 11.5px; color: var(--muted); }
.cmpvs { font-size: 9.5px; color: var(--dim); letter-spacing: 1px; }
.cmprow { display: grid; grid-template-columns: 1fr 28px 1fr; align-items: center; gap: 6px; padding: 6px 8px; border-radius: 5px; font-size: 11.5px; }
.cmprow + .cmprow { margin-top: 2px; }
.cmprow.same { background: transparent; }
.cmprow.only_left, .cmprow.only_right { background: #12181d; }
.cmprow.fork { background: #1d1012; outline: 1px solid #4a2226; }
.cs { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cskill { color: var(--muted); }
.cmprow.fork .cskill { color: #ffb3b7; }
.csnone { color: #2f3942; }
.cgap { text-align: center; color: var(--dim); font-size: 10px; }
.cmpfoot { margin: 14px 0 0; font-size: 11.5px; color: var(--dim); line-height: 1.7; }
.cmpfoot b { color: var(--c-warn, #e0a34a); }

/* 迁移 */
.mig { padding: 8px 20px; }
.migrow { display: flex; align-items: center; gap: 14px; padding: 9px 0; border-top: 1px solid #151b21; font-size: 13px; }
.migrow:first-child { border-top: 0; }
.mtask { width: 40px; color: var(--muted); }
.mk { font-size: 12px; color: var(--dim); }
.mk.new { color: #f0cd93; }
.marrow { color: var(--line-2); }

/* 版本指标 */
.vt { width: 100%; border-collapse: collapse; font-size: 12.5px; }
.vt th { text-align: right; padding: 12px 18px; font-weight: 500; color: var(--dim); font-size: 10.5px; letter-spacing: .8px; border-bottom: 1px solid var(--line); }
.vt th:first-child { text-align: left; }
.vt td { text-align: right; padding: 10px 18px; border-bottom: 1px solid #151b21; }
.vt td:first-child { text-align: left; color: var(--text); }
.vt tr:last-child td { border-bottom: 0; }

.linky { text-decoration: none; }
.cta { display: flex; gap: 10px; margin-top: 34px; flex-wrap: wrap; }

@media (max-width: 900px) {
  .mtx th, .mtx td { padding: 9px 10px; }
  .kinds { display: none; }
}
</style>
