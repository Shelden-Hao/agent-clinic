<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { api, type Pattern, type Report, type Step } from "../api";
import { go } from "../router";

const report = ref<Report | null>(null);
const error = ref<string | null>(null);
const activeId = ref<string | null>(null);
const saving = ref(false);
const savedMsg = ref("");
const copied = ref(false);

async function copyPatch(text: string) {
  try {
    await navigator.clipboard.writeText(text);
    copied.value = true;
    setTimeout(() => (copied.value = false), 1600);
  } catch {
    // 非 HTTPS 或权限被拒时退化成选中文本，不让按钮变成一个死掉的装饰
    const ta = document.createElement("textarea");
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
    copied.value = true;
    setTimeout(() => (copied.value = false), 1600);
  }
}

onMounted(async () => {
  try {
    report.value = await api.overview();
    activeId.value = report.value.patterns[0]?.id ?? null;
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
  }
});

const pct = (n: number) => `${Math.round(n * 100)}%`;

const active = computed<Pattern | null>(
  () => report.value?.patterns.find((p) => p.id === activeId.value) ?? null
);

const fork = computed(() => report.value?.fork ?? null);
const forkRows = computed(() => (fork.value?.rows ?? []).filter((r) => r.left || r.right));

function argsBrief(args: Record<string, unknown>): string {
  const keys = Object.keys(args ?? {});
  if (!keys.length) return "";
  return keys.map((k) => `${k}=${JSON.stringify(args[k])}`).join(", ").slice(0, 68);
}

function stepLabel(s: Step | null): string {
  if (!s) return "";
  if (s.kind === "final") return "给出最终答复";
  if (s.kind === "plan") return "规划 / 思考";
  return `${s.tool}(${argsBrief(s.args)})`;
}

async function saveActiveCase() {
  const m = active.value?.minimal;
  if (!m) return;
  saving.value = true;
  try {
    const c = m.case as Record<string, unknown> & { case_id: string };
    const res = await api.addRegression({
      case_id: c.case_id,
      kind: m.kind,
      label: m.label,
      task_id: c.task_id as string,
      task_prompt: c.task_prompt as string | undefined,
      expect: c.expect as Record<string, unknown>,
      steps: m.chain
        .filter((n) => n.keep)
        .map((n) => ({ i: n.step_index, kind: n.kind, tool: n.tool, args: n.args, ok: n.ok, role: n.role })),
      assertion: c.assertion as string,
      verified_by_replay: false,
      note: c.note as string | undefined,
    });
    savedMsg.value = `已存入回归集（共 ${res.total} 条）`;
  } catch (e) {
    savedMsg.value = e instanceof Error ? e.message : String(e);
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <p v-if="error" class="err">加载失败：{{ error }}</p>
  <p v-else-if="!report" class="loading">正在归纳失败模式…</p>

  <template v-else>
    <header class="head">
      <h1>失败模式</h1>
      <p class="sub">
        {{ report.summary.failed }} 条失败不是 {{ report.summary.failed }} 个问题。
        按「性质 + 出错工具 + 归一化证据」聚一次，只剩下 {{ report.patterns.length }} 类。
        每一类都配一条<b>最小复现</b>，可以直接拿去当测试用例。
      </p>
    </header>

    <div class="patterns">
      <button
        v-for="p in report.patterns"
        :key="p.id"
        class="pattern card"
        :class="{ active: p.id === activeId }"
        @click="activeId = p.id; savedMsg = ''"
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
          <span v-if="p.minimal" class="pmin">{{ p.minimal.original_steps }}→{{ p.minimal.minimal_steps }} 步</span>
        </div>
      </button>
    </div>

    <section v-if="active" class="card detail">
      <div class="dhead">
        <span class="pid mono">{{ active.id }}</span>
        <h2>{{ active.label }}</h2>
        <span class="tag bad">{{ active.count }} 条 · {{ pct(active.share) }}</span>
      </div>

      <p class="detail-ev">{{ active.signature }}</p>

      <dl class="kv mono">
        <dt>性质</dt><dd>{{ active.label }}（{{ active.kind }}）</dd>
        <dt>出现工具</dt><dd>{{ active.tool }}</dd>
        <dt>版本分布</dt>
        <dd>
          <span v-for="(c, v) in active.versions" :key="v" class="tag" style="margin-right: 6px">{{ v }} × {{ c }}</span>
        </dd>
        <dt>涉及任务</dt><dd>{{ active.tasks.join("、") }}</dd>
      </dl>

      <div class="advice">
        <div class="eyebrow">修复方向</div>
        <p>{{ active.advice }}</p>
      </div>

      <div v-if="active.patch" class="patchbox">
        <div class="phead">
          <div class="eyebrow">可直接粘贴的规则草稿</div>
          <button class="btn tiny" @click="copyPatch(active.patch!)">
            {{ copied ? "已复制" : "复制" }}
          </button>
        </div>
        <pre class="patchtext">{{ active.patch }}</pre>
        <p class="pnote">
          这是针对这一类病的条款草稿，粘进 system prompt 就能用。
          它不给「往哪个方向改」的建议，而是直接给出可执行的那句话 ——
          诊断到这一步才算闭环。
        </p>
      </div>

      <!-- 最小复现 -->
      <div v-if="active.minimal" class="minwrap">
        <div class="mhead">
          <div class="eyebrow">最小复现用例 · 取自 {{ active.minimal.run_id }}</div>
          <span class="tag warn">结构推断 · 未经重放验证</span>
          <button class="btn tiny primary" :disabled="saving" @click="saveActiveCase">
            {{ saving ? "保存中…" : "存进回归集" }}
          </button>
        </div>
        <p v-if="savedMsg" class="saved mono">{{ savedMsg }}</p>

        <div class="mstat">
          <div><b class="mono">{{ active.minimal.original_steps }}</b><span>原始步数</span></div>
          <div class="arrow">→</div>
          <div><b class="mono c-warn">{{ active.minimal.minimal_steps }}</b><span>最小步数</span></div>
          <div class="arrow">→</div>
          <div><b class="mono">{{ pct(active.minimal.reduction) }}</b><span>压缩率</span></div>
        </div>

        <p v-if="active.minimal.minimal_kind === 'terminal_only'" class="terminal">
          这类失败<b>没有出错的那一步</b>：最短复现就是一次不产生任何动作的收尾。
          病在指令缺失，不在执行出错。
        </p>
        <div v-else class="chips">
          <div
            v-for="n in active.minimal.chain.filter((c) => c.keep)"
            :key="n.step_index"
            class="chip"
            :class="n.role"
          >
            <span class="cnum mono">{{ n.step_index }}</span>
            <span class="ctool mono">{{ n.tool ?? n.kind }}</span>
          </div>
        </div>

        <div class="cf">
          <div class="eyebrow">反事实</div>
          <p class="cwhat"><b>{{ active.minimal.counterfactual.what_if }}</b></p>
          <p class="cpred">{{ active.minimal.counterfactual.predicted }}</p>
        </div>

        <button class="btn tiny" @click="go('/runs/' + active.minimal.run_id)">查看这条轨迹的逐步标注 →</button>
      </div>
    </section>

    <!-- 分叉对比 -->
    <section v-if="fork" class="block">
      <div class="eyebrow">分叉点 · 同一条任务，两条路径</div>
      <p class="taskline">
        <span class="mono tag">{{ fork.task_id }}</span>
        {{ fork.task_prompt }}
      </p>
      <div class="fork card">
        <div class="fork-head">
          <div class="ok-side">成功 <span class="mono dim">{{ fork.good_run }}</span></div>
          <div class="mid"></div>
          <div class="bad-side">失败 <span class="mono dim">{{ fork.bad_run }}</span></div>
        </div>
        <div
          v-for="(row, i) in forkRows"
          :key="i"
          class="frow"
          :class="{
            diverge: row.right?.step_index === fork.fork_at,
            missL: !row.left,
            missR: !row.right,
          }"
        >
          <div class="cell left mono">
            <span v-if="row.left" :class="{ err: row.left.ok === false }">{{ stepLabel(row.left) }}</span>
            <span v-else class="ghost">—</span>
          </div>
          <div class="seam">
            <span v-if="row.right?.step_index === fork.fork_at" class="forkmark">分叉</span>
          </div>
          <div class="cell right mono">
            <span v-if="row.right" :class="{ err: row.right.ok === false }">{{ stepLabel(row.right) }}</span>
            <span v-else class="ghost">—</span>
          </div>
        </div>
      </div>
      <p v-if="fork.diagnosis" class="verdict-sub">
        归因：<b class="c-bad">{{ fork.diagnosis.label }}</b>
        · 第 {{ fork.diagnosis.step_index }} 步 · 置信度 {{ fork.diagnosis.confidence }} ——
        {{ fork.diagnosis.evidence }}
      </p>
    </section>
  </template>
</template>

<style scoped>
.head { margin-bottom: 24px; }
h1 { font-size: 22px; margin: 0 0 8px; font-weight: 600; }
.sub { margin: 0; color: var(--muted); max-width: 68ch; font-size: 13.5px; }
.sub b { color: var(--text); }

.patterns { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 12px; }
.pattern { padding: 16px 18px; text-align: left; color: inherit; cursor: pointer; font: inherit; transition: border-color .15s, background .15s; }
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
.pmin { color: var(--amber); }

.detail { margin-top: 14px; padding: 22px 24px; }
.dhead { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.dhead h2 { font-size: 17px; margin: 0; font-weight: 600; }
.detail-ev { font-size: 15px; margin: 12px 0 18px; padding-left: 12px; border-left: 2px solid var(--red); }
.kv { display: grid; grid-template-columns: 90px 1fr; gap: 8px 14px; font-size: 12.5px; }
.kv dt { color: var(--dim); }
.kv dd { margin: 0; }
.advice { margin-top: 18px; padding: 14px 16px; background: #0d1216; border: 1px solid var(--line); border-radius: 8px; }
.advice p { margin: 8px 0 0; font-size: 13.5px; color: var(--muted); }

.patchbox { margin-top: 12px; padding: 14px 16px; background: #0f1418; border: 1px solid #1d2b30; border-radius: 8px; }
.phead { display: flex; align-items: center; gap: 10px; }
.phead .btn { margin-left: auto; }
.patchtext {
  margin: 10px 0 0; padding: 11px 13px; background: #080b0e; border: 1px solid var(--line);
  border-radius: 6px; font: 12.5px/1.7 var(--mono); color: #c9d6e2; white-space: pre-wrap;
}
.pnote { margin: 10px 0 0; color: var(--dim); font-size: 11.5px; line-height: 1.65; }

.minwrap { margin-top: 20px; padding-top: 18px; border-top: 1px solid var(--line); }
.mhead { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.mhead .btn { margin-left: auto; }
.saved { color: var(--green); font-size: 11.5px; margin: 8px 0 0; }
.mstat { display: flex; align-items: center; gap: 18px; margin: 16px 0; }
.mstat div { display: flex; flex-direction: column; }
.mstat b { font-size: 22px; font-weight: 600; }
.mstat span { font-size: 10.5px; color: var(--dim); }
.mstat .arrow { color: var(--line-2); font-size: 16px; }
.terminal { padding: 12px 14px; background: #0f1712; border: 1px solid #1e4131; border-radius: 8px; font-size: 13px; color: var(--muted); line-height: 1.7; }
.terminal b { color: var(--green); }
.chips { display: flex; gap: 7px; flex-wrap: wrap; }
.chip { display: flex; align-items: center; gap: 7px; padding: 6px 10px; border-radius: 7px; border: 1px solid var(--line-2); background: #0d1216; font-size: 11.5px; }
.chip.root { border-color: #4a2226; background: #1a1012; }
.chip.ctx { border-color: #1c3f42; }
.chip.cons { border-color: #45351d; }
.cnum { color: var(--dim); font-size: 10px; }
.ctool { color: var(--text); }
.cf { margin: 16px 0; }
.cwhat { margin: 8px 0 5px; font-size: 14px; }
.cpred { margin: 0; color: var(--muted); font-size: 12.5px; }

.taskline { margin: 0 0 12px; color: var(--muted); font-size: 13.5px; }
.fork { overflow: hidden; }
.fork-head { display: grid; grid-template-columns: 1fr 72px 1fr; padding: 12px 18px; border-bottom: 1px solid var(--line); font-size: 12px; }
.ok-side { color: var(--green); }
.bad-side { color: var(--red); text-align: right; }
.frow { display: grid; grid-template-columns: 1fr 72px 1fr; border-top: 1px solid #161c22; font-size: 12px; }
.frow:first-of-type { border-top: 0; }
.cell { padding: 9px 20px; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cell.right { text-align: right; }
.cell .err { color: var(--red); }
.cell .ghost { color: #2f3944; }
.seam { display: flex; align-items: center; justify-content: center; }
.forkmark { font: 9px/1 var(--mono); display: inline-block; color: #04211f; background: var(--amber); padding: 3px 4px; border-radius: 3px; letter-spacing: 1px; }
.frow.diverge { background: #1b1214; box-shadow: inset 3px 0 0 var(--red); }
.frow.missL .cell.left, .frow.missR .cell.right { background: repeating-linear-gradient(45deg, #0d1216, #0d1216 6px, #0a0e12 6px, #0a0e12 12px); }
.verdict-sub { color: var(--muted); margin: 12px 0 0; font-size: 13.5px; }
.verdict-sub b { color: var(--red); }
</style>
