<script setup lang="ts">
/** 实时流。
 *
 * 这一页存在的理由：把「事后导出」变成「边跑边看」。
 * 所以它刻意不做聚合、不做统计 —— 那些在别的页。这里只做一件事：
 * 让一条条决策在发生的当下出现在屏幕上，并且当场给出结论。
 */
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { api, type StreamEvent, type StreamInfo } from "../api";
import { go } from "../router";

interface LiveRun {
  run_id: string;
  task_id?: string;
  version?: string;
  model?: string;
  source?: string;
  steps: NonNullable<StreamEvent["step"]>[];
  result?: StreamEvent;
}

const info = ref<StreamInfo | null>(null);
const runs = ref<LiveRun[]>([]);
const connected = ref(false);
const status = ref("未连接");
const error = ref<string | null>(null);
const replaying = ref(false);
const copied = ref(false);

let es: EventSource | null = null;

const total = computed(() => runs.value.length);
const failed = computed(() => runs.value.filter((r) => r.result && r.result.success === false).length);
const done = computed(() => runs.value.filter((r) => r.result).length);

function push(ev: StreamEvent) {
  if (ev.type === "hello") return;

  const id = ev.run_id || "";
  if (!id) return;

  let run = runs.value.find((r) => r.run_id === id);
  if (!run) {
    run = { run_id: id, task_id: ev.task_id, version: ev.version, model: ev.model, source: ev.source, steps: [] };
    // 新的放最前面：流是「最近发生了什么」，不是「从头读到尾」
    runs.value.unshift(run);
    if (runs.value.length > 40) runs.value.pop();
  }

  if (ev.type === "step" && ev.step) {
    run.steps.push(ev.step);
  } else if (ev.type === "trace") {
    run.result = ev;
  }
}

function connect() {
  if (es) es.close();
  es = new EventSource("/api/stream/feed");
  es.onopen = () => {
    connected.value = true;
    status.value = "已连接";
  };
  es.onerror = () => {
    connected.value = false;
    // EventSource 会自己重连，这里只如实显示状态，不假装一切正常
    status.value = "连接中断，正在自动重连";
  };
  es.onmessage = (m) => {
    try {
      push(JSON.parse(m.data) as StreamEvent);
    } catch {
      /* 单条事件坏掉不该让整条流停摆 */
    }
  };
}

onMounted(async () => {
  try {
    info.value = await api.streamInfo();
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
  }
  connect();
});

onBeforeUnmount(() => {
  es?.close();
  es = null;
});

async function replay() {
  replaying.value = true;
  try {
    await api.replayStream(4, 0.35);
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
  } finally {
    replaying.value = false;
  }
}

function clear() {
  runs.value = [];
}

const snippet = computed(() => {
  const origin = typeof location !== "undefined" ? location.origin : "https://your-host";
  return `import { Clinic } from "${origin}/sdk/agent-clinic.js";

const clinic = new Clinic({ endpoint: "${origin}", key: "你的上报密钥" });

// 一次请求 = 一条轨迹
const trace = clinic.trace({
  taskId: "T01",
  promptVersion: "v2",
  taskPrompt: "用户想把订单 A1024 退掉",
  model: "deepseek-chat",
});

// 把工具包一层，之后每次调用自动上报 —— 不用记得埋点
const tools = clinic.wrap(trace, { get_order, refund_order, escalate });

await tools.get_order({ order_id: "A1024" });

// 成败由你判定，SDK 不猜
trace.end({ success: false, failReason: "没有发起退款" });`;
});

async function copySnippet() {
  try {
    await navigator.clipboard.writeText(snippet.value);
  } catch {
    const ta = document.createElement("textarea");
    ta.value = snippet.value;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }
  copied.value = true;
  setTimeout(() => (copied.value = false), 1600);
}

function argsBrief(args: Record<string, unknown>): string {
  const ks = Object.keys(args || {});
  if (!ks.length) return "";
  return ks.map((k) => `${k}=${String(args[k])}`).join(", ");
}

const SOURCE_LABEL: Record<string, string> = {
  sdk: "SDK 上报",
  replay: "样本回放",
};
</script>

<template>
  <div v-if="error" class="err">加载失败：{{ error }}</div>

  <div class="head">
    <p class="eyebrow">实时流</p>
    <h1>边跑边诊</h1>
    <p class="sub">
      把 SDK 接进你自己的 Agent，每一步决策都会实时出现在下面，并在轨迹结束时当场给出结论。
      没有接 SDK 也能看效果 —— 点「回放样本」放几条仓库里的真实轨迹。
    </p>
  </div>

  <div class="bar">
    <span class="live" :class="{ on: connected }"><i></i>{{ status }}</span>
    <span class="dim mono">{{ done }} 条已判定 · {{ total }} 条在流上 · 失败 {{ failed }}</span>
    <button class="btn tiny" :disabled="replaying" @click="replay">
      {{ replaying ? "回放中…" : "回放样本" }}
    </button>
    <button class="btn tiny" @click="clear">清空视图</button>
  </div>

  <!-- 流本体 -->
  <section class="block">
    <div v-if="!runs.length" class="vacant">
      <p>还没有事件进来。</p>
      <p class="dim">
        点上面的「回放样本」看效果，或者把下面这段代码接进你的 Agent ——
        接上之后，这里会随着你的 Agent 运行实时滚动。
      </p>
    </div>

    <div v-else class="stream">
      <div v-for="r in runs" :key="r.run_id" class="lrun" :class="{ bad: r.result && r.result.success === false }">
        <div class="lhead">
          <b class="mono">{{ r.run_id }}</b>
          <span class="tag mono" v-if="r.version">{{ r.version }}</span>
          <span class="src mono" :class="r.source">{{ SOURCE_LABEL[r.source || "sdk"] }}</span>
          <span class="dim mono">{{ r.steps.length }} 步</span>
          <span class="lres" v-if="!r.result">进行中…</span>
          <span class="lres" v-else-if="r.result.success">
            <span class="c-ok">通过</span>
          </span>
          <span class="lres" v-else>
            <span class="c-bad">{{ r.result.kind_label }}</span>
            <em class="mono">置信 {{ r.result.confidence }}</em>
          </span>
        </div>

        <div class="lsteps">
          <div v-for="(s, i) in r.steps" :key="i" class="lstep" :class="{ err: !s.ok }">
            <span class="li mono">{{ s.i }}</span>
            <template v-if="s.kind === 'tool_call'">
              <span class="lt mono" :class="{ bad: !s.ok }">{{ s.tool }}</span>
              <span class="la mono">{{ argsBrief(s.args) }}</span>
              <span v-if="s.latency_ms" class="ll mono">{{ s.latency_ms }}ms</span>
              <span v-if="!s.ok" class="le">{{ s.error }}</span>
            </template>
            <template v-else>
              <span class="lc">{{ s.content }}</span>
            </template>
          </div>
          <div v-if="!r.steps.length" class="lwait">等待第一步…</div>
        </div>

        <div v-if="r.result && r.result.evidence" class="lev">
          <b>判定依据</b>{{ r.result.evidence }}
        </div>
      </div>
    </div>
  </section>

  <!-- 接入方式 -->
  <section class="block">
    <p class="eyebrow">两行接入</p>
    <div class="codebox">
      <button class="copy" @click="copySnippet">{{ copied ? "已复制" : "复制" }}</button>
      <pre>{{ snippet }}</pre>
    </div>
    <p class="note">
      关键是 <b>wrap()</b>：把工具函数包一层，之后每次调用自动上报。
      手工埋点意味着总有人会忘记埋 —— 数据不全，后面所有归因都是在残缺样本上做结论。
    </p>
  </section>

  <section class="block" v-if="info">
    <p class="eyebrow">接口</p>
    <div class="eps">
      <div v-for="(v, k) in info.endpoints" :key="k" class="ep">
        <span class="mono ek">{{ k }}</span>
        <span class="mono ev">{{ v }}</span>
      </div>
    </div>
  </section>

  <section class="card block limits" v-if="info">
    <p class="eyebrow">这一页的边界</p>
    <ul>
      <li v-for="(l, i) in info.limits" :key="i">{{ l }}</li>
      <li>
        上报密钥由服务端环境变量 <b>CLINIC_KEY</b> 控制，限流
        <b>{{ info.rate_limit_per_min }} 条/分钟</b>。不做成完全开放：
        一个能写数据的公开接口，几小时内就会变成垃圾场。
      </li>
      <li>
        「回放样本」放的是仓库内<b>真实轨迹</b>，但节奏是模拟的，事件上标了
        <b>样本回放</b>。拿回放冒充实时数据，是这类工具最容易犯的错。
      </li>
    </ul>
  </section>

  <div class="cta">
    <button class="btn" @click="go('/intake')">批量导入已有轨迹 →</button>
    <button class="btn" @click="go('/sessions')">看多轮场景 →</button>
  </div>
</template>

<style scoped>
.head { margin-bottom: 18px; }
.head h1 { font-size: 25px; margin: 4px 0 8px; letter-spacing: -0.3px; }
.sub { color: var(--muted); font-size: 13.5px; max-width: 760px; line-height: 1.75; }

.bar { display: flex; align-items: center; gap: 14px; margin-bottom: 18px; flex-wrap: wrap; }
.live { display: inline-flex; align-items: center; gap: 7px; font: 11.5px var(--mono); color: var(--dim); }
.live i { width: 7px; height: 7px; border-radius: 999px; background: #3a444e; }
.live.on i { background: var(--green); box-shadow: 0 0 0 3px rgba(70, 201, 127, 0.16); }
.live.on { color: var(--green); }
.bar .dim { font-size: 11px; }

.vacant { padding: 40px 22px; text-align: center; border: 1px dashed var(--line); border-radius: 9px; }
.vacant p { margin: 0 0 8px; font-size: 13.5px; color: var(--text); }
.vacant .dim { font-size: 12px; line-height: 1.8; max-width: 520px; margin: 0 auto; }

.stream { display: flex; flex-direction: column; gap: 12px; }
.lrun {
  background: var(--panel, #11161b); border: 1px solid var(--line); border-radius: 9px;
  padding: 13px 16px; animation: slidein 0.28s ease;
}
@keyframes slidein {
  from { opacity: 0; transform: translateY(-6px); }
  to { opacity: 1; transform: none; }
}
.lrun.bad { border-color: #3a2427; }
.lhead { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 10px; }
.lhead b { font-size: 12px; }
.lhead .dim { font-size: 10px; }
.src { font: 9px var(--mono); padding: 2px 6px; border-radius: 3px; border: 1px solid var(--line); color: var(--dim); }
.src.replay { color: var(--c-warn, #e0a34a); border-color: #4a3a1c; }
.src.sdk { color: var(--cyan); border-color: #1d3b39; }
.lres { margin-left: auto; font-size: 11.5px; display: flex; align-items: center; gap: 8px; }
.lres em { font-style: normal; font-size: 10px; color: var(--dim); }

.lsteps { display: flex; flex-direction: column; gap: 2px; }
.lstep {
  display: flex; align-items: baseline; gap: 9px; padding: 4px 8px;
  border-radius: 5px; background: #0b1014; font-size: 11.5px;
}
.lstep.err { background: #170f11; }
.li { font-size: 9.5px; color: #2f3942; min-width: 14px; }
.lt { color: var(--cyan); }
.lt.bad { color: var(--red); }
.la { color: var(--dim); font-size: 10.5px; flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ll { font-size: 9.5px; color: #2f3942; }
.le { color: #c9757a; font-size: 10.5px; }
.lc { color: var(--muted); line-height: 1.7; }
.lwait { font-size: 11px; color: var(--dim); padding: 4px 8px; }

.lev {
  margin-top: 11px; padding: 10px 12px; border-radius: 6px;
  background: #170f11; border-left: 2px solid #7a3438;
  font-size: 11.5px; color: var(--muted); line-height: 1.8;
}
.lev b { color: #ffb3b7; font: 9.5px var(--mono); letter-spacing: 1px; margin-right: 8px; }

.codebox { position: relative; }
.codebox pre {
  margin: 0; padding: 16px 18px; background: #080b0e; border: 1px solid var(--line);
  border-radius: 8px; font: 11.5px/1.85 var(--mono); color: #bfd4de;
  overflow-x: auto; white-space: pre;
}
.copy {
  position: absolute; top: 10px; right: 10px; z-index: 1;
  background: #131c22; border: 1px solid var(--line); color: var(--muted);
  border-radius: 6px; padding: 5px 10px; font: 10.5px var(--mono); cursor: pointer;
}
.copy:hover { color: var(--text); border-color: #2a3a44; }

.eps { display: flex; flex-direction: column; gap: 6px; }
.ep { display: flex; gap: 14px; font-size: 11px; }
.ek { color: var(--cyan); min-width: 70px; }
.ev { color: var(--muted); }

.limits { padding: 18px 20px; }
.limits ul { margin: 9px 0 0; padding-left: 18px; color: var(--muted); font-size: 12px; line-height: 1.85; }
.limits li { margin-bottom: 5px; }
.limits b { color: var(--text); }

.cta { display: flex; gap: 10px; margin: 24px 0 40px; flex-wrap: wrap; }
.btn.tiny { padding: 6px 11px; font-size: 11px; }
</style>
