<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { api, type Report } from "../api";
import { go } from "../router";

const report = ref<Report | null>(null);
const error = ref<string | null>(null);

onMounted(async () => {
  try {
    report.value = await api.overview();
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
  }
});

const pct = (n: number) => `${Math.round(n * 100)}%`;
const num = (n: number) => n.toLocaleString("zh-CN");

const verdict = computed(() => {
  const s = report.value?.summary;
  if (!s) return "";
  return `${s.total} 条真实轨迹里，${s.failed} 条失败。它们不是 ${s.failed} 个独立问题，而是 ${report.value?.patterns.length} 类病。`;
});

const worst = computed(() => report.value?.patterns[0] ?? null);

const regression = computed(() => {
  const r = report.value?.regression;
  if (!r || (r.fixed.length === 0 && r.worse.length === 0)) return null;
  return r;
});

/** 评分矩阵：把两个版本的每一项得分摆在一起，退化项自动标红。 */
const scoreRows = computed(() => {
  const e = report.value?.evaluations;
  if (!e) return [];
  const versions = Object.keys(e.by_version);
  if (versions.length < 2) return [];
  const [a, b] = versions;
  return e.evaluators.map((ev) => {
    const va = e.by_version[a].per_evaluator[ev.key];
    const vb = e.by_version[b].per_evaluator[ev.key];
    return {
      key: ev.key,
      label: ev.label,
      from: va,
      to: vb,
      delta: va != null && vb != null ? vb - va : null,
    };
  });
});

const cost = computed(() => report.value?.cost ?? null);
const maxHist = computed(() => Math.max(1, ...(cost.value?.steps_histogram ?? []).map((h) => h.runs)));
</script>

<template>
  <p v-if="error" class="err">加载失败：{{ error }}</p>
  <p v-else-if="!report" class="loading">正在读取轨迹快照…</p>

  <template v-else-if="report.ready">
    <header class="head">
      <h1>一次会诊，而不是一次日志回放</h1>
      <p class="sub">
        记录工具告诉你「这一次发生了什么」。这里回答另外三个问题：
        <b>你的 Agent 得了什么病、占几成、最短的复现路径是什么。</b>
      </p>
      <div class="meta mono">
        <span>{{ report.summary.models.join(" / ") }}</span>
        <span class="dot">·</span>
        <span>{{ report.summary.tasks }} 个任务</span>
        <span class="dot">·</span>
        <span>{{ report.generated_at.slice(0, 16).replace("T", " ") }}</span>
      </div>
    </header>

    <section class="verdict card">
      <div class="eyebrow">结论</div>
      <p class="verdict-text">{{ verdict }}</p>
      <p class="verdict-sub" v-if="worst">
        最致命的一类是
        <a href="#/patterns" class="linky"><b class="c-bad">{{ worst.label }}</b></a>
        ：{{ worst.signature }}，占全部失败的 <b>{{ pct(worst.share) }}</b>，涉及
        {{ worst.tasks.length }} 个任务。
      </p>
      <div class="stats">
        <div><b>{{ report.summary.total }}</b><span>轨迹</span></div>
        <div><b class="c-bad">{{ report.summary.failed }}</b><span>失败</span></div>
        <div><b>{{ pct(report.summary.fail_rate) }}</b><span>失败率</span></div>
        <div><b>{{ report.patterns.length }}</b><span>失败模式</span></div>
      </div>
    </section>

    <!-- 版本对比 -->
    <section v-if="report.versions.length > 1" class="card block">
      <div class="pad">
        <div class="eyebrow">同一批任务 · 两个版本</div>
      </div>
      <div class="vbar" v-for="v in report.versions" :key="v.version">
        <div class="vname mono">{{ v.version }}</div>
        <div class="track">
          <div class="fill" :style="{ width: pct(v.fail_rate) }" :class="{ low: v.fail_rate < 0.15 }"></div>
        </div>
        <div class="vnum mono">
          {{ pct(v.fail_rate) }}
          <span class="dim">失败 · {{ v.total }} 条 · 均 {{ v.avg_steps }} 步</span>
        </div>
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

    <!-- 评分矩阵 -->
    <section v-if="scoreRows.length" class="block">
      <div class="eyebrow">逐项体检 · 评分器看同一批数据</div>
      <div class="card">
        <table class="sgrid mono">
          <thead>
            <tr>
              <th>评分项</th>
              <th v-for="v in report.versions" :key="v.version">{{ v.version }}</th>
              <th>变化</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="s in scoreRows" :key="s.key">
              <td class="sc-label">{{ s.label }}</td>
              <td>{{ s.from ?? "—" }}</td>
              <td>{{ s.to ?? "—" }}</td>
              <td :class="s.delta == null ? 'dim' : s.delta < 0 ? 'c-bad' : s.delta > 0 ? 'c-ok' : 'dim'">
                {{ s.delta == null ? "未参与" : (s.delta > 0 ? "+" : "") + s.delta.toFixed(3) }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <p class="note">
        这张表是<b>独立于诊断规则</b>算出来的：评分器只看轨迹本身，不知道归因结果。
        它和上面的「修复代价」互相印证 —— 两条独立路径指向同一个结论，那个结论才站得住。
        注意 <b>转人工策略</b> 这一项是往下走的。
      </p>
    </section>

    <!-- 成本 -->
    <section v-if="cost" class="block">
      <div class="eyebrow">成本画像 · 失败到底贵在哪</div>
      <div class="costgrid">
        <div class="card cbox">
          <div class="eyebrow">单条均值</div>
          <div class="cbig mono">{{ num(cost.overall.tokens_avg) }}<span>tokens</span></div>
          <div class="csmall mono">
            均 {{ cost.overall.steps_avg }} 步 · p90 {{ num(cost.overall.tokens_p90) }} tokens
          </div>
        </div>
        <div class="card cbox">
          <div class="eyebrow">失败 vs 成功</div>
          <div class="cbig mono" :class="(cost.failure_premium?.tokens_delta ?? 0) > 0 ? 'c-bad' : 'c-warn'">
            {{ (cost.failure_premium?.tokens_delta ?? 0) > 0 ? "+" : "" }}{{ num(cost.failure_premium?.tokens_delta ?? 0) }}
            <span>tokens / 条</span>
          </div>
          <div class="csmall mono">
            步数差 {{ cost.failure_premium?.steps_delta }} 步 · 失败 {{ cost.failure_premium?.steps_ratio }}× 步数
          </div>
        </div>
        <div class="card cbox hist">
          <div class="eyebrow">步数分布</div>
          <div class="bars">
            <div v-for="h in cost.steps_histogram" :key="h.steps" class="bar">
              <i :style="{ height: (h.runs / maxHist) * 100 + '%' }"></i>
              <b class="mono">{{ h.steps }}</b>
            </div>
          </div>
        </div>
      </div>
      <p v-if="cost.failure_premium" class="note">{{ cost.failure_premium.verdict }}</p>
      <div class="kinds">
        <div v-for="k in cost.by_kind" :key="k.label" class="krow">
          <span class="klabel">{{ k.label }}</span>
          <span class="kbar"><i :style="{ width: (k.runs / report.summary.total) * 100 + '%' }"></i></span>
          <span class="knum mono">{{ k.runs }} 条 · {{ num(k.tokens_avg) }} tok · {{ k.steps_avg }} 步</span>
        </div>
      </div>
    </section>

    <section class="block limits card">
      <div class="eyebrow">这份报告不声称什么</div>
      <ul>
        <li>它只解释<b>已有轨迹</b>，不会替你重新跑一遍模型。想验证修复，就再采一批。</li>
        <li>归因是规则式的，判不出来时会明确写「未归类」，而不是编一个理由。</li>
        <li>「置信度」是规则的证据强度，不是统计意义上的概率。</li>
        <li>「最小复现」是<b>结构推断</b>，没有经过重跑验证 —— 界面上每一处都会原样标注这一点。</li>
        <li>样本量 {{ report.summary.total }} 条。低于 30 条时，占比数字只应看趋势，不应当结论。</li>
      </ul>
    </section>

    <div class="cta">
      <button class="btn" @click="go('/runs')">逐条看轨迹 →</button>
      <button class="btn" @click="go('/patterns')">看失败模式与最小复现 →</button>
      <button class="btn primary" @click="go('/intake')">接入你自己的轨迹 →</button>
    </div>
  </template>

  <p v-else class="loading">数据集尚未采集：{{ report.reason }}</p>
</template>

<style scoped>
.head { margin-bottom: 30px; }
h1 { font-size: 23px; margin: 0 0 8px; letter-spacing: 0.3px; font-weight: 600; }
.sub { margin: 0; color: var(--muted); max-width: 62ch; font-size: 14px; }
.sub b { color: var(--text); font-weight: 600; }
.meta { color: var(--dim); font-size: 11.5px; display: flex; gap: 8px; align-items: center; margin-top: 12px; }
.meta .dot { color: var(--line-2); }

.verdict { padding: 28px 30px; background: linear-gradient(180deg, #101a1e 0%, #10151a 60%); }
.verdict-text { font-size: 21px; line-height: 1.55; margin: 12px 0 10px; letter-spacing: 0.2px; }
.verdict-sub { color: var(--muted); margin: 0; font-size: 14px; }
.verdict-sub b { color: var(--text); }
.linky { text-decoration: none; }
.c-bad { color: var(--red); }
.stats { display: flex; gap: 42px; margin-top: 22px; flex-wrap: wrap; }
.stats div { display: flex; flex-direction: column; }
.stats b { font: 600 25px/1.2 var(--mono); }
.stats span { font-size: 11.5px; color: var(--dim); }

.pad { padding: 20px 20px 0; }
.vbar { display: flex; align-items: center; gap: 16px; padding: 12px 20px; }
.vname { width: 34px; color: var(--muted); font-size: 13px; }
.track { flex: 1; height: 8px; background: #1a2026; border-radius: 999px; overflow: hidden; }
.fill { height: 100%; background: linear-gradient(90deg, #b8454c, var(--red)); border-radius: 999px; }
.fill.low { background: linear-gradient(90deg, #2f7d55, var(--green)); }
.vnum { width: 230px; text-align: right; font-size: 13px; }
.vbar + .note { margin: 4px 20px 18px; }

.regrid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.reg { padding: 18px 20px; }
.reg.bad { border-color: #3a2226; background: #120f11; }
.reg h3 { font-size: 11.5px; letter-spacing: 1.2px; text-transform: uppercase; margin: 0 0 14px; font-family: var(--mono); }
.reg h3.fixed { color: var(--green); }
.reg h3.worse { color: var(--red); }
.regrow { display: flex; justify-content: space-between; align-items: baseline; padding: 7px 0; border-top: 1px solid #161c22; font-size: 13.5px; }
.regrow:first-of-type { border-top: 0; }
.rlabel { color: var(--text); }
.rnum { color: var(--muted); font-size: 12.5px; }
.rnum i { color: var(--dim); font-style: normal; padding: 0 3px; }
.rnum.up { color: var(--red); }

.sgrid { width: 100%; border-collapse: collapse; font-size: 12.5px; }
.sgrid th { text-align: right; padding: 12px 18px; font-weight: 500; color: var(--dim); font-size: 10.5px; letter-spacing: .8px; border-bottom: 1px solid var(--line); }
.sgrid th:first-child { text-align: left; }
.sgrid td { text-align: right; padding: 10px 18px; border-bottom: 1px solid #151b21; }
.sgrid td:first-child { text-align: left; }
.sgrid tr:last-child td { border-bottom: 0; }
.sc-label { font-family: system-ui, sans-serif; font-size: 13px; }

.costgrid { display: grid; grid-template-columns: 1fr 1fr 1.2fr; gap: 12px; }
.cbox { padding: 16px 18px; }
.cbig { font-size: 24px; font-weight: 600; margin: 10px 0 4px; }
.cbig span { font-size: 11px; color: var(--dim); margin-left: 6px; font-weight: 400; }
.csmall { font-size: 11px; color: var(--dim); }
.hist { display: flex; flex-direction: column; }
.bars { display: flex; align-items: flex-end; gap: 6px; height: 64px; margin-top: auto; padding-bottom: 14px; }
.bar { flex: 1; position: relative; display: flex; align-items: flex-end; justify-content: center; height: 100%; }
.bar i {
  width: 100%; max-width: 26px; background: linear-gradient(180deg, #2d6d69, #1b4644);
  border-radius: 3px 3px 0 0; min-height: 3px; display: block;
}
.bar b {
  position: absolute; bottom: -15px; left: 0; right: 0; text-align: center;
  font-size: 9px; color: var(--dim); font-weight: 400;
}

.kinds { margin-top: 12px; }
.krow { display: flex; align-items: center; gap: 14px; padding: 8px 0; border-top: 1px solid #141a20; font-size: 13px; }
.krow:first-child { border-top: 0; }
.klabel { width: 96px; color: var(--muted); }
.kbar { flex: 1; height: 5px; background: #161c22; border-radius: 999px; overflow: hidden; }
.kbar i { display: block; height: 100%; background: #2d6d69; border-radius: 999px; }
.knum { width: 210px; text-align: right; font-size: 11px; color: var(--dim); }

.limits { padding: 22px 26px; }
.limits ul { margin: 10px 0 0; padding-left: 18px; color: var(--muted); font-size: 13.5px; }
.limits li { margin-bottom: 6px; }
.limits b { color: var(--text); }

.cta { display: flex; gap: 10px; margin-top: 34px; flex-wrap: wrap; }

@media (max-width: 900px) {
  .costgrid { grid-template-columns: 1fr; }
  .regrid { grid-template-columns: 1fr; }
}
</style>
