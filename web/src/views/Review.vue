<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { api, type ReviewItem, type ReviewQueue } from "../api";
import { go } from "../router";

const queue = ref<ReviewQueue | null>(null);
const kinds = ref<{ kind: string; label: string }[]>([]);
const error = ref<string | null>(null);
const flash = ref<string | null>(null);
const busy = ref<string | null>(null);

/** 正在编辑的那一条（展开改判/驳回的理由输入） */
const editing = ref<{ run_id: string; action: "override" | "dismiss"; kind: string; note: string } | null>(
  null,
);

async function load() {
  queue.value = await api.reviewQueue();
}

onMounted(async () => {
  try {
    const [q, cat] = await Promise.all([api.reviewQueue(), api.catalog()]);
    queue.value = q;
    kinds.value = (cat.kinds ?? []).map((k) => ({ kind: k.kind, label: k.label }));
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
  }
});

const stats = computed(() => queue.value ?? null);
const items = computed(() => queue.value?.items ?? []);
const pending = computed(() => items.value.filter((i) => i.status === "pending"));
const resolved = computed(() => items.value.filter((i) => i.status === "resolved"));

/** 改判候选项：把当前判定排除掉，否则「改判成同一个病」毫无意义 */
function options(item: ReviewItem) {
  return kinds.value.filter((k) => k.kind !== item.machine.kind);
}

function startOverride(item: ReviewItem) {
  editing.value = { run_id: item.run_id, action: "override", kind: options(item)[0]?.kind ?? "", note: "" };
  flash.value = null;
}

function startDismiss(item: ReviewItem) {
  editing.value = { run_id: item.run_id, action: "dismiss", kind: "", note: "" };
  flash.value = null;
}

async function confirm(item: ReviewItem) {
  busy.value = item.run_id;
  try {
    await api.submitReview(item.run_id, { action: "confirm", note: "" });
    await load();
    flash.value = `${item.run_id} 已确认：机器判定成立。`;
  } catch (e) {
    flash.value = `提交失败：${e instanceof Error ? e.message : String(e)}`;
  } finally {
    busy.value = null;
  }
}

async function submitForm() {
  const f = editing.value;
  if (!f) return;
  busy.value = f.run_id;
  try {
    await api.submitReview(f.run_id, { action: f.action, kind: f.kind, note: f.note });
    editing.value = null;
    await load();
    flash.value =
      f.action === "override"
        ? `${f.run_id} 已改判。复核后的病种分布已经变了。`
        : `${f.run_id} 已判为不算失败，失败总数减一。`;
  } catch (e) {
    flash.value = `提交失败：${e instanceof Error ? e.message : String(e)}`;
  } finally {
    busy.value = null;
  }
}

async function undo(item: ReviewItem) {
  busy.value = item.run_id;
  try {
    await api.cancelReview(item.run_id);
    await load();
    flash.value = `${item.run_id} 的复核已撤销，回到待签字状态。`;
  } catch (e) {
    flash.value = `撤销失败：${e instanceof Error ? e.message : String(e)}`;
  } finally {
    busy.value = null;
  }
}

const actionTone: Record<string, string> = { confirm: "ok", override: "warn", dismiss: "bad" };

const diff = computed(() => {
  const q = stats.value;
  if (!q) return [];
  const map = new Map<string, { label: string; before: number; after: number }>();
  for (const r of q.before) map.set(r.kind, { label: r.label, before: r.count, after: 0 });
  for (const r of q.after) {
    const row = map.get(r.kind) ?? { label: r.label, before: 0, after: 0 };
    row.after = r.count;
    map.set(r.kind, row);
  }
  return [...map.values()].filter((r) => r.before !== r.after).sort((a, b) => b.after - a.after);
});
</script>

<template>
  <p v-if="error" class="err">加载失败：{{ error }}</p>
  <p v-else-if="!queue" class="loading">正在读取复核队列…</p>

  <template v-else>
    <header class="head">
      <h1>机器判不准的，等人签字</h1>
      <p class="sub">
        规则式归因给出的是<b>证据强度</b>，不是<b>正确概率</b>。置信度 0.70 和 0.85 的诊断
        在报告里长得一模一样，但前者可能只是证据刚够写下来。
        所以证据不足的那几条不直接进结论 —— 它们先进这里，等人看一眼。
      </p>
      <div class="meta mono">
        <span>置信度阈值 ≤ {{ queue.confidence_floor }}</span>
        <span class="dot">·</span>
        <span>或同任务同版本结果不一致</span>
        <span class="dot">·</span>
        <span>或规则没认出来</span>
      </div>
    </header>

    <section class="stats card">
      <div>
        <b class="mono">{{ queue.total_failed }}</b>
        <span>失败诊断</span>
      </div>
      <div>
        <b class="mono c-warn">{{ queue.queued }}</b>
        <span>进队列</span>
      </div>
      <div>
        <b class="mono c-warn">{{ queue.pending }}</b>
        <span>待签字</span>
      </div>
      <div>
        <b class="mono c-ok">{{ queue.resolved }}</b>
        <span>已签字</span>
      </div>
      <div class="breakdown mono">
        <span>确认 {{ queue.actions.confirm ?? 0 }}</span>
        <span>改判 {{ queue.actions.override ?? 0 }}</span>
        <span>驳回 {{ queue.actions.dismiss ?? 0 }}</span>
      </div>
    </section>
    <p class="note">
      {{ queue.total_failed }} 条失败里只有 {{ queue.queued }} 条需要人看
      （{{ Math.round((queue.queued / Math.max(1, queue.total_failed)) * 100) }}%）。
      剩下的判得足够确定，不占用你的时间 —— 一个筛不动的队列等于没有队列。
    </p>

    <p v-if="flash" class="flash">{{ flash }}</p>

    <!-- 待签字 -->
    <section class="block">
      <div class="eyebrow">待签字 · {{ pending.length }} 条</div>
      <p v-if="!pending.length" class="empty card pad-empty">
        队列空了。所有值得怀疑的诊断都已经有人看过 ——
        注意这不等于「所有诊断都对了」，只等于「没有人再怀疑它们」。
      </p>

      <article v-for="item in pending" :key="item.run_id" class="card rv">
        <div class="rvhead">
          <div class="rvleft">
            <span class="rid mono">{{ item.run_id }}</span>
            <span class="rtask mono">{{ item.task_id }} · {{ item.version }}</span>
          </div>
          <div class="rvright">
            <span class="conf mono" :class="item.machine.confidence <= queue.confidence_floor ? 'low' : ''">
              置信度 {{ item.machine.confidence.toFixed(2) }}
            </span>
            <span class="tag warn">待签字</span>
          </div>
        </div>

        <div class="judge">
          <div class="jkind">
            <span class="jlabel">{{ item.machine.label }}</span>
            <span class="jstep mono">@第 {{ item.machine.step_index }} 步</span>
          </div>
          <p class="jevidence">{{ item.machine.evidence }}</p>
        </div>

        <div class="sigblock">
          <div class="eyebrow">为什么把它挑出来</div>
          <ul class="siglist">
            <li v-for="(s, i) in item.signal_labels" :key="i">{{ s }}</li>
          </ul>
          <p v-for="(h, i) in item.hints" :key="'h' + i" class="hint">{{ h }}</p>
        </div>

        <div v-if="item.peers.length" class="peers">
          <div class="eyebrow">同版本的其他轨迹 · 对照</div>
          <div class="peerlist">
            <a
              v-for="p in item.peers"
              :key="p.run_id"
              class="peer mono"
              :href="'#/runs/' + encodeURIComponent(p.run_id)"
              @click.prevent="go('/runs/' + p.run_id)"
            >
              <span>{{ p.run_id }}</span>
              <span :class="p.success ? 'c-ok' : 'c-bad'">{{ p.success ? "成功" : p.label || "失败" }}</span>
              <span class="dim">{{ p.steps }} 步</span>
            </a>
          </div>
        </div>

        <!-- 编辑区 -->
        <div v-if="editing && editing.run_id === item.run_id" class="form">
          <div class="frow">
            <label class="flabel" v-if="editing.action === 'override'">改判为</label>
            <select v-if="editing.action === 'override'" v-model="editing.kind" class="finput">
              <option v-for="k in options(item)" :key="k.kind" :value="k.kind">{{ k.label }}</option>
            </select>
            <label class="flabel" v-else>原因</label>
            <input
              v-model="editing.note"
              class="finput grow"
              :placeholder="editing.action === 'override' ? '为什么机器判错了？（必填，改判是唯一会改写结论的动作）' : '为什么这次不算失败？（例如：任务期望本身写错了）'"
            />
          </div>
          <div class="fbtns">
            <button class="btn primary" :disabled="busy === item.run_id" @click="submitForm">
              {{ editing.action === "override" ? "提交改判" : "提交驳回" }}
            </button>
            <button class="btn" @click="editing = null">取消</button>
          </div>
        </div>

        <!-- 操作区 -->
        <div v-else class="acts">
          <button class="btn primary" :disabled="busy === item.run_id" @click="confirm(item)">确认机器判定</button>
          <button class="btn" :disabled="busy === item.run_id" @click="startOverride(item)">改判为另一种病</button>
          <button class="btn" :disabled="busy === item.run_id" @click="startDismiss(item)">判为不算失败</button>
          <a class="btn ghost" :href="'#/runs/' + encodeURIComponent(item.run_id)" @click.prevent="go('/runs/' + item.run_id)">
            先看完整轨迹 →
          </a>
        </div>
      </article>
    </section>

    <!-- 已签字 -->
    <section v-if="resolved.length" class="block">
      <div class="eyebrow">已签字 · {{ resolved.length }} 条</div>
      <article v-for="item in resolved" :key="item.run_id" class="card rv done">
        <div class="rvhead">
          <div class="rvleft">
            <span class="rid mono">{{ item.run_id }}</span>
            <span class="rtask mono">{{ item.task_id }} · {{ item.version }}</span>
          </div>
          <div class="rvright">
            <span class="tag" :class="actionTone[item.review!.action] || 'info'">
              {{ queue.actions_label[item.review!.action] || item.review!.action }}
            </span>
          </div>
        </div>
        <div class="seal">
          <span class="sealtag">
            机器原判：{{ item.machine.label }}
            <template v-if="item.review!.action === 'override' && item.review!.kind">
              → 改判为
              {{ kinds.find((k) => k.kind === item.review!.kind)?.label || item.review!.kind }}
            </template>
            <template v-else-if="item.review!.action === 'dismiss'"> → 不计为失败</template>
          </span>
          <span class="sealmeta mono">
            {{ item.review!.reviewer }} · {{ item.review!.at.slice(0, 16).replace("T", " ") }}
          </span>
        </div>
        <p v-if="item.review!.note" class="sealnote">「{{ item.review!.note }}」</p>
        <div class="acts">
          <button class="btn tiny" :disabled="busy === item.run_id" @click="undo(item)">撤销复核</button>
        </div>
      </article>
    </section>

    <!-- 复核对结论的影响 -->
    <section class="block">
      <div class="eyebrow">复核对结论的影响</div>
      <div v-if="diff.length" class="card">
        <table class="dt mono">
          <thead>
            <tr>
              <th>病种</th>
              <th>机器判定</th>
              <th>人工复核后</th>
              <th>变化</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="d in diff" :key="d.label">
              <td class="dl">{{ d.label }}</td>
              <td>{{ d.before }}</td>
              <td>{{ d.after }}</td>
              <td :class="d.after > d.before ? 'c-bad' : 'c-ok'">
                {{ d.after > d.before ? "+" : "" }}{{ d.after - d.before }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-else class="empty">
        还没有发生任何改判或驳回 —— 复核后的分布与机器判定完全一致。
        确认（而不是改判）不会改变分布，这是刻意的：签字确认不该被算成一次「动作」。
      </p>
      <p class="note">
        失败总数：机器判定 {{ queue.total_failed }} 条 → 人工复核后 {{ queue.after_failed }} 条。
        {{ queue.caveat }}
      </p>
    </section>

    <div class="cta">
      <button class="btn" @click="go('/experiments')">看逐任务实验矩阵 →</button>
      <button class="btn" @click="go('/runs')">回到轨迹浏览器 →</button>
    </div>
  </template>
</template>

<style scoped>
.head { margin-bottom: 26px; }
h1 { font-size: 23px; margin: 0 0 8px; letter-spacing: 0.3px; font-weight: 600; }
.sub { margin: 0; color: var(--muted); max-width: 68ch; font-size: 14px; }
.sub b { color: var(--text); font-weight: 600; }
.meta { color: var(--dim); font-size: 11.5px; display: flex; gap: 8px; align-items: center; margin-top: 12px; flex-wrap: wrap; }
.meta .dot { color: var(--line-2); }

.stats { display: flex; align-items: center; gap: 38px; padding: 20px 24px; flex-wrap: wrap; }
.stats > div { display: flex; flex-direction: column; }
.stats b { font-size: 22px; font-weight: 600; }
.stats span { font-size: 11px; color: var(--dim); }
.breakdown { margin-left: auto; gap: 14px; flex-direction: row !important; font-size: 11px; color: var(--dim); }
.breakdown span { font-size: 11px; }

.flash {
  margin: 18px 0 0; padding: 11px 16px; border-radius: 8px;
  background: #0d1b1d; border: 1px solid #1c3f42; color: #b9e6e2; font-size: 13px;
}

.pad-empty { padding: 22px 24px; }

/* 复核卡片 */
.rv { padding: 18px 22px 16px; margin-bottom: 12px; }
.rv.done { background: var(--panel-2); }
.rvhead { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
.rvleft { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
.rid { font-size: 13.5px; color: var(--text); }
.rtask { font-size: 11px; color: var(--dim); }
.rvright { display: flex; align-items: center; gap: 10px; }
.conf { font-size: 11px; color: var(--muted); }
.conf.low { color: var(--amber); }

.judge { margin-top: 14px; padding: 13px 16px; border-radius: 8px; background: #0c1216; border: 1px solid #1a2229; }
.jkind { display: flex; align-items: baseline; gap: 10px; }
.jlabel { font-size: 15px; color: var(--red); font-weight: 600; }
.jstep { font-size: 10.5px; color: var(--dim); }
.jevidence { margin: 7px 0 0; font-size: 13px; color: var(--muted); line-height: 1.65; }

.sigblock { margin-top: 14px; }
.siglist { margin: 8px 0 0; padding-left: 18px; font-size: 12.5px; color: var(--muted); }
.siglist li { margin-bottom: 3px; }
.hint { margin: 8px 0 0; font-size: 12px; color: var(--dim); }

.peers { margin-top: 14px; }
.peerlist { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 8px; }
.peer {
  display: flex; gap: 10px; align-items: center; font-size: 11.5px;
  padding: 6px 10px; border-radius: 6px; border: 1px solid #1a2229; background: #0c1216;
  color: var(--muted); text-decoration: none;
}
.peer:hover { border-color: #2a333d; color: var(--text); }

.acts { display: flex; gap: 8px; margin-top: 18px; padding-top: 14px; border-top: 1px solid #151b21; flex-wrap: wrap; }
.btn.ghost { background: transparent; border-color: transparent; color: var(--dim); text-decoration: none; }
.btn.ghost:hover { color: var(--cyan); background: transparent; }

.form { margin-top: 18px; padding-top: 14px; border-top: 1px solid #151b21; }
.frow { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.flabel { font: 11px/1 var(--mono); color: var(--dim); letter-spacing: .6px; }
.finput {
  font: 12.5px var(--mono); padding: 8px 10px; border-radius: 6px;
  background: #0c1216; border: 1px solid var(--line-2); color: var(--text);
}
.finput.grow { flex: 1; min-width: 240px; }
.fbtns { display: flex; gap: 8px; margin-top: 12px; }

/* 签章 */
.seal { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-top: 12px; flex-wrap: wrap; }
.sealtag {
  font: 11.5px/1 var(--mono); padding: 6px 10px; border-radius: 5px;
  color: #a8e8c6; border: 1px solid #1e4131; background: #0f1d16;
}
.sealmeta { font-size: 10.5px; color: var(--dim); }
.sealnote { margin: 10px 0 0; font-size: 13px; color: var(--muted); font-style: italic; }
.rv.done .acts { border-top-color: #12181d; margin-top: 12px; }

/* 影响表 */
.dt { width: 100%; border-collapse: collapse; font-size: 12.5px; }
.dt th { text-align: right; padding: 12px 18px; font-weight: 500; color: var(--dim); font-size: 10.5px; letter-spacing: .8px; border-bottom: 1px solid var(--line); }
.dt th:first-child { text-align: left; }
.dt td { text-align: right; padding: 10px 18px; border-bottom: 1px solid #151b21; }
.dt td:first-child { text-align: left; }
.dt .dl { font-family: system-ui, sans-serif; font-size: 13px; }
.dt tr:last-child td { border-bottom: 0; }

.cta { display: flex; gap: 10px; margin-top: 34px; flex-wrap: wrap; }

@media (max-width: 900px) {
  .stats { gap: 22px; }
  .breakdown { margin-left: 0; }
}
</style>
