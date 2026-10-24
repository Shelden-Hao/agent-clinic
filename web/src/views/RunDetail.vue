<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { api, type RunDetail } from "../api";
import { go } from "../router";

const props = defineProps<{ runId: string }>();

const data = ref<RunDetail | null>(null);
const error = ref<string | null>(null);
const loading = ref(false);
const saving = ref(false);
const saved = ref<string>("");
const showRaw = ref(false);

async function load() {
  loading.value = true;
  error.value = null;
  data.value = null;
  try {
    data.value = await api.run(props.runId);
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
  } finally {
    loading.value = false;
  }
}

onMounted(load);
watch(() => props.runId, load);

const d = computed(() => data.value);
const diag = computed(() => d.value?.diagnosis ?? null);
const minimal = computed(() => d.value?.minimal ?? null);
const ev = computed(() => d.value?.evaluation ?? null);

const keptSteps = computed(() => minimal.value?.chain.filter((n) => n.keep) ?? []);
const droppedSteps = computed(() => minimal.value?.chain.filter((n) => !n.keep) ?? []);
const toolCalls = computed(() => d.value?.steps.filter((s) => s.kind === "tool_call").length ?? 0);

const roleMeta: Record<string, { label: string; cls: string; desc: string }> = {
  root_cause: { label: "根因", cls: "root", desc: "诊断指认的出错决策" },
  required_context: { label: "前置", cls: "ctx", desc: "根因依赖的输入，缺了无法复现" },
  consequence: { label: "后果", cls: "cons", desc: "被根因牵连的后续步骤" },
  droppable: { label: "可丢", cls: "drop", desc: "与本因无因果关系，最小化时删除" },
};

function roleOf(s: { mark?: string | null }) {
  return s.mark ? roleMeta[s.mark] ?? null : null;
}

function argsBrief(args: Record<string, unknown>): string {
  const keys = Object.keys(args ?? {});
  if (!keys.length) return "";
  return keys.map((k) => `${k}=${JSON.stringify(args[k])}`).join(" · ").slice(0, 90);
}

function stepTitle(s: { kind: string; tool: string | null }): string {
  if (s.kind === "final") return "最终答复";
  if (s.kind === "plan") return "规划 / 思考";
  return s.tool ?? s.kind;
}

async function saveCase() {
  if (!minimal.value) return;
  saving.value = true;
  try {
    const c = minimal.value.case as Record<string, unknown> & { case_id: string };
    const res = await api.addRegression({
      case_id: c.case_id,
      kind: minimal.value.kind,
      label: minimal.value.label,
      task_prompt: d.value?.task_prompt,
      steps: minimal.value.chain
        .filter((n) => n.keep)
        .map((n) => ({ i: n.step_index, kind: n.kind, tool: n.tool, args: n.args, ok: n.ok, role: n.role })),
      assertion: c.assertion as string,
      verified_by_replay: false,
      note: c.note as string | undefined,
    });
    saved.value = `已存入回归集（共 ${res.total} 条）`;
  } catch (e) {
    saved.value = e instanceof Error ? e.message : String(e);
  } finally {
    saving.value = false;
  }
}

const rawJson = computed(() => (d.value ? JSON.stringify({ steps: d.value.steps }, null, 2) : ""));
</script>

<template>
  <p v-if="error" class="err">
    加载失败：{{ error }} · <a href="#/runs">返回列表</a>
  </p>
  <p v-else-if="loading || !d" class="loading">正在会诊这条轨迹…</p>

  <template v-else>
    <div class="crumb mono"><a href="#/runs">轨迹浏览器</a> <i>/</i> {{ d.run_id }}</div>

    <header class="head">
      <div class="titlerow">
        <h1 class="mono">{{ d.run_id }}</h1>
        <span v-if="d.success" class="tag ok">成功</span>
        <span v-else-if="!d.success_known" class="tag warn">成败未判定</span>
        <span v-else class="tag bad">{{ diag?.label }}</span>
      </div>
      <p class="prompt">{{ d.task_prompt || "（该轨迹未携带任务原文）" }}</p>
      <div class="meta mono">
        <span>任务 {{ d.task_id }}</span><span class="dot">·</span>
        <span>{{ d.version }}</span><span class="dot">·</span>
        <span>{{ d.model }}</span><span class="dot">·</span>
        <span>{{ toolCalls }} 次调用 / {{ d.steps.length }} 步</span>
        <span v-if="d.usage.total_tokens" class="dot">·</span>
        <span v-if="d.usage.total_tokens">{{ d.usage.total_tokens }} tokens</span>
        <span v-if="d.fail_reason" class="dot">·</span>
        <span v-if="d.fail_reason" class="dim">{{ d.fail_reason }}</span>
      </div>
    </header>

    <!-- 会诊结论 -->
    <section class="verdict card">
      <div class="eyebrow">会诊结论</div>
      <p class="vtext">
        <b v-if="diag && !d.success" class="c-bad">{{ diag.label }}</b>
        <b v-else-if="d.success" class="c-ok">未发现异常</b>
        <b v-else class="c-warn">未归类</b>
        <span class="vstep mono" v-if="diag && diag.step_index > 0">第 {{ diag.step_index }} 步</span>
        <span class="vconf mono" :class="(diag?.confidence ?? 0) >= 0.85 ? 'c-ok' : (diag?.confidence ?? 0) >= 0.6 ? 'c-warn' : 'c-bad'">
          置信 {{ (diag?.confidence ?? 0).toFixed(2) }}
        </span>
      </p>
      <p class="vwhy">{{ diag?.evidence }}</p>
      <div v-if="diag?.findings && diag.findings.length > 1" class="findings">
        <div class="eyebrow">同期观察到的其他问题</div>
        <div v-for="(f, i) in diag.findings.slice(1)" :key="i" class="findrow">
          <span class="ftag mono">@{{ f.step_index }}</span>
          <span class="flabel">{{ f.label }}</span>
          <span class="fev dim">{{ f.evidence }}</span>
        </div>
      </div>
    </section>

    <!-- 逐步时间线 -->
    <section class="block">
      <div class="eyebrow">执行时间线 · 每一步带角色标注</div>
      <div class="timeline">
        <div
          v-for="s in d.steps"
          :key="s.step_index"
          class="tlrow"
          :class="[s.mark ? roleMeta[s.mark].cls : '', { dropped: s.keep === false }]"
        >
          <div class="tlidx mono">{{ s.step_index }}</div>
          <div class="tlbody">
            <div class="tlhead">
              <span class="tltool mono" :class="{ bad: !s.ok }">{{ stepTitle(s) }}</span>
              <span v-if="s.kind === 'tool_call'" class="tlargs mono">{{ argsBrief(s.args) }}</span>
              <span v-if="s.mark" class="tlrole" :class="s.mark">{{ roleMeta[s.mark].label }}</span>
              <span v-if="s.keep === false" class="tldrop mono">最小化时删除</span>
            </div>
            <div v-if="s.why" class="tlwhy">{{ s.why }}</div>
            <div v-if="!s.ok && s.error" class="tlerr mono">{{ s.error }}</div>
            <details v-if="s.kind === 'final' || s.content" class="tlcontent">
              <summary class="mono">{{ s.kind === "final" ? "展开答复原文" : "展开思考" }}</summary>
              <pre>{{ s.content }}</pre>
            </details>
            <details v-if="s.observation != null" class="tlcontent">
              <summary class="mono">工具返回</summary>
              <pre>{{ typeof s.observation === "string" ? s.observation : JSON.stringify(s.observation, null, 2) }}</pre>
            </details>
          </div>
        </div>
      </div>
    </section>

    <!-- 最小复现 -->
    <section v-if="minimal" class="block">
      <div class="eyebrow">最小复现 · 把 {{ minimal.original_steps }} 步压到 {{ minimal.minimal_steps }} 步</div>
      <div class="mincard card">
        <div class="minhead">
          <div class="minstat">
            <span class="minnum mono">{{ minimal.original_steps }} → {{ minimal.minimal_steps }}</span>
            <span class="dim">去掉 {{ minimal.original_steps - minimal.minimal_steps }} 步，减少 {{ Math.round(minimal.reduction * 100) }}%</span>
          </div>
          <div class="minbadges">
            <span class="tag warn">结构推断</span>
            <span class="tag">未经重放验证</span>
          </div>
          <button class="btn tiny primary" :disabled="saving" @click="saveCase">
            {{ saving ? "保存中…" : "存进回归集" }}
          </button>
        </div>
        <p v-if="saved" class="saved mono">{{ saved }}</p>

        <div v-if="minimal.minimal_kind === 'terminal_only'" class="terminal">
          <b>这类失败没有「出错的那一步」。</b>
          最短复现就是一次不产生任何动作的收尾 —— 说明病在指令缺失，不在执行出错。
          修法自然也不同：补规则，不是改工具。
        </div>

        <div v-else class="minchain">
          <div class="eyebrow">保留下来的因果链</div>
          <div class="chips">
            <div v-for="n in keptSteps" :key="n.step_index" class="chip" :class="n.role">
              <span class="cnum mono">{{ n.step_index }}</span>
              <span class="ctool mono">{{ n.tool ?? n.kind }}</span>
              <span class="crole">{{ roleMeta[n.role]?.label }}</span>
            </div>
          </div>
        </div>

        <div v-if="droppedSteps.length" class="minchain dropped">
          <div class="eyebrow">被删掉的 {{ droppedSteps.length }} 步</div>
          <div class="chips">
            <div v-for="n in droppedSteps" :key="n.step_index" class="chip drop">
              <span class="cnum mono">{{ n.step_index }}</span>
              <span class="ctool mono">{{ n.tool ?? n.kind }}</span>
            </div>
          </div>
        </div>

        <div class="counter">
          <div class="eyebrow">反事实：改哪一处能让这条链断掉</div>
          <p class="cwhat"><b>{{ minimal.counterfactual.what_if }}</b></p>
          <p class="cpred">{{ minimal.counterfactual.predicted }}</p>
          <p class="ccaveat">{{ minimal.caveat }}</p>
        </div>
      </div>
    </section>

    <!-- 评分 -->
    <section v-if="ev" class="block">
      <div class="eyebrow">
        体检评分 · {{ ev.overall ?? "—" }}
        <span v-if="ev.scope === 'tool_only'" class="tag warn" style="margin-left: 8px">仅工具层</span>
      </div>
      <div class="card sbox">
        <div v-for="s in ev.scores" :key="s.key" class="srow" :class="s.score == null ? 'skip' : s.score >= 0.5 ? 'pass' : 'fail'">
          <span class="sdot"></span>
          <span class="slabel">{{ s.label }}</span>
          <span class="sreason">{{ s.reason }}</span>
          <span class="sval mono">{{ s.score == null ? "跳过" : s.score.toFixed(1) }}</span>
        </div>
        <p v-if="ev.note" class="note" style="padding: 0 18px 14px">{{ ev.note }}</p>
      </div>
    </section>

    <!-- 原始数据 -->
    <section class="block">
      <button class="btn tiny" @click="showRaw = !showRaw">{{ showRaw ? "收起" : "查看原始步骤 JSON" }}</button>
      <pre v-if="showRaw" class="raw mono">{{ rawJson }}</pre>
    </section>

    <div class="cta">
      <button class="btn" @click="go('/runs')">← 返回列表</button>
      <button class="btn primary" @click="go('/intake')">接入我的轨迹 →</button>
    </div>
  </template>
</template>

<style scoped>
.crumb { font-size: 11.5px; color: var(--dim); margin-bottom: 14px; }
.crumb a { color: var(--dim); text-decoration: none; }
.crumb a:hover { color: var(--cyan); }
.crumb i { font-style: normal; padding: 0 6px; }

.head { margin-bottom: 22px; }
.titlerow { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
h1 { font-size: 20px; margin: 0; font-weight: 600; letter-spacing: .2px; }
.prompt { margin: 12px 0 8px; color: var(--text); font-size: 14.5px; line-height: 1.65; max-width: 76ch; }
.meta { color: var(--dim); font-size: 11.5px; display: flex; gap: 7px; flex-wrap: wrap; }
.meta .dot { color: var(--line-2); }

.verdict { padding: 22px 24px; }
.vtext { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; margin: 12px 0 8px; font-size: 18px; }
.vstep { font-size: 12px; color: var(--muted); }
.vconf { font-size: 11px; padding: 3px 8px; border: 1px solid var(--line-2); border-radius: 999px; }
.vwhy { color: var(--muted); margin: 0; font-size: 14px; padding-left: 12px; border-left: 2px solid #4a2226; }
.findings { margin-top: 18px; padding-top: 16px; border-top: 1px solid var(--line); }
.findrow { display: flex; gap: 10px; align-items: baseline; font-size: 12.5px; padding: 5px 0; }
.ftag { color: var(--dim); font-size: 11px; min-width: 34px; }
.flabel { color: var(--amber); min-width: 76px; }
.fev { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.timeline { position: relative; }
.tlrow { display: flex; gap: 14px; padding-bottom: 2px; }
.tlidx {
  width: 26px; flex: 0 0 26px; text-align: right; font-size: 11px; color: var(--dim);
  padding-top: 12px; position: relative;
}
.tlidx::after {
  content: ""; position: absolute; left: 50%; top: 30px; bottom: -4px;
  width: 1px; background: var(--line); transform: translateX(-50%);
}
.tlrow:last-child .tlidx::after { display: none; }
.tlbody {
  flex: 1; border: 1px solid var(--line); background: var(--panel-2);
  border-radius: 9px; padding: 11px 14px; margin-bottom: 8px; min-width: 0;
}
.tlhead { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
.tltool { font-size: 13px; color: var(--text); font-weight: 500; }
.tltool.bad { color: var(--red); }
.tlargs { font-size: 11px; color: var(--dim); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 46ch; }
.tlrole { font-size: 10px; padding: 2px 7px; border-radius: 999px; margin-left: auto; letter-spacing: .5px; }
.tlrole.root { background: #2a1114; color: #ffb3b7; border: 1px solid #4a2226; }
.tlrole.ctx { background: #0d1f22; color: #9de6df; border: 1px solid #1c3f42; }
.tlrole.cons { background: #1c1609; color: #f0cd93; border: 1px solid #45351d; }
.tlrole.drop { background: #12161a; color: var(--dim); border: 1px solid var(--line); }
.tldrop { font-size: 10px; color: var(--dim); margin-left: auto; }
.tlwhy { margin-top: 7px; font-size: 12px; color: var(--muted); line-height: 1.6; }
.tlerr { margin-top: 7px; font-size: 11.5px; color: var(--red); background: #180e10; border: 1px solid #3a2226; border-radius: 6px; padding: 7px 10px; }
.tlcontent { margin-top: 7px; }
.tlcontent summary { font-size: 10.5px; color: var(--dim); cursor: pointer; }
.tlcontent summary:hover { color: var(--cyan); }
.tlcontent pre {
  margin: 8px 0 0; padding: 10px 12px; background: #080b0e; border: 1px solid var(--line);
  border-radius: 6px; font-size: 11.5px; color: var(--muted); white-space: pre-wrap;
  word-break: break-word; max-height: 260px; overflow: auto;
}
.tlrow.root .tlbody { border-color: #4a2226; background: #150e10; }
.tlrow.ctx .tlbody { border-color: #1c3a3d; }
.tlrow.cons .tlbody { border-color: #3d3320; }
.tlrow.dropped .tlbody { opacity: .52; border-style: dashed; }

.mincard { padding: 20px 22px; }
.minhead { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
.minstat { display: flex; flex-direction: column; gap: 2px; }
.minnum { font-size: 20px; font-weight: 600; }
.minstat .dim { font-size: 11.5px; }
.minbadges { display: flex; gap: 6px; }
.minhead .btn { margin-left: auto; }
.saved { color: var(--green); font-size: 11.5px; margin: 10px 0 0; }
.terminal {
  margin-top: 16px; padding: 14px 16px; background: #0f1712; border: 1px solid #1e4131;
  border-radius: 8px; font-size: 13.5px; color: var(--muted); line-height: 1.7;
}
.terminal b { color: var(--green); }
.minchain { margin-top: 18px; }
.minchain.dropped { margin-top: 14px; }
.chips { display: flex; gap: 7px; flex-wrap: wrap; margin-top: 9px; }
.chip {
  display: flex; align-items: center; gap: 7px; padding: 6px 10px; border-radius: 7px;
  border: 1px solid var(--line-2); background: #0d1216; font-size: 11.5px;
}
.chip.root { border-color: #4a2226; background: #1a1012; }
.chip.ctx { border-color: #1c3f42; }
.chip.cons { border-color: #45351d; }
.chip.drop { opacity: .55; border-style: dashed; }
.cnum { color: var(--dim); font-size: 10px; }
.ctool { color: var(--text); }
.crole { color: var(--dim); font-size: 10px; }
.counter { margin-top: 20px; padding-top: 16px; border-top: 1px solid var(--line); }
.cwhat { margin: 10px 0 6px; font-size: 14.5px; }
.cpred { margin: 0; color: var(--muted); font-size: 13px; }
.ccaveat { margin: 10px 0 0; color: var(--dim); font-size: 11.5px; line-height: 1.6; }

.sbox { padding: 6px 0; }
.srow { display: flex; align-items: baseline; gap: 12px; padding: 9px 18px; font-size: 13px; }
.sdot { width: 6px; height: 6px; border-radius: 50%; flex: 0 0 6px; margin-top: 7px; }
.srow.pass .sdot { background: var(--green); }
.srow.fail .sdot { background: var(--red); }
.srow.skip .sdot { background: #2a333d; }
.slabel { width: 82px; flex: 0 0 82px; color: var(--text); }
.sreason { flex: 1; color: var(--muted); font-size: 12.5px; }
.sval { font-size: 12px; color: var(--dim); }
.srow.fail .sval { color: var(--red); }
.srow.pass .sval { color: var(--muted); }

.raw { margin-top: 12px; padding: 14px; background: #080b0e; border: 1px solid var(--line); border-radius: 8px; font-size: 11.5px; color: var(--muted); overflow: auto; max-height: 480px; }
.cta { display: flex; gap: 10px; margin-top: 34px; }
@media (max-width: 720px) {
  .srow { flex-wrap: wrap; }
  .sreason { flex-basis: 100%; }
}
</style>
