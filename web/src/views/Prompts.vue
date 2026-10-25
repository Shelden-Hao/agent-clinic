<script setup lang="ts">
/** Prompt 版本管理。
 *
 * 这一页要接上的是整个工具链最后一段断掉的路：
 * 诊断已经告诉你哪里坏了、规则草稿已经给出该写什么，
 * 但「把它写进 prompt、并且记住为什么这么写」这件事以前落在人的记忆里。
 *
 * 三个动作对应三个问题：
 *   详情 —— 这一版到底写了什么，每条规则的出处是哪
 *   对比 —— 两版之间改了几条规矩
 *   生成 —— 拿诊断结论直接长出下一版的草稿
 */
import { computed, onMounted, ref } from "vue";
import {
  api,
  type ComposeSources,
  type PromptDiff,
  type PromptOverview,
  type PromptVersion,
} from "../api";
import { go } from "../router";

const list = ref<PromptOverview | null>(null);
const error = ref<string | null>(null);
const tab = ref<"detail" | "diff" | "compose">("detail");
const sel = ref<string | null>(null);

const aId = ref<string>("");
const bId = ref<string>("");
const diffData = ref<PromptDiff | null>(null);
const diffBusy = ref(false);

const src = ref<ComposeSources | null>(null);
const baseId = ref<string>("");
const picked = ref<string[]>([]);
const pickedCross = ref<string[]>([]);
const composing = ref(false);
const composed = ref<PromptVersion | null>(null);

async function load() {
  const d = await api.prompts();
  list.value = d;
  if (!sel.value) {
    // 默认选中最后一个「已投入使用」的版本 —— 那才是当前真实在跑的那一版
    const act = d.versions.filter((v) => v.status === "active");
    sel.value = (act.at(-1) ?? d.versions[0])?.id ?? null;
  }
  if (!aId.value) {
    const ids = d.versions.map((v) => v.id);
    aId.value = ids[0] ?? "";
    bId.value = ids[1] ?? ids[0] ?? "";
  }
  if (!baseId.value) baseId.value = sel.value ?? d.versions[0]?.id ?? "";
}

onMounted(async () => {
  try {
    await load();
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
  }
});

const current = computed<PromptVersion | null>(
  () => list.value?.versions.find((v) => v.id === sel.value) ?? null,
);

const versions = computed(() => list.value?.versions ?? []);

async function runDiff() {
  if (!aId.value || !bId.value) return;
  diffBusy.value = true;
  try {
    diffData.value = await api.promptDiff(aId.value, bId.value);
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
  } finally {
    diffBusy.value = false;
  }
}

async function openCompose() {
  tab.value = "compose";
  if (src.value) return;
  try {
    const s = await api.composeSources();
    src.value = s;
    // 默认把「出现次数 ≥ 3」的病勾上。这是硬阈值，不是我的偏好。
    picked.value = s.single.filter((x) => x.recommended).map((x) => x.kind);
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
  }
}

/** 模板里 picked / pickedCross 已被 Vue 自动解包成数组本身，
 *  所以这里收的是数组，不是 ref —— 传 ref 会拿到一个不可变的对象。 */
function toggle(arr: string[], k: string) {
  const i = arr.indexOf(k);
  if (i >= 0) arr.splice(i, 1);
  else arr.push(k);
}

const canCompose = computed(() => picked.value.length + pickedCross.value.length > 0 && !!baseId.value);

async function doCompose() {
  if (!canCompose.value) return;
  composing.value = true;
  try {
    const r = await api.compose({
      base: baseId.value,
      kinds: [...picked.value],
      cross_kinds: [...pickedCross.value],
    });
    composed.value = r.version;
    await load();
    sel.value = r.version.id;
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
  } finally {
    composing.value = false;
  }
}

function pct(v: number | null | undefined): string {
  return v == null ? "未测" : `${Math.round(v * 100)}%`;
}

const STATUS_CLASS: Record<string, string> = {
  active: "st-active",
  draft: "st-draft",
  archived: "st-arch",
};

const OP_LABEL: Record<string, string> = {
  equal: "同",
  insert: "新增",
  delete: "删除",
  replace: "改写",
};
</script>

<template>
  <div v-if="error" class="err">加载失败：{{ error }}</div>
  <p v-else-if="!list" class="loading">正在读取版本表…</p>

  <template v-else>
    <div class="head">
      <p class="eyebrow">Prompt 版本</p>
      <h1>改了什么，为什么改</h1>
      <p class="sub">{{ list.headline }}</p>
    </div>

    <!-- 版本墙 -->
    <section class="block">
      <div class="wall">
        <button
          v-for="v in versions"
          :key="v.id"
          class="vcard"
          :class="[STATUS_CLASS[v.status], { on: sel === v.id }]"
          @click="sel = v.id; tab = 'detail'"
        >
          <div class="vtop">
            <b class="mono">{{ v.id }}</b>
            <span class="st">{{ v.status_label }}</span>
          </div>
          <div class="vname">{{ v.label }}</div>
          <div class="vstat">
            <span class="mono" :class="v.stats.fail_rate == null ? 'dim' : v.stats.fail_rate > 0.4 ? 'c-bad' : 'c-ok'">
              失败率 {{ pct(v.stats.fail_rate) }}
            </span>
            <span class="dim mono">{{ v.stats.runs }} 条轨迹</span>
          </div>
          <div class="vline mono" v-if="v.lineage.length > 1">
            {{ v.lineage.join(" → ") }}
          </div>
        </button>
      </div>
      <p class="note">{{ list.caveat }}</p>
    </section>

    <!-- 工具条 -->
    <div class="tabs">
      <button class="tab" :class="{ on: tab === 'detail' }" @click="tab = 'detail'">版本详情</button>
      <button class="tab" :class="{ on: tab === 'diff' }" @click="tab = 'diff'; if (!diffData) runDiff()">
        两版对比
      </button>
      <button class="tab" :class="{ on: tab === 'compose' }" @click="openCompose">从诊断生成下一版</button>
    </div>

    <!-- 详情 -->
    <section v-if="tab === 'detail' && current" class="block">
      <div class="dtwo">
        <div class="dcard">
          <div class="dhead">
            <b class="mono">{{ current.id }}</b>
            <span class="tag">{{ current.status_label }}</span>
            <span class="dim mono" v-if="current.parent">父版本 {{ current.parent }}</span>
          </div>
          <p class="dnote" v-if="current.note">{{ current.note }}</p>

          <div class="dsec">
            <p class="eyebrow">正文</p>
            <pre class="sys">{{ current.system }}</pre>
          </div>
        </div>

        <div class="dside">
          <div class="sbox">
            <p class="eyebrow">体检</p>
            <div class="kv">
              <span>关联轨迹</span><b class="mono">{{ current.stats.runs }}</b>
            </div>
            <div class="kv">
              <span>通过</span><b class="mono">{{ current.stats.ok }}</b>
            </div>
            <div class="kv">
              <span>失败率</span>
              <b class="mono" :class="current.stats.fail_rate == null ? 'dim' : ''">
                {{ pct(current.stats.fail_rate) }}
              </b>
            </div>
            <div v-if="current.stats.kinds.length" class="kindsleft">
              <span class="eyebrow">残留病种</span>
              <div v-for="k in current.stats.kinds" :key="k.kind" class="krow">
                <span>{{ k.label }}</span><b class="mono">{{ k.count }}</b>
              </div>
            </div>
            <p v-else-if="!current.stats.measured" class="boxnote">
              这一版还没有跑过任何轨迹。失败率显示「未测」而不是 0% ——
              把没测过说成 0% 会让人以为它是最好的版本。
            </p>
          </div>

          <div class="sbox" v-if="current.sources.length">
            <p class="eyebrow">每条规则的出处</p>
            <div v-for="(s, i) in current.sources" :key="i" class="srcline">
              <span class="sk mono">{{ s.kind_label }}</span>
              <b>{{ s.label }}</b>
              <p v-if="s.evidence">{{ s.evidence }}</p>
            </div>
          </div>

          <div class="sbox" v-if="current.changelog.length">
            <p class="eyebrow">变更记录</p>
            <ul class="chg">
              <li v-for="(c, i) in current.changelog" :key="i">{{ c }}</li>
            </ul>
          </div>
        </div>
      </div>
    </section>

    <!-- 对比 -->
    <section v-if="tab === 'diff'" class="block">
      <div class="dpick">
        <select v-model="aId" @change="runDiff">
          <option v-for="v in versions" :key="v.id" :value="v.id">{{ v.id }} · {{ v.label }}</option>
        </select>
        <span class="arrow">→</span>
        <select v-model="bId" @change="runDiff">
          <option v-for="v in versions" :key="v.id" :value="v.id">{{ v.id }} · {{ v.label }}</option>
        </select>
        <button class="btn tiny" :disabled="diffBusy" @click="runDiff">
          {{ diffBusy ? "对齐中…" : "重新对比" }}
        </button>
      </div>

      <div v-if="diffData" class="dwrap">
        <div class="dstat">
          <span><b class="mono c-ok">{{ diffData.stats.sentences_b }}</b> 句（原 {{ diffData.stats.sentences_a }} 句）</span>
          <span>新增 <b class="mono">{{ diffData.stats.added }}</b></span>
          <span>删除 <b class="mono">{{ diffData.stats.removed }}</b></span>
          <span>改写 <b class="mono">{{ diffData.stats.changed }}</b></span>
        </div>

        <div v-if="diffData.same" class="card clean">
          <p class="note">这两个版本的正文完全一致。</p>
        </div>

        <div v-else class="drows">
          <div v-for="(r, i) in diffData.rows" :key="i" class="drow" :class="r.op">
            <span class="op mono">{{ OP_LABEL[r.op] }}</span>
            <div class="side">
              <span class="ln mono">{{ r.a_line ?? "" }}</span>
              <span class="tx" :class="{ none: !r.a }">{{ r.a ?? "" }}</span>
            </div>
            <div class="side">
              <span class="ln mono">{{ r.b_line ?? "" }}</span>
              <span class="tx" :class="{ none: !r.b }">{{ r.b ?? "" }}</span>
            </div>
          </div>
        </div>
        <p class="note" v-if="diffData">{{ diffData.note }}</p>
      </div>
    </section>

    <!-- 生成 -->
    <section v-if="tab === 'compose'" class="block">
      <p class="note">
        勾选要修的失败模式，在指定基线的正文末尾追加对应规则。
        <b>每条追加都会带一个来源标记</b>——这是刻意的冗余，方便日后回看「这条规矩凭什么加」。
        基线版一个字都不改：改动了旧规则，就再也说不清「修好的是新规则还是改坏的旧规则」。
      </p>

      <div class="cpick">
        <label class="cl">
          <span>基于</span>
          <select v-model="baseId">
            <option v-for="v in versions" :key="v.id" :value="v.id">{{ v.id }} · {{ v.label }}</option>
          </select>
        </label>
        <button class="btn" :disabled="!canCompose || composing" @click="doCompose">
          {{ composing ? "生成中…" : `生成新版本（${picked.length + pickedCross.length} 条依据）` }}
        </button>
      </div>

      <template v-if="src">
        <p class="eyebrow">单轮失败模式</p>
        <div class="cg">
          <button
            v-for="x in src.single"
            :key="x.kind"
            class="citem"
            :class="{ on: picked.includes(x.kind) }"
            @click="toggle(picked, x.kind)"
          >
            <div class="ci-top">
              <b>{{ x.label }}</b>
              <span class="mono">{{ x.count }} 次</span>
              <span v-if="x.recommended" class="rec">推荐</span>
            </div>
            <p>{{ x.patch }}</p>
            <p class="ci-why" v-if="x.advice">{{ x.advice }}</p>
          </button>
        </div>

        <template v-if="src.cross.length">
          <p class="eyebrow">多轮失效</p>
          <div class="cg">
            <button
              v-for="x in src.cross"
              :key="x.kind"
              class="citem"
              :class="{ on: pickedCross.includes(x.kind) }"
              @click="toggle(pickedCross, x.kind)"
            >
              <div class="ci-top">
                <b>{{ x.label }}</b>
                <span class="mono">{{ x.count }} 次</span>
              </div>
              <p>{{ x.patch }}</p>
              <p class="ci-why">涉及会话 {{ x.sessions.join("、") }}</p>
            </button>
          </div>
        </template>

        <p class="note">{{ src.note }}</p>
      </template>

      <div v-if="composed" class="cresult">
        <p class="eyebrow">已生成 {{ composed.id }}（草稿）</p>
        <p class="cnote">
          血缘 {{ composed.lineage.join(" → ") }}。它现在<b>没有轨迹验证</b>，所以失败率是空的。
          下一步是拿它跑一批轨迹，再回实验对比看它到底修好了没有。
        </p>
        <div class="cbtns">
          <button class="btn" @click="go('/sessions')">看多轮场景 →</button>
          <button class="btn" @click="go('/experiments')">看实验对比 →</button>
          <button class="btn" @click="tab = 'detail'">看这一版的正文 →</button>
        </div>
      </div>
    </section>

    <section class="card block limits">
      <p class="eyebrow">这一页不做什么</p>
      <ul>
        <li>
          <b>不自动优化 prompt。</b>文本质量不是规则引擎能保证的事，声称能自动优化只会让输出不可追责。
          它做的是把「该写什么」和「为什么写」摆出来，编辑动作留给人。
        </li>
        <li>
          <b>不改基线版。</b>草稿版本只在末尾追加，不动已有规则 —— 否则「修好的是新规则还是改坏的旧规则」永远说不清。
        </li>
        <li>
          <b>草稿的失败率是空的，不是 0%。</b>没测过和测了全挂是两件事。
        </li>
      </ul>
    </section>
  </template>
</template>

<style scoped>
.head { margin-bottom: 20px; }
.head h1 { font-size: 25px; margin: 4px 0 8px; letter-spacing: -0.3px; }
.sub { color: var(--muted); font-size: 13.5px; max-width: 720px; line-height: 1.75; }

.wall { display: grid; grid-template-columns: repeat(auto-fill, minmax(210px, 1fr)); gap: 12px; }
.vcard {
  text-align: left; background: var(--panel, #11161b); border: 1px solid var(--line);
  border-radius: 9px; padding: 13px 15px; cursor: pointer; color: var(--text);
  font: inherit; transition: border-color .12s, background .12s;
}
.vcard:hover { border-color: #2a3a44; }
.vcard.on { border-color: var(--cyan); background: #0f1a1d; }
.vtop { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.vtop b { font-size: 12.5px; }
.st { font: 9.5px var(--mono); padding: 2px 6px; border-radius: 3px; border: 1px solid; }
.st-active { color: var(--green); border-color: #23503a; }
.st-draft { color: var(--c-warn, #e0a34a); border-color: #4a3a1c; }
.st-arch { color: var(--dim); border-color: var(--line); }
.vname { font-size: 13px; color: var(--text); margin: 8px 0 10px; }
.vstat { display: flex; align-items: center; justify-content: space-between; gap: 8px; font-size: 10.5px; }
.vline { margin-top: 8px; font-size: 9.5px; color: var(--dim); }

.tabs { display: flex; gap: 4px; margin: 26px 0 0; border-bottom: 1px solid var(--line); }
.tab {
  background: transparent; border: 0; border-bottom: 2px solid transparent; color: var(--muted);
  padding: 9px 14px; font: 13px system-ui; cursor: pointer;
}
.tab:hover { color: var(--text); }
.tab.on { color: var(--text); border-bottom-color: var(--cyan); }

.dtwo { display: grid; grid-template-columns: minmax(0, 1.45fr) minmax(0, 1fr); gap: 16px; }
.dcard { background: var(--panel, #11161b); border: 1px solid var(--line); border-radius: 9px; padding: 16px 18px; }
.dhead { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; flex-wrap: wrap; }
.dhead b { font-size: 13px; }
.dhead .dim { font-size: 10.5px; }
.dnote { margin: 0 0 14px; font-size: 12.5px; color: var(--muted); line-height: 1.8; }
.dsec .eyebrow { margin-bottom: 8px; }
.sys {
  margin: 0; padding: 14px; background: #080b0e; border: 1px solid var(--line); border-radius: 7px;
  font: 12px/1.9 var(--mono); color: var(--text); white-space: pre-wrap; word-break: break-word;
  max-height: 520px; overflow: auto;
}

.dside { display: flex; flex-direction: column; gap: 14px; }
.sbox { background: var(--panel, #11161b); border: 1px solid var(--line); border-radius: 9px; padding: 14px 16px; }
.sbox .eyebrow { margin-bottom: 10px; }
.kv { display: flex; align-items: center; justify-content: space-between; font-size: 12px; color: var(--muted); padding: 5px 0; }
.kv b { color: var(--text); font-size: 12.5px; }
.kindsleft { margin-top: 10px; padding-top: 10px; border-top: 1px dashed var(--line); }
.krow { display: flex; align-items: center; justify-content: space-between; font-size: 11.5px; color: var(--muted); padding: 3px 0; }
.krow b { color: var(--text); }
.boxnote { margin: 10px 0 0; font-size: 11.5px; color: var(--dim); line-height: 1.8; }

.srcline { padding: 8px 0; border-bottom: 1px dashed #1a2229; }
.srcline:last-child { border-bottom: 0; }
.sk { font-size: 9.5px; color: var(--cyan); border: 1px solid #1d3b39; padding: 1px 5px; border-radius: 3px; }
.srcline b { display: block; font-size: 12px; margin: 6px 0 3px; color: var(--text); }
.srcline p { margin: 0; font-size: 11.5px; color: var(--muted); line-height: 1.75; }

.chg { margin: 0; padding-left: 16px; font-size: 11.5px; color: var(--muted); line-height: 1.85; }

.dpick { display: flex; align-items: center; gap: 10px; margin-bottom: 16px; flex-wrap: wrap; }
.dpick select {
  background: var(--panel, #11161b); color: var(--text); border: 1px solid var(--line);
  border-radius: 7px; padding: 8px 10px; font: 12px var(--mono); max-width: 100%;
}
.arrow { color: var(--dim); }
.dstat { display: flex; gap: 22px; font-size: 11.5px; color: var(--muted); margin-bottom: 14px; flex-wrap: wrap; }
.dstat b { color: var(--text); }

.drows { display: flex; flex-direction: column; gap: 2px; }
.drow {
  display: grid; grid-template-columns: 40px 1fr 1fr; gap: 8px; align-items: start;
  padding: 6px 8px; border-radius: 5px; font-size: 11.5px;
}
.drow.equal { background: transparent; }
.drow.insert { background: #0e1a16; }
.drow.delete { background: #1a0f11; }
.drow.replace { background: #1a150a; }
.op { font-size: 9.5px; color: var(--dim); padding-top: 2px; }
.drow.insert .op { color: var(--green); }
.drow.delete .op { color: var(--red); }
.drow.replace .op { color: var(--c-warn, #e0a34a); }
.side { display: flex; gap: 8px; min-width: 0; }
.ln { font-size: 9.5px; color: #2f3942; min-width: 16px; text-align: right; padding-top: 2px; }
.tx { color: var(--muted); line-height: 1.75; min-width: 0; word-break: break-word; }
.drow.insert .side:last-child .tx { color: #9fe3bb; }
.drow.delete .side:first-child .tx { color: #e8a1a5; }
.drow.replace .tx { color: #e3cd9a; }
.tx.none { color: transparent; }

.cpick { display: flex; align-items: center; gap: 12px; margin: 16px 0 22px; flex-wrap: wrap; }
.cl { display: flex; align-items: center; gap: 8px; font-size: 12.5px; color: var(--muted); }
.cl select {
  background: var(--panel, #11161b); color: var(--text); border: 1px solid var(--line);
  border-radius: 7px; padding: 8px 10px; font: 12px var(--mono);
}

.cg { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 12px; margin-bottom: 8px; }
.citem {
  text-align: left; background: var(--panel, #11161b); border: 1px solid var(--line);
  border-radius: 9px; padding: 14px 15px; cursor: pointer; color: var(--text); font: inherit;
  transition: border-color .12s, background .12s;
}
.citem:hover { border-color: #2a3a44; }
.citem.on { border-color: var(--cyan); background: #0f1a1d; }
.ci-top { display: flex; align-items: center; gap: 9px; margin-bottom: 8px; flex-wrap: wrap; }
.ci-top b { font-size: 13px; }
.ci-top .mono { font-size: 10.5px; color: var(--dim); }
.rec { font: 9.5px var(--mono); color: var(--cyan); border: 1px solid #1d3b39; padding: 1px 5px; border-radius: 3px; }
.citem p { margin: 0; font-size: 12px; color: var(--muted); line-height: 1.8; }
.citem .ci-why { margin-top: 8px; font-size: 11px; color: var(--dim); padding-top: 8px; border-top: 1px dashed #1a2229; }

.cresult { margin-top: 22px; background: #0f1a1d; border: 1px solid #1d3b39; border-radius: 9px; padding: 16px 18px; }
.cnote { margin: 8px 0 14px; font-size: 12.5px; color: var(--muted); line-height: 1.85; }
.cnote b { color: var(--text); }
.cbtns { display: flex; gap: 9px; flex-wrap: wrap; }

.clean { padding: 16px 18px; }
.limits { padding: 18px 20px; margin-top: 22px; }
.limits ul { margin: 9px 0 0; padding-left: 18px; color: var(--muted); font-size: 12px; line-height: 1.9; }
.limits li { margin-bottom: 6px; }
.limits b { color: var(--text); }
.btn.tiny { padding: 6px 11px; font-size: 11px; }

@media (max-width: 900px) {
  .dtwo { grid-template-columns: 1fr; }
  .drow { grid-template-columns: 34px 1fr; }
  .drow .side:last-child { grid-column: 2; }
  .cg { grid-template-columns: 1fr; }
}
</style>
