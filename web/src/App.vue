<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { fetchReport, type Report, type Step } from "./api";

const report = ref<Report | null>(null);
const error = ref<string | null>(null);
const activePattern = ref<string | null>(null);

onMounted(async () => {
  try {
    report.value = await fetchReport();
    activePattern.value = report.value.patterns[0]?.id ?? null;
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
  }
});

const pct = (n: number) => `${Math.round(n * 100)}%`;

const verdict = computed(() => {
  const s = report.value?.summary;
  if (!s) return "";
  const kinds = report.value?.patterns.length ?? 0;
  return `${s.total} 条真实轨迹里，${s.failed} 条失败。它们不是 ${s.failed} 个独立问题，而是 ${kinds} 类病。`;
});

const worst = computed(() => report.value?.patterns[0] ?? null);

const versionRows = computed(() => report.value?.versions ?? []);

const regression = computed(() => {
  const r = report.value?.regression;
  if (!r || (r.fixed.length === 0 && r.worse.length === 0)) return null;
  return r;
});

const active = computed(
  () => report.value?.patterns.find((p) => p.id === activePattern.value) ?? null
);

function argsBrief(args: Record<string, unknown>): string {
  const keys = Object.keys(args ?? {});
  if (!keys.length) return "";
  return keys.map((k) => `${k}=${JSON.stringify(args[k])}`).join(", ").slice(0, 72);
}

function stepLabel(s: Step | null): string {
  if (!s) return "";
  if (s.kind === "final") return "给出最终答复";
  return `${s.tool}(${argsBrief(s.args)})`;
}

const forkRows = computed(() => {
  const f = report.value?.fork;
  if (!f) return [];
  return f.rows.filter((r) => r.left?.kind !== "thought" && r.right?.kind !== "thought");
});
</script>

<template>
  <div class="wrap">
    <header class="head">
      <div>
        <h1>Agent 会诊室</h1>
        <p class="sub">
          记录工具告诉你「这一次发生了什么」。这里回答另一个问题：
          <b>你的 Agent 得了什么病、占几成、最短的复现路径是什么。</b>
        </p>
      </div>
      <div class="meta mono" v-if="report?.ready">
        <span>{{ report.summary.models.join(" / ") }}</span>
        <span class="dot">·</span>
        <span>{{ report.summary.tasks }} 个任务</span>
        <span class="dot">·</span>
        <span>{{ report.generated_at.slice(0, 16).replace("T", " ") }}</span>
      </div>
    </header>

    <p v-if="error" class="err">加载失败：{{ error }}</p>
    <p v-else-if="!report" class="loading mono">正在读取轨迹快照…</p>

    <template v-else-if="report.ready">
      <!-- 结论 -->
      <section class="verdict card">
        <div class="eyebrow">结论</div>
        <p class="verdict-text">{{ verdict }}</p>
        <p class="verdict-sub" v-if="worst">
          最致命的一类是 <b class="c-bad">{{ worst.label }}</b>：{{ worst.signature }}，
          占全部失败的 <b>{{ pct(worst.share) }}</b>，涉及 {{ worst.tasks.length }} 个任务。
        </p>
        <div class="stats">
          <div><b>{{ report.summary.total }}</b><span>轨迹</span></div>
          <div><b class="c-bad">{{ report.summary.failed }}</b><span>失败</span></div>
          <div><b>{{ pct(report.summary.fail_rate) }}</b><span>失败率</span></div>
          <div><b>{{ report.patterns.length }}</b><span>失败模式</span></div>
        </div>
      </section>

      <!-- 版本对比 -->
      <section v-if="versionRows.length > 1" class="card block">
        <div class="eyebrow">同一批任务 · 两个版本</div>
        <div class="vbar" v-for="v in versionRows" :key="v.version">
          <div class="vname mono">{{ v.version }}</div>
          <div class="track">
            <div class="fill" :style="{ width: pct(v.fail_rate) }" :class="{ low: v.fail_rate < 0.15 }"></div>
          </div>
          <div class="vnum mono">{{ pct(v.fail_rate) }} <span class="dim">失败 · {{ v.total }} 条 · 均 {{ v.avg_steps }} 步</span></div>
        </div>
        <p class="note">
          改完 prompt 之后失败率的变化，是「修复到底有没有用」最直接的证据 —— 也是这个工具存在的理由。
        </p>
      </section>

      <!-- 修复的代价 -->
      <section v-if="regression" class="block">
        <div class="eyebrow">修复的代价 · {{ regression.from }} → {{ regression.to }}</div>
        <div class="regrid">
          <div class="card reg">
            <h3 class="fixed">修好了 {{ regression.fixed.length }} 类</h3>
            <div v-if="!regression.fixed.length" class="empty">没有明显改善的类别</div>
            <div v-for="r in regression.fixed" :key="r.id" class="regrow">
              <span class="rlabel">{{ r.label }}</span>
              <span class="rnum mono">{{ r.old_n }} <i>→</i> {{ r.new_n }}</span>
            </div>
          </div>
          <div class="card reg bad">
            <h3 class="worse">弄坏了 {{ regression.worse.length }} 类</h3>
            <div v-if="!regression.worse.length" class="empty">没有新增的退化</div>
            <div v-for="r in regression.worse" :key="r.id" class="regrow">
              <span class="rlabel">{{ r.label }}</span>
              <span class="rnum mono up">{{ r.old_n }} <i>→</i> {{ r.new_n }}</span>
            </div>
          </div>
        </div>
        <p class="note">
          总失败率变化 <b class="mono">{{ (regression.net! * 100).toFixed(1) }} 个百分点</b>。
          只看总分会把「修好两类、弄坏一类」这件事整个藏起来 —— 这正是需要逐类对比的原因。
        </p>
      </section>

      <!-- 失败模式 -->
      <section class="block">
        <div class="eyebrow">失败模式</div>
        <div class="patterns">
          <button
            v-for="p in report.patterns"
            :key="p.id"
            class="pattern card"
            :class="{ active: p.id === activePattern }"
            @click="activePattern = p.id"
          >
            <div class="prow">
              <span class="pid mono">{{ p.id }}</span>
              <span class="plabel">{{ p.label }}</span>
              <span class="pcount mono">{{ p.count }}</span>
            </div>
            <div class="pbar"><i :style="{ width: pct(p.share) }"></i></div>
            <div class="psig">{{ p.signature }}</div>
            <div class="pfoot mono">
              <span>{{ pct(p.share) }}</span>
              <span>{{ p.tool === "-" ? "跨工具" : p.tool }}</span>
              <span>{{ p.tasks.length }} 个任务</span>
            </div>
          </button>
        </div>

        <div v-if="active" class="card detail">
          <div class="eyebrow">这一类到底在发生什么</div>
          <p class="detail-ev">{{ active.signature }}</p>
          <dl class="kv mono">
            <dt>性质</dt><dd>{{ active.label }}（{{ active.kind }}）</dd>
            <dt>出现工具</dt><dd>{{ active.tool }}</dd>
            <dt>版本分布</dt>
            <dd>
              <span v-for="(c, v) in active.versions" :key="v" class="tag" style="margin-right:6px">{{ v }} × {{ c }}</span>
            </dd>
            <dt>涉及任务</dt><dd>{{ active.tasks.join("、") }}</dd>
          </dl>
          <div class="advice">
            <div class="eyebrow">修复方向</div>
            <p>{{ active.advice }}</p>
          </div>
        </div>
      </section>

      <!-- 分叉对比 -->
      <section v-if="report.fork" class="block">
        <div class="eyebrow">分叉点 · 同一条任务，两条路径</div>
        <p class="taskline">
          <span class="mono tag">{{ report.fork.task_id }}</span>
          {{ report.fork.task_prompt }}
        </p>
        <div class="fork card">
          <div class="fork-head">
            <div class="ok-side">成功 <span class="mono dim">{{ report.fork.good_run }}</span></div>
            <div class="mid"></div>
            <div class="bad-side">失败 <span class="mono dim">{{ report.fork.bad_run }}</span></div>
          </div>
          <div
            v-for="(row, i) in forkRows"
            :key="i"
            class="frow"
            :class="{
              diverge: row.right?.step_index === report.fork.fork_at,
              missL: !row.left,
              missR: !row.right,
            }"
          >
            <div class="cell left mono">
              <span v-if="row.left" :class="{ err: row.left.ok === false }">{{ stepLabel(row.left) }}</span>
              <span v-else class="ghost">—</span>
            </div>
            <div class="seam">
              <span v-if="row.right?.step_index === report.fork.fork_at" class="forkmark">分叉</span>
            </div>
            <div class="cell right mono">
              <span v-if="row.right" :class="{ err: row.right.ok === false }">{{ stepLabel(row.right) }}</span>
              <span v-else class="ghost">—</span>
            </div>
          </div>
        </div>
        <p v-if="report.fork.diagnosis" class="verdict-sub">
          归因：<b class="c-bad">{{ report.fork.diagnosis.label }}</b>
          · 第 {{ report.fork.diagnosis.step_index }} 步 · 置信度
          {{ report.fork.diagnosis.confidence }} —— {{ report.fork.diagnosis.evidence }}
        </p>
      </section>

      <!-- 局限 -->
      <section class="block limits card">
        <div class="eyebrow">这份报告不声称什么</div>
        <ul>
          <li>它只解释<b>已有轨迹</b>，不会替你重新跑一遍模型。想验证修复，就再采一批。</li>
          <li>归因是规则式的，判不出来时会明确写「未归类」，而不是编一个理由。</li>
          <li>「置信度」是规则的证据强度，不是统计意义上的概率。</li>
          <li>
            样本量：{{ report.summary.total }} 条。低于 30 条时，占比数字只应当看趋势，不应当当结论。
          </li>
        </ul>
      </section>
    </template>

    <p v-else class="loading mono">
      数据集尚未采集：{{ report.reason }}
    </p>

    <footer class="foot mono">
      Agent 会诊室 · 轨迹来自浏览器端真实运行，分析全部离线完成 ·
      <a href="/api/docs">接口</a>
    </footer>
  </div>
</template>

<style scoped>
.head { display: flex; justify-content: space-between; align-items: flex-start; gap: 24px; flex-wrap: wrap; margin-bottom: 34px; }
h1 { font-size: 24px; margin: 0 0 8px; letter-spacing: 0.3px; }
.sub { margin: 0; color: var(--muted); max-width: 60ch; font-size: 14px; }
.sub b { color: var(--text); font-weight: 600; }
.meta { color: var(--dim); font-size: 12px; display: flex; gap: 8px; align-items: center; }
.meta .dot { color: var(--line-2); }

.verdict { padding: 28px 30px; background: linear-gradient(180deg, #101a1e 0%, #10151a 60%); }
.verdict-text { font-size: 22px; line-height: 1.5; margin: 12px 0 10px; letter-spacing: 0.2px; }
.verdict-sub { color: var(--muted); margin: 0; font-size: 14px; }
.verdict-sub b { color: var(--text); }
.c-bad { color: var(--red); }

.stats { display: flex; gap: 40px; margin-top: 22px; flex-wrap: wrap; }
.stats div { display: flex; flex-direction: column; }
.stats b { font: 600 26px/1.2 var(--mono); }
.stats span { font-size: 12px; color: var(--dim); }

.block { margin-top: 34px; }
.block > .eyebrow { margin-bottom: 14px; }

.vbar { display: flex; align-items: center; gap: 16px; padding: 12px 20px; }
.vname { width: 34px; color: var(--muted); font-size: 13px; }
.track { flex: 1; height: 8px; background: #1a2026; border-radius: 999px; overflow: hidden; }
.fill { height: 100%; background: linear-gradient(90deg, #b8454c, var(--red)); border-radius: 999px; }
.fill.low { background: linear-gradient(90deg, #2f7d55, var(--green)); }
.vnum { width: 220px; text-align: right; font-size: 13px; }
.dim { color: var(--dim); }
.note { margin: 4px 20px 18px; color: var(--dim); font-size: 12.5px; }

.patterns { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 12px; }.pattern { padding: 16px 18px; text-align: left; color: inherit; cursor: pointer; font: inherit; transition: border-color .15s, background .15s; }
.pattern:hover { border-color: var(--line-2); }
.pattern.active { border-color: var(--cyan); background: #0f1a1d; }
.prow { display: flex; align-items: baseline; gap: 10px; }
.pid { color: var(--dim); font-size: 11px; }
.plabel { font-weight: 600; font-size: 15px; }
.pcount { margin-left: auto; font-size: 18px; color: var(--red); }
.pbar { height: 4px; background: #1a2026; border-radius: 999px; margin: 10px 0; overflow: hidden; }
.pbar i { display: block; height: 100%; background: var(--red); border-radius: 999px; }
.psig { font-size: 12.5px; color: var(--muted); line-height: 1.5; min-height: 40px; }
.pfoot { display: flex; gap: 12px; font-size: 11px; color: var(--dim); margin-top: 10px; }

.detail { margin-top: 14px; padding: 22px 24px; }
.detail-ev { font-size: 15px; margin: 10px 0 16px; padding-left: 12px; border-left: 2px solid var(--red); }
.kv { display: grid; grid-template-columns: 90px 1fr; gap: 8px 14px; font-size: 12.5px; }
.kv dt { color: var(--dim); }
.kv dd { margin: 0; }
.advice { margin-top: 18px; padding: 14px 16px; background: #0d1216; border: 1px solid var(--line); border-radius: 8px; }
.advice p { margin: 8px 0 0; font-size: 13.5px; color: var(--muted); }

.taskline { margin: 0 0 12px; color: var(--muted); font-size: 13.5px; }
.fork { overflow: hidden; }
.fork-head { display: grid; grid-template-columns: 1fr 72px 1fr; padding: 12px 18px; border-bottom: 1px solid var(--line); font-size: 12px; }
.ok-side { color: var(--green); }
.bad-side { color: var(--red); text-align: right; }
.mid { }
.frow { display: grid; grid-template-columns: 1fr 72px 1fr; border-top: 1px solid #161c22; font-size: 12px; }
.frow:first-of-type { border-top: 0; }
.cell { padding: 9px 20px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cell.right { text-align: right; }
.cell .err { color: var(--red); }
.cell .ghost { color: #2f3944; }
.seam { display: flex; align-items: center; justify-content: center; }
.forkmark { font: 9px/1 var(--mono); display:inline-block; color: #04211f; background: var(--amber); padding: 3px 4px; border-radius: 3px; letter-spacing: 1px; }
.frow.diverge { background: #1b1214; box-shadow: inset 3px 0 0 var(--red); }
.frow.missL .cell.left, .frow.missR .cell.right { background: repeating-linear-gradient(45deg, #0d1216, #0d1216 6px, #0a0e12 6px, #0a0e12 12px); }

.regrid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.reg { padding: 18px 20px; }
.reg.bad { border-color: #3a2226; background: #120f11; }
.reg h3 { font-size: 12px; letter-spacing: 1.2px; text-transform: uppercase; margin: 0 0 14px; font-family: var(--mono); }
.reg h3.fixed { color: var(--green); }
.reg h3.worse { color: var(--red); }
.regrow { display: flex; justify-content: space-between; align-items: baseline; padding: 7px 0; border-top: 1px solid #161c22; font-size: 13.5px; }
.regrow:first-of-type { border-top: 0; }
.rlabel { color: var(--text); }
.rnum { color: var(--muted); font-size: 12.5px; }
.rnum i { color: var(--dim); font-style: normal; padding: 0 3px; }
.rnum.up { color: var(--red); }
.empty { color: var(--dim); font-size: 13px; }
@media (max-width: 720px) { .regrid { grid-template-columns: 1fr; } }

.limits { padding: 22px 26px; }.limits ul { margin: 10px 0 0; padding-left: 18px; color: var(--muted); font-size: 13.5px; }
.limits li { margin-bottom: 6px; }
.limits b { color: var(--text); }

.err { color: var(--red); }
.loading { color: var(--dim); margin: 40px 0; }
.foot { margin-top: 56px; color: var(--dim); font-size: 11.5px; border-top: 1px solid var(--line); padding-top: 18px; }
</style>
