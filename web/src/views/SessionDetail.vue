<script setup lang="ts">
/** 单场会话详情：把「第几轮、谁说了什么、它做了什么」摊开。
 *
 * 版式跟单条轨迹详情刻意不同：那里是「一条竖线」，这里是「按轮分段」。
 * 多轮的问题往往在两段之间，所以每轮之间要留出空隙让人看见「过渡」。
 */
import { computed, onMounted, ref } from "vue";
import { api, type SessionDetail } from "../api";
import { go } from "../router";

const props = defineProps<{ sessionId: string }>();
const data = ref<SessionDetail | null>(null);
const error = ref<string | null>(null);
const copied = ref<string | null>(null);

onMounted(async () => {
  try {
    data.value = await api.session(props.sessionId);
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
  }
});

const findings = computed(() => data.value?.findings ?? []);

/** 每轮的跨轮问题，按轮号索引，方便在轮卡片上直接标出来 */
const byTurn = computed(() => {
  const m = new Map<number, typeof findings.value>();
  for (const f of findings.value) {
    const arr = m.get(f.turn_index) ?? [];
    arr.push(f);
    m.set(f.turn_index, arr);
  }
  return m;
});

const DEP_LABEL: Record<string, string> = {
  needs_history: "离开历史就做不对",
  harmed_by_history: "被历史害了",
  independent: "自带完整信息",
};

const DEP_CLASS: Record<string, string> = {
  needs_history: "dep-need",
  harmed_by_history: "dep-harm",
  independent: "dep-free",
};

function brief(args: Record<string, unknown>): string {
  const keys = Object.keys(args || {});
  if (!keys.length) return "—";
  return keys.map((k) => `${k}=${String(args[k])}`).join(", ");
}

async function copy(text: string, key: string) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    // 剪贴板 API 在非 https / 无用户手势时会被拒，退回到老办法
    const ta = document.createElement("textarea");
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }
  copied.value = key;
  setTimeout(() => (copied.value = null), 1600);
}
</script>

<template>
  <div v-if="error" class="err">加载失败：{{ error }}</div>
  <p v-else-if="!data" class="loading">正在读取会话…</p>

  <template v-else>
    <a class="back" href="#/sessions" @click.prevent="go('/sessions')">← 多轮会话</a>

    <div class="head">
      <p class="eyebrow">
        {{ data.scenario_id }} · {{ data.version }} · {{ data.model }}
      </p>
      <h1 class="mono">{{ data.session_id }}</h1>
      <p class="goal">{{ data.goal }}</p>
      <div class="hstats">
        <span class="tag" :class="data.success ? 'ok' : 'bad'">
          {{ data.success ? "会话通过" : `${data.turns.filter((t) => !t.success).length} 轮失败` }}
        </span>
        <span class="dim mono">{{ data.turns.length }} 轮 · 用户 {{ data.user_id }}</span>
      </div>
    </div>

    <!-- 跨轮结论：放在最前面，因为这是这一页存在的理由 -->
    <section v-if="findings.length" class="block">
      <p class="eyebrow">跨轮发现</p>
      <div class="fgrid">
        <div v-for="(f, i) in findings" :key="i" class="fcard">
          <div class="fhead">
            <b>{{ f.label }}</b>
            <span class="fturn mono">第 {{ f.turn_index }} 轮</span>
            <span class="fconf mono">置信 {{ f.confidence }}</span>
          </div>
          <p class="fev">{{ f.evidence }}</p>
          <p class="ffix"><span>修法</span>{{ f.fix }}</p>
        </div>
      </div>
    </section>

    <section v-else class="card block clean">
      <p class="eyebrow">没有发现跨轮问题</p>
      <p class="note">
        三类检测（纠正被忽略 / 跨轮丢失 / 重复副作用）都没有命中。
        这不等于会话完美 —— 它只说明<b>这三类具体问题</b>没有发生，
        单轮层面的问题请看每一轮的判定。
      </p>
    </section>

    <!-- 逐轮时间线 -->
    <section class="block">
      <p class="eyebrow">逐轮发生了什么</p>
      <div class="turns">
        <div v-for="t in data.turns" :key="t.turn_index" class="turn" :class="{ bad: !t.success }">
          <div class="tlabel">
            <span class="tno mono">第 {{ t.turn_index }} 轮</span>
            <span class="tres" :class="t.success ? 'c-ok' : 'c-bad'">
              {{ t.success ? "通过" : "失败" }}
            </span>
            <span v-if="t.dependency" class="dep" :class="DEP_CLASS[t.dependency]">
              {{ DEP_LABEL[t.dependency] }}
            </span>
            <span class="ttok mono">{{ t.tokens }} tok</span>
          </div>

          <div class="user">
            <span class="who">用户</span>
            <p>{{ t.user }}</p>
          </div>

          <div class="acts">
            <div v-for="s in t.steps" :key="s.step_index" class="act" :class="s.kind">
              <span class="ano mono">{{ s.step_index }}</span>
              <template v-if="s.kind === 'tool_call'">
                <span class="atool mono" :class="{ bad: !s.ok }">{{ s.tool }}</span>
                <span class="aargs mono">{{ brief(s.args) }}</span>
                <span v-if="!s.ok" class="aerr">{{ s.error }}</span>
              </template>
              <template v-else>
                <span class="ath">{{ s.content }}</span>
              </template>
            </div>
            <p v-if="!t.steps.length" class="empty">这一轮没有任何步骤</p>
          </div>

          <div v-if="t.fail_reason" class="freason">
            <span>判定</span>{{ t.fail_reason }}
          </div>

          <div v-for="(f, k) in byTurn.get(t.turn_index) || []" :key="k" class="xbad">
            <b>{{ f.label }}</b> {{ f.evidence }}
          </div>
        </div>
      </div>
    </section>

    <!-- 规则草稿 -->
    <section v-if="data.patches.length" class="block">
      <p class="eyebrow">这些问题的修法，可以直接写进 system prompt</p>
      <div class="pgrid">
        <div v-for="p in data.patches" :key="p.kind" class="pcard">
          <div class="phead">
            <b>{{ p.label }}</b>
            <button class="btn tiny" @click="copy(p.text, p.kind)">
              {{ copied === p.kind ? "已复制" : "复制规则" }}
            </button>
          </div>
          <p class="ptext">{{ p.text }}</p>
        </div>
      </div>
      <p class="note">
        跨轮病的修法通常是「给 Agent 加一条记忆或去重规则」，和单轮病「加一条决策规则」不同。
        去 <a href="#/prompts" @click.prevent="go('/prompts')">Prompt 版本</a> 可以把这些规则追加成一个新版本。
      </p>
    </section>

    <section class="card block limits">
      <p class="eyebrow">判定口径</p>
      <ul>
        <li>{{ data.caveat }}</li>
        <li>
          「被历史害了」指的是：这一轮如果<b>单独</b>拿出来测反而会通过 ——
          失败来自前一轮留下的状态，而不是这一轮的决策。
        </li>
        <li>
          「离开历史就做不对」指的是：这一轮用户<b>没有</b>给出目标标识符，
          单轮测试给它一条孤立输入，必然做不对。
        </li>
      </ul>
    </section>
  </template>
</template>

<style scoped>
.back { display: inline-block; font: 11.5px var(--mono); color: var(--dim); text-decoration: none; margin-bottom: 14px; }
.back:hover { color: var(--cyan); }

.head { margin-bottom: 22px; }
.head h1 { font-size: 21px; margin: 4px 0 8px; letter-spacing: -0.3px; }
.goal { color: var(--muted); font-size: 13.5px; margin: 0 0 11px; }
.hstats { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; font-size: 12px; }
.tag.ok { color: var(--green); border-color: #23503a; }
.tag.bad { color: var(--red); border-color: #552427; }

.fgrid { display: flex; flex-direction: column; gap: 12px; }
.fcard { background: #170f11; border: 1px solid #4a2226; border-radius: 9px; padding: 15px 17px; }
.fhead { display: flex; align-items: center; gap: 10px; margin-bottom: 9px; flex-wrap: wrap; }
.fhead b { font-size: 13.5px; color: #ffb3b7; }
.fturn { font-size: 10.5px; color: var(--dim); border: 1px solid var(--line-2, #2a333d); padding: 2px 6px; border-radius: 3px; }
.fconf { margin-left: auto; font-size: 10.5px; color: var(--dim); }
.fev { margin: 0; font-size: 12.5px; color: var(--muted); line-height: 1.85; }
.ffix { margin: 11px 0 0; font-size: 12px; color: var(--text); line-height: 1.8; padding-top: 10px; border-top: 1px dashed #33201f; }
.ffix span { font: 9.5px var(--mono); color: var(--c-warn, #e0a34a); letter-spacing: 1px; margin-right: 8px; }

.clean { padding: 18px 20px; }

.turns { display: flex; flex-direction: column; gap: 16px; }
.turn { background: var(--panel, #11161b); border: 1px solid var(--line); border-radius: 9px; padding: 15px 17px; }
.turn.bad { border-color: #3a2427; }
.tlabel { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; flex-wrap: wrap; }
.tno { font-size: 11.5px; color: var(--text); }
.tres { font-size: 11.5px; }
.dep { font: 10px var(--mono); padding: 2px 7px; border-radius: 999px; border: 1px solid; }
.dep-need { color: #7fb2d9; border-color: #22405a; background: #0d1620; }
.dep-harm { color: #e0a34a; border-color: #4a3a1c; background: #1a150a; }
.dep-free { color: var(--dim); border-color: var(--line); }
.ttok { margin-left: auto; font-size: 10px; color: var(--dim); }

.user { display: flex; gap: 10px; align-items: flex-start; margin-bottom: 12px; }
.who { font: 9.5px var(--mono); color: var(--dim); letter-spacing: 1px; padding-top: 3px; flex: 0 0 auto; }
.user p { margin: 0; font-size: 13px; color: var(--text); line-height: 1.75; }

.acts { display: flex; flex-direction: column; gap: 3px; padding-left: 2px; }
.act { display: flex; align-items: baseline; gap: 9px; padding: 5px 9px; border-radius: 5px; background: #0b1014; font-size: 11.5px; }
.act.thought { background: transparent; }
.act.final { background: #0e1a16; }
.ano { font-size: 9.5px; color: #2f3942; min-width: 14px; }
.atool { color: var(--cyan); }
.atool.bad { color: var(--red); }
.aargs { color: var(--dim); font-size: 10.5px; flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.aerr { color: #c9757a; font-size: 10.5px; }
.ath { color: var(--muted); line-height: 1.7; }

.freason { margin-top: 11px; padding-top: 10px; border-top: 1px dashed #2a2024; font-size: 11.5px; color: #c9757a; }
.freason span { font: 9.5px var(--mono); color: var(--dim); letter-spacing: 1px; margin-right: 8px; }

.xbad { margin-top: 10px; padding: 10px 12px; border-radius: 6px; background: #170f11; border-left: 2px solid #7a3438; font-size: 11.5px; color: var(--muted); line-height: 1.8; }
.xbad b { color: #ffb3b7; margin-right: 6px; }

.pgrid { display: flex; flex-direction: column; gap: 10px; }
.pcard { background: var(--panel, #11161b); border: 1px solid var(--line); border-radius: 8px; padding: 13px 15px; }
.phead { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 8px; }
.phead b { font-size: 12.5px; }
.ptext { margin: 0; font-size: 12.5px; color: var(--muted); line-height: 1.85; }

.limits { padding: 18px 20px; }
.limits ul { margin: 9px 0 0; padding-left: 18px; color: var(--muted); font-size: 12px; line-height: 1.85; }
.limits li { margin-bottom: 5px; }
.limits b { color: var(--text); }

.btn.tiny { padding: 5px 10px; font-size: 10.5px; }
</style>
