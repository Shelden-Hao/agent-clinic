<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { api, type IntakeResult, type RegressionCase, type RegressionVerify } from "../api";
import { go } from "../router";

const text = ref("");
const format = ref("auto");
const formats = ref<{ key: string; label: string }[]>([
  { key: "auto", label: "自动识别" },
  { key: "native", label: "Agent 会诊室原生格式" },
  { key: "openai", label: "OpenAI Chat Completions messages" },
  { key: "otlp", label: "OpenTelemetry / OTLP（GenAI 语义约定）" },
  { key: "langsmith", label: "LangSmith 运行导出" },
]);

const result = ref<IntakeResult | null>(null);
const busy = ref(false);
const error = ref<string | null>(null);
const cases = ref<RegressionCase[]>([]);
const dragOver = ref(false);
const openRun = ref<string | null>(null);
const verifyResult = ref<RegressionVerify | null>(null);
const verifying = ref(false);

const STATUS_LABEL: Record<string, string> = {
  fixed: "已修复",
  partial: "不稳定",
  present: "仍存在",
  changed: "变成别的病",
  uncovered: "未覆盖",
};
const statusLabel = (k: string) => STATUS_LABEL[k] ?? k;
const countClass = (k: string) =>
  k === "fixed" ? "ok" : k === "present" ? "bad" : k === "changed" || k === "partial" ? "warn" : "";

async function runVerify(useUploaded: boolean) {
  if (verifying.value) return;
  verifying.value = true;
  error.value = null;
  try {
    verifyResult.value = await api.verifyRegression(useUploaded ? text.value : "");
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
  } finally {
    verifying.value = false;
  }
}

async function loadCases() {
  try {
    cases.value = (await api.regression()).cases;
  } catch {
    /* 回归集读不到不影响主流程 */
  }
}

onMounted(async () => {
  loadCases();
  try {
    const f = await api.formats();
    if (f.formats?.length) formats.value = [{ key: "auto", label: "自动识别" }, ...f.formats.map((x) => ({ key: x.key, label: x.label }))];
  } catch {
    /* 用内置兜底列表 */
  }
});

async function run() {
  if (!text.value.trim() || busy.value) return;
  busy.value = true;
  error.value = null;
  result.value = null;
  try {
    result.value = await api.intake(text.value, format.value);
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
  } finally {
    busy.value = false;
  }
}

async function loadSample(fmt: string) {
  busy.value = true;
  error.value = null;
  try {
    const payload = await api.sample(fmt);
    text.value = JSON.stringify(payload, null, 2);
    format.value = "auto";
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
  } finally {
    busy.value = false;
  }
}

function onFile(file: File) {
  if (file.size > 4 * 1024 * 1024) {
    error.value = "文件超过 4MB，请截取一部分再试";
    return;
  }
  const reader = new FileReader();
  reader.onload = () => {
    text.value = String(reader.result || "");
  };
  reader.readAsText(file);
}

function onDrop(e: DragEvent) {
  dragOver.value = false;
  const f = e.dataTransfer?.files?.[0];
  if (f) onFile(f);
}

function onPick(e: Event) {
  const f = (e.target as HTMLInputElement).files?.[0];
  if (f) onFile(f);
}

async function save(runId: string) {
  const r = result.value?.runs.find((x) => x.run_id === runId);
  if (!r?.minimal) return;
  const c = r.minimal.case as Record<string, unknown> & { case_id: string };
  try {
    await api.addRegression({
      case_id: c.case_id,
      kind: r.minimal.kind,
      label: r.minimal.label,
      task_id: c.task_id as string,
      task_prompt: c.task_prompt as string | undefined,
      expect: c.expect as Record<string, unknown>,
      steps: r.minimal.chain
        .filter((n) => n.keep)
        .map((n) => ({ i: n.step_index, kind: n.kind, tool: n.tool, args: n.args, ok: n.ok, role: n.role })),
      assertion: c.assertion as string,
      verified_by_replay: false,
      note: c.note as string | undefined,
    });
    await loadCases();
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
  }
}

async function removeCase(id: string) {
  try {
    await api.delRegression(id);
    await loadCases();
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
  }
}

const stats = computed(() => result.value?.stats ?? null);

function scoreClass(v: number | null): string {
  if (v == null) return "dim";
  return v < 0.7 ? "c-bad" : v < 0.9 ? "c-warn" : "c-ok";
}
</script>

<template>
  <header class="head">
    <h1>现场接诊</h1>
    <p class="sub">
      把你自己 Agent 的轨迹贴进来。支持 OpenTelemetry（GenAI 语义约定）、OpenAI messages 和
      LangSmith 导出 —— 会自动识别格式。
      <b>解析完立即返回诊断，不落库、不外传</b>；只有你主动点「存进回归集」，才会写入。
    </p>
  </header>

  <section class="card intake">
    <div class="dropzone" :class="{ over: dragOver }" @dragover.prevent="dragOver = true" @dragleave="dragOver = false" @drop.prevent="onDrop">
      <textarea
        v-model="text"
        class="ta mono"
        spellcheck="false"
        placeholder='把 JSON / JSONL 粘贴在这里，或直接把文件拖进这个区域…&#10;&#10;不知道格式长什么样？点下面的「载入示例」，那是从真实轨迹转出来的。'
      ></textarea>
      <div class="dzfoot">
        <label class="btn tiny filebtn">
          选择文件
          <input type="file" accept=".json,.jsonl,.txt" @change="onPick" />
        </label>
        <span class="dzsize mono">{{ text.length.toLocaleString("zh-CN") }} 字符</span>
        <span class="dzhint">支持 .json / .jsonl，最大 4MB</span>
      </div>
    </div>

    <div class="controls">
      <select v-model="format" class="sel">
        <option v-for="f in formats" :key="f.key" :value="f.key">{{ f.label }}</option>
      </select>
      <button class="btn primary" :disabled="busy || !text.trim()" @click="run">
        {{ busy ? "诊断中…" : "开始会诊" }}
      </button>
      <button class="btn tiny" @click="text = ''; result = null">清空</button>
      <span class="spacer"></span>
      <span class="dim mono" style="font-size: 11px">载入示例：</span>
      <button class="btn tiny" @click="loadSample('openai')">OpenAI</button>
      <button class="btn tiny" @click="loadSample('otlp')">OTLP</button>
      <button class="btn tiny" @click="loadSample('langsmith')">LangSmith</button>
    </div>

    <p v-if="error" class="err" style="margin: 12px 20px 0">{{ error }}</p>
  </section>

  <!-- 诊断结果 -->
  <template v-if="result">
    <section v-if="!result.ok" class="block">
      <div class="card warnbox">
        <div class="eyebrow">没能读懂这份数据</div>
        <ul>
          <li v-for="(w, i) in result.warnings" :key="i">{{ w }}</li>
        </ul>
      </div>
    </section>

    <template v-else>
      <section class="block">
        <div class="eyebrow">识别结果</div>
        <div class="card reshead">
          <div class="rgrid">
            <div><b class="mono">{{ result.format_label || result.format }}</b><span>识别格式</span></div>
            <div><b class="mono">{{ stats?.total ?? 0 }}</b><span>轨迹</span></div>
            <div><b class="mono">{{ stats?.with_tool_calls ?? 0 }}</b><span>含工具调用</span></div>
            <div>
              <b class="mono" :class="stats?.task_level_ready ? 'c-ok' : 'c-warn'">
                {{ stats?.task_level_ready ? "可用" : "不可用" }}
              </b>
              <span>任务级归因</span>
            </div>
          </div>
          <p v-if="result.caveat" class="caveat">{{ result.caveat }}</p>
          <ul v-if="result.warnings.length" class="warns">
            <li v-for="(w, i) in result.warnings.slice(0, 4)" :key="i">{{ w }}</li>
            <li v-if="result.warnings.length > 4" class="dim">…还有 {{ result.warnings.length - 4 }} 条</li>
          </ul>
        </div>
      </section>

      <section class="block">
        <div class="eyebrow">逐条诊断</div>
        <div class="runs">
          <div v-for="r in result.runs" :key="r.run_id" class="card run">
            <div class="rhead" @click="openRun = openRun === r.run_id ? null : r.run_id">
              <span class="rname mono">{{ r.run_id }}</span>
              <span v-if="r.success_known && r.success" class="tag ok">成功</span>
              <span v-else-if="!r.success_known && r.diagnosis.kind === 'unknown'" class="tag warn">未判定</span>
              <span v-else class="tag bad">{{ r.diagnosis.label }}</span>
              <span class="rmeta mono">{{ r.steps }} 步 · {{ r.tokens }} tok</span>
              <span class="rscore mono" :class="scoreClass(r.evaluation.overall)">{{ r.evaluation.overall ?? "—" }}</span>
              <span class="rchev">{{ openRun === r.run_id ? "▾" : "▸" }}</span>
            </div>

            <div v-if="openRun === r.run_id" class="rbody">
              <p class="rev">{{ r.diagnosis.evidence }}</p>
              <div class="rtools mono">
                <span v-for="(t, i) in r.tools" :key="i" class="tag">{{ t }}</span>
              </div>

              <div v-if="r.minimal" class="mini">
                <div class="minihead">
                  <span class="mono">{{ r.minimal.original_steps }} → {{ r.minimal.minimal_steps }} 步</span>
                  <span class="tag warn">结构推断</span>
                  <button class="btn tiny" @click="save(r.run_id)">存进回归集</button>
                </div>
                <p v-if="r.minimal.minimal_kind === 'terminal_only'" class="dim" style="font-size: 12.5px">
                  这类失败没有出错的那一步：最短复现就是一次不产生任何动作的收尾。
                </p>
                <p class="whatif"><b>{{ r.minimal.counterfactual.what_if }}</b></p>
              </div>

              <div class="rscores">
                <div v-for="s in r.evaluation.scores" :key="s.key" class="rsc" :class="s.score == null ? 'skip' : s.score >= 0.5 ? 'pass' : 'fail'">
                  <span class="rscl">{{ s.label }}</span>
                  <span class="rscv mono">{{ s.score == null ? "—" : s.score.toFixed(1) }}</span>
                </div>
              </div>
            </div>
          </div>
        </div>
      </section>
    </template>
  </template>

  <!-- 回归集 -->
  <section class="block">
    <div class="reghead">
      <div class="eyebrow">我的回归集 · {{ cases.length }} 条最小复现用例</div>
      <div class="regactions" v-if="cases.length">
        <button class="btn tiny" :disabled="verifying" @click="runVerify(false)">用内置轨迹验收</button>
        <button class="btn tiny primary" :disabled="verifying || !text.trim()" @click="runVerify(true)">
          {{ verifying ? "验收中…" : "用这批上传的轨迹验收" }}
        </button>
      </div>
    </div>

    <div v-if="verifyResult" class="card vresult">
      <p class="vsum">{{ verifyResult.summary }}</p>
      <div class="vcounts">
        <span v-for="(n, k) in verifyResult.counts" :key="k" class="tag" :class="countClass(String(k))">
          {{ statusLabel(String(k)) }} {{ n }}
        </span>
      </div>
      <div class="vlist">
        <div v-for="v in verifyResult.verdicts" :key="v.case_id" class="vrow" :class="v.status">
          <span class="vstatus">{{ v.status_label }}</span>
          <span class="vlabel">{{ v.label }}</span>
          <span class="vdetail">{{ v.detail }}</span>
        </div>
      </div>
      <p class="note">
        「变成了别的病」是最该看的一格：它意味着你的修复把这批订单从一个坑推到了另一个坑。
        「这次没有覆盖」不算通过 —— 没考和考过了是两件事。<br />
        注意：「用内置轨迹验收」会把仓库里的 v1 和 v2 混在一起考，所以出现「部分修复」是预期的 ——
        它说明同一任务在不同版本下表现不一致。真正有意义的用法是改完 prompt 重跑一批，
        把<b>新版本</b>的轨迹传进来再验收。
      </p>
    </div>

    <div v-if="!cases.length" class="card emptybox">
      还没有用例。上面任何一条失败轨迹的「最小复现」都可以存进来，
      下次改完 prompt，这批用例就是你的验收清单。
    </div>
    <div v-else class="cases">
      <div v-for="c in cases" :key="c.case_id" class="card case">
        <div class="chead">
          <span class="tag bad">{{ c.label }}</span>
          <span class="cid mono">{{ c.case_id }}</span>
          <span class="tag warn">未重放验证</span>
          <button class="btn tiny" @click="removeCase(c.case_id)">移除</button>
        </div>
        <p class="cassert">{{ c.assertion }}</p>
        <div class="csteps mono">
          <span v-for="s in c.steps" :key="s.i" class="step" :class="s.role">{{ s.i }}·{{ s.tool ?? s.kind }}</span>
        </div>
      </div>
    </div>
  </section>

  <div class="cta">
    <button class="btn" @click="go('/runs')">← 看内置的 48 条轨迹</button>
    <a class="btn" href="/api/docs" target="_blank" rel="noreferrer">接口文档</a>
  </div>
</template>

<style scoped>
.head { margin-bottom: 22px; }
h1 { font-size: 22px; margin: 0 0 8px; font-weight: 600; }
.sub { margin: 0; color: var(--muted); max-width: 70ch; font-size: 13.5px; }
.sub b { color: var(--text); }

.intake { padding: 14px; }
.dropzone { border: 1px dashed var(--line-2); border-radius: 9px; padding: 4px; transition: border-color .15s, background .15s; }
.dropzone.over { border-color: var(--cyan); background: #0c1a1c; }
.ta {
  width: 100%; min-height: 168px; resize: vertical; background: transparent; color: var(--text);
  border: 0; outline: none; padding: 12px; font-size: 12px; line-height: 1.6;
}
.dzfoot { display: flex; align-items: center; gap: 12px; padding: 4px 10px 8px; }
.filebtn { position: relative; overflow: hidden; }
.filebtn input { position: absolute; inset: 0; opacity: 0; cursor: pointer; }
.dzsize { font-size: 10.5px; color: var(--dim); }
.dzhint { font-size: 10.5px; color: var(--dim); margin-left: auto; }
.controls { display: flex; gap: 8px; align-items: center; margin-top: 12px; flex-wrap: wrap; }
.sel { font: 12px/1 var(--mono); padding: 8px 10px; border-radius: 7px; background: #0c1116; color: var(--text); border: 1px solid var(--line-2); }
.spacer { flex: 1; }

.warnbox { padding: 18px 20px; border-color: #45351d; background: #14110a; }
.warnbox ul { margin: 10px 0 0; padding-left: 18px; color: var(--muted); font-size: 13px; }

.reshead { padding: 18px 20px; }
.rgrid { display: flex; gap: 40px; flex-wrap: wrap; }
.rgrid div { display: flex; flex-direction: column; }
.rgrid b { font-size: 15px; font-weight: 600; }
.rgrid span { font-size: 10.5px; color: var(--dim); }
.caveat { margin: 14px 0 0; padding: 10px 12px; background: #0d1216; border-left: 2px solid var(--amber); font-size: 12.5px; color: var(--muted); }
.warns { margin: 12px 0 0; padding-left: 18px; color: var(--dim); font-size: 11.5px; }

.runs { display: flex; flex-direction: column; gap: 8px; }
.run { padding: 0; overflow: hidden; }
.rhead { display: flex; align-items: center; gap: 10px; padding: 13px 16px; cursor: pointer; flex-wrap: wrap; }
.rhead:hover { background: #131a20; }
.rname { font-size: 12.5px; }
.rmeta { font-size: 11px; color: var(--dim); margin-left: auto; }
.rscore { font-size: 13px; width: 38px; text-align: right; }
.rchev { color: var(--dim); font-size: 10px; width: 12px; text-align: right; }
.rbody { padding: 4px 16px 16px; border-top: 1px solid var(--line); }
.rev { margin: 12px 0 10px; font-size: 13px; color: var(--muted); padding-left: 11px; border-left: 2px solid #4a2226; }
.rtools { display: flex; gap: 6px; flex-wrap: wrap; }
.mini { margin: 14px 0; padding: 12px 14px; background: #0d1216; border: 1px solid var(--line); border-radius: 8px; }
.minihead { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; font-size: 12.5px; }
.minihead .btn { margin-left: auto; }
.whatif { margin: 10px 0 0; font-size: 13px; color: var(--muted); }
.whatif b { color: var(--text); }
.rscores { display: flex; gap: 8px; flex-wrap: wrap; }
.rsc { display: flex; align-items: center; gap: 6px; padding: 4px 9px; border-radius: 6px; border: 1px solid var(--line); font-size: 11px; }
.rsc.pass { border-color: #1e4131; background: #0d1a13; }
.rsc.fail { border-color: #4a2226; background: #180e10; }
.rsc.skip { opacity: .55; }
.rscl { color: var(--muted); }
.rscv { color: var(--text); }

.emptybox { padding: 20px; color: var(--dim); font-size: 13px; }
.reghead { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; margin-bottom: 14px; }
.regactions { display: flex; gap: 8px; margin-left: auto; }
.vresult { padding: 18px 20px; margin-bottom: 12px; }
.vsum { margin: 0 0 12px; font-size: 14px; line-height: 1.65; }
.vcounts { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; }
.vlist { display: flex; flex-direction: column; }
.vrow { display: flex; gap: 12px; align-items: baseline; padding: 8px 0; border-top: 1px solid #141a20; font-size: 12.5px; }
.vstatus { width: 76px; flex: 0 0 76px; font-size: 11px; }
.vrow.fixed .vstatus { color: var(--green); }
.vrow.present .vstatus { color: var(--red); }
.vrow.partial .vstatus { color: var(--amber); }
.vrow.changed .vstatus { color: var(--amber); }
.vrow.uncovered .vstatus { color: var(--dim); }
.vlabel { width: 84px; flex: 0 0 84px; color: var(--text); }
.vdetail { flex: 1; color: var(--muted); }
.cases { display: flex; flex-direction: column; gap: 8px; }
.case { padding: 14px 16px; }
.chead { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.cid { font-size: 11px; color: var(--dim); margin-right: auto; }
.cassert { margin: 10px 0; font-size: 13px; color: var(--muted); }
.csteps { display: flex; gap: 6px; flex-wrap: wrap; }
.step { font-size: 10.5px; padding: 3px 7px; border-radius: 5px; border: 1px solid var(--line-2); color: var(--dim); }
.step.root_cause { border-color: #4a2226; color: #ffb3b7; }
.step.required_context { border-color: #1c3f42; color: #9de6df; }
.step.consequence { border-color: #45351d; color: #f0cd93; }

.cta { display: flex; gap: 10px; margin-top: 34px; }
.cta a.btn { text-decoration: none; display: inline-flex; align-items: center; }
</style>
