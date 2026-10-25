<script setup lang="ts">
/** 多轮会话视图。
 *
 * 这一页要回答的问题不是「支持多轮了吗」，而是：
 * **只测单轮的团队，漏掉了什么？**
 * 所有版式都围绕这一个问题排。
 */
import { computed, onMounted, ref } from "vue";
import { api, type Scenario, type SessionList, type SessionRow } from "../api";
import { go } from "../router";

const data = ref<SessionList | null>(null);
const scenarios = ref<Scenario[]>([]);
const error = ref<string | null>(null);

onMounted(async () => {
  try {
    const [s, sc] = await Promise.all([api.sessions(), api.scenarios()]);
    data.value = s;
    scenarios.value = sc.scenarios ?? [];
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
  }
});

const sum = computed(() => data.value?.summary ?? null);
const rows = computed(() => data.value?.sessions ?? []);

/** 按场景聚合：同一场景的 v1/v2 并排看，才知道新版本有没有改善跨轮表现 */
const byScenario = computed(() => {
  const m = new Map<string, SessionRow[]>();
  for (const r of rows.value) {
    const arr = m.get(r.scenario_id) ?? [];
    arr.push(r);
    m.set(r.scenario_id, arr);
  }
  return [...m.entries()]
    .map(([id, list]) => ({
      id,
      meta: scenarios.value.find((s) => s.scenario_id === id),
      list: list.sort((a, b) => a.version.localeCompare(b.version)),
      passed: list.filter((x) => x.success).length,
      total: list.length,
    }))
    .sort((a, b) => a.id.localeCompare(b.id));
});

const maxProg = computed(() => Math.max(0.01, ...(sum.value?.progression ?? []).map((p) => p.rate)));

/** 末轮是否比首轮更差。这句解释必须跟着数据走 ——
 *  写死成「越聊越差」，一旦数据反过来，这一页就在用自己的结论打自己的脸。 */
const laterWorse = computed(() => {
  const p = sum.value?.progression ?? [];
  return p.length > 1 && p[p.length - 1].rate > p[0].rate;
});

const crossPatches = computed(() => {
  const out: { kind: string; label: string; count: number; note: string }[] = [];
  for (const k of sum.value?.kinds ?? []) out.push(k as never);
  return out;
});

function pct(v: number | undefined | null): string {
  return v == null ? "—" : `${Math.round(v * 100)}%`;
}
</script>

<template>
  <div v-if="error" class="err">加载失败：{{ error }}</div>
  <p v-else-if="!data" class="loading">正在读取会话…</p>

  <div v-else-if="!data.ready" class="card block">
    <p class="eyebrow">还没有数据</p>
    <p class="note">{{ data.reason }}</p>
  </div>

  <template v-else>
    <div class="head">
      <p class="eyebrow">多轮会话</p>
      <h1>单轮测试漏掉的那一层</h1>
      <p class="sub">
        {{ sum!.sessions }} 场会话、{{ sum!.turns }} 轮对话，全部真实跑出来（各轮共享世界状态与对话历史）。
        同一个 Agent，单轮看上去正常，放进会话里就不一定了。
      </p>
    </div>

    <!-- 一句话结论：先说「漏了什么样本」，再说「抓了什么病」。
         不把漏检率当主数字 —— 它是 0 的时候这张卡会读成「这功能什么都没发现」，
         而事实是：那几轮之所以测不出来，是因为单轮根本造不出这条用例。 -->
    <section class="card concl">
      <div class="cbig">
        <b class="c-warn">{{ sum!.needs_history_turns }}<i>/{{ sum!.turns }}</i></b>
        <span>个轮次，单轮测试连样本都造不出来</span>
      </div>
      <p class="cwhy">
        这些轮次的用户输入里没有订单号、没有任务对象——「就按你说的办」「这个不要了」。
        单轮测试给它一条孤立输入，不是会做错，是<b>无从下手</b>。
        多轮的第一层价值就在这里：<b>不是测了没过，是这条用例压根造不出来</b>。
      </p>
      <p class="cwhy" v-if="sum!.harmed_turns > 0">
        另有一类更危险：<b>{{ sum!.harmed_turns }}</b> 个轮次「被历史害了」——
        单独拿出来测会通过，放进会话里却挂了，占全部 {{ sum!.failed_turns }} 个失败轮次的
        <b>{{ pct(sum!.blind_spot_rate) }}</b>。它的绿灯是真的，只是不该亮。
      </p>
      <p class="cwhy" v-else>
        这一批里被历史害到失败的轮次是 <b>0</b> 个：这个模型在改口跟随、跨轮指代、重复开单上都没栽。
        写出来是因为<b>这个数字必须能是 0</b>——如果它永远大于 0，那多半是判据在误报，而不是模型真的更差。
        上面那 {{ sum!.needs_history_turns }} 轮依然说明：不漏样本和抓跨轮病是两件事，前者先发生。
      </p>
    </section>

    <!-- 两种视角的对照 -->
    <section class="block">
      <p class="eyebrow">两种算法，两个数字</p>
      <div class="vs">
        <div class="vscard">
          <span class="vsq">按轮算</span>
          <b class="mono">{{ pct(sum!.turn_success_rate) }}</b>
          <em>每一轮单独判定成功与否，然后求比例</em>
        </div>
        <div class="vsarrow">≠</div>
        <div class="vscard">
          <span class="vsq">按会话算</span>
          <b class="mono">{{ pct(sum!.session_success_rate) }}</b>
          <em>一场会话里每一轮都通过，这场才算通过</em>
        </div>
      </div>
      <p class="note">
        两个数字差多少，就是「聊到一半崩掉」的比例。只报按轮的成功率会把它藏起来：
        一场 3 轮会话挂 1 轮，按轮算是 67%，按会话算是 0 —— 而用户真实的感受是后者。
      </p>
    </section>

    <!-- 逐轮递进：越聊越差还是越聊越稳 -->
    <section class="block">
      <p class="eyebrow">第几轮开始崩</p>
      <div class="prog">
        <div v-for="p in sum!.progression" :key="p.turn" class="prow">
          <span class="pl mono">第 {{ p.turn }} 轮</span>
          <div class="ptrack">
            <div class="pfill" :style="{ width: (p.rate / maxProg) * 100 + '%' }"></div>
          </div>
          <span class="pv mono">{{ pct(p.rate) }}</span>
          <span class="pn mono">{{ p.failed }}/{{ p.n }}</span>
        </div>
      </div>
      <p class="note">
        每轮的分母是「有这么多轮的会话数」。<template v-if="laterWorse">
          这一批是后面几轮更崩 —— 前面轮次留下的状态，要到后面才变成债。
        </template>
        <template v-else>
          这一批反过来：第 1 轮最差。因为第 1 轮往往就要决定「这件事该不该直接办」，
          后面的轮次多半只是回答问题。<b>所以逐轮失败率只能说明「哪一轮难」，
          不能单独当跨轮退化的证据</b> —— 要判跨轮，看的是下面那三类跨轮病。
        </template>
      </p>
    </section>

    <!-- 会话逐场：同一场景两版并排 -->
    <section class="block">
      <p class="eyebrow">逐场景对账</p>
      <div class="scgrid">
        <div v-for="s in byScenario" :key="s.id" class="scard">
          <div class="shead">
            <b class="mono">{{ s.id }}</b>
            <span class="tag">{{ s.meta?.label ?? s.id }}</span>
            <span class="sres mono" :class="s.passed === s.total ? 'c-ok' : 'c-bad'">
              {{ s.passed }}/{{ s.total }} 场通过
            </span>
          </div>
          <p class="stest" v-if="s.meta">
            测的是 <b>{{ s.meta.note }}</b>
          </p>
          <div class="srows">
            <a
              v-for="r in s.list"
              :key="r.session_id"
              class="sr"
              :href="'#/sessions/' + encodeURIComponent(r.session_id)"
              @click.prevent="go('/sessions/' + r.session_id)"
            >
              <span class="sv mono">{{ r.version }}</span>
              <span class="sdot" :class="r.success ? 'ok' : 'bad'"></span>
              <span class="sdet">
                <template v-if="r.success">每轮都通过</template>
                <template v-else>
                  第 {{ r.failed_turns.join("、") }} 轮失败
                  <em v-if="r.cross_labels.length">· {{ r.cross_labels.join("、") }}</em>
                </template>
              </span>
              <span class="sgo">→</span>
            </a>
          </div>
        </div>
      </div>
    </section>

    <!-- 三类跨轮病 -->
    <section class="block" v-if="crossPatches.length">
      <p class="eyebrow">只有多轮才会出现的三类问题</p>
      <div class="kinds">
        <div v-for="k in crossPatches" :key="k.kind" class="kcard">
          <div class="khead">
            <b>{{ k.label }}</b>
            <span class="mono c-warn">{{ k.count }} 次</span>
          </div>
          <p>{{ k.note }}</p>
        </div>
      </div>
      <p class="note">
        这三类病和单轮的「过度转人工」「只问不做」不是一回事：
        单轮病改的是「这一次怎么决策」，跨轮病改的是「记不记得住、跟不跟得上」。
        修复手段也不同 —— 后者要往 system prompt 里加记忆与去重规则。
      </p>
    </section>

    <!-- 一条都没检出来时，必须把「没检出」写出来。
         藏起来最省事，但那等于让读者以为这功能没做完；
         而真相是判据被刻意调成了「宁可漏报」——这两件事必须分开讲。 -->
    <section class="block empty" v-else-if="sum!.sessions">
      <p class="eyebrow">只有多轮才会出现的三类问题</p>
      <p class="note">
        这一批 {{ sum!.sessions }} 场会话、{{ sum!.turns }} 轮里，三类跨轮病
        <b>一次都没触发</b>：没有「用户改了口还在按旧目标办」，
        没有「上一轮办过的事又办一遍」，也没有「跨轮指代丢失」。
      </p>
      <p class="note">
        检测跑了，是判据被刻意调成<b>宁可漏报、不许误报</b>——误报会让人去改一条本来就对的规则，
        代价比漏报大得多（每一类都有专门的「不许触发」单测锁着）。
        真实的结论是：这个模型的错发生在更早的地方（该直接办还是该先问），
        轮不到跨轮病出场。上面那 {{ sum!.failed_turns }} 个失败轮次里，
        {{ sum!.needs_history_turns }} 轮连样本都造不出来，但它们失败的原因仍是单轮决策。
      </p>
    </section>

    <section class="card block limits">
      <p class="eyebrow">这一页的边界</p>
      <ul>
        <li>{{ sum!.caveat }}</li>
        <li>
          会话里的每一轮<b>单独判定</b>，会话成功 = 每轮都成功。压缩成一个布尔值会丢掉「第几轮崩的」这条线索。
        </li>
        <li>
          跨轮结论只依赖可见证据：哪一轮的哪句话、哪次调用的哪个参数。
          <b>不做语义连贯性打分</b> —— 那种判断没法反驳，也就没法修。
        </li>
        <li>
          各轮<b>共享世界状态</b>：第 1 轮退掉的钱，第 2 轮查出来就是已退。
          不共享状态的「多轮」只是把几条轨迹粘上同一个编号。
        </li>
      </ul>
    </section>

    <div class="cta">
      <button class="btn" @click="go('/prompts')">去 Prompt 版本 → 把这些规则写进去</button>
      <button class="btn" @click="go('/runs')">看单轮轨迹 →</button>
    </div>
  </template>
</template>

<style scoped>
.head { margin-bottom: 22px; }
.head h1 { font-size: 25px; margin: 4px 0 8px; letter-spacing: -0.3px; }
.sub { color: var(--muted); font-size: 13.5px; max-width: 720px; line-height: 1.75; }

.concl { padding: 22px 24px; border-left: 2px solid var(--c-warn, #e0a34a); }
.cbig { display: flex; align-items: baseline; gap: 12px; }
.cbig b { font: 700 34px/1 var(--mono); letter-spacing: -1px; }
.cbig b i { font-style: normal; font-size: 19px; color: var(--dim); letter-spacing: 0; }
.cbig span { color: var(--muted); font-size: 13.5px; }
.cwhy { margin: 12px 0 0; font-size: 13px; color: var(--muted); line-height: 1.85; max-width: 820px; }
.cwhy b { color: var(--text); }

.vs { display: grid; grid-template-columns: 1fr 40px 1fr; align-items: stretch; gap: 0; }
.vscard {
  background: var(--panel, #11161b); border: 1px solid var(--line); border-radius: 8px;
  padding: 18px 20px; display: flex; flex-direction: column; gap: 6px;
}
.vsq { font: 10px/1 var(--mono); letter-spacing: 1.4px; color: var(--dim); text-transform: uppercase; }
.vscard b { font: 700 28px/1.1 var(--mono); letter-spacing: -0.5px; }
.vscard em { font-style: normal; font-size: 12px; color: var(--muted); line-height: 1.6; }
.vsarrow { display: grid; place-items: center; color: var(--dim); font-size: 18px; }

.prog { display: flex; flex-direction: column; gap: 8px; }
.prow { display: grid; grid-template-columns: 70px 1fr 46px 56px; align-items: center; gap: 10px; }
.pl { font-size: 12px; color: var(--muted); }
.ptrack { height: 8px; background: #0c1116; border-radius: 999px; overflow: hidden; }
.pfill { height: 100%; background: linear-gradient(90deg, #6b3a3d, var(--red)); border-radius: 999px; }
.pv { font-size: 12px; color: var(--text); text-align: right; }
.pn { font: 11px var(--mono); color: var(--dim); text-align: right; }

.scgrid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 14px; }
.scard { background: var(--panel, #11161b); border: 1px solid var(--line); border-radius: 9px; padding: 15px 16px; }
.shead { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.shead b { font-size: 12.5px; color: var(--text); }
.sres { margin-left: auto; font-size: 11px; }
.stest { margin: 9px 0 12px; font-size: 12px; color: var(--muted); line-height: 1.7; }
.stest b { color: var(--text); font-weight: 500; }
.srows { display: flex; flex-direction: column; gap: 6px; }
.sr {
  display: flex; align-items: center; gap: 9px; padding: 8px 10px; border-radius: 6px;
  background: #0c1116; border: 1px solid transparent; text-decoration: none; color: var(--muted);
  font-size: 12px; transition: border-color .12s, background .12s;
}
.sr:hover { border-color: #243039; background: #0e141a; }
.sv { font-size: 10.5px; color: var(--dim); min-width: 18px; }
.sdot { width: 6px; height: 6px; border-radius: 999px; flex: 0 0 auto; }
.sdot.ok { background: var(--green); }
.sdot.bad { background: var(--red); }
.sdet { flex: 1; min-width: 0; color: var(--text); font-size: 12px; }
.sdet em { font-style: normal; color: var(--c-warn, #e0a34a); }
.sgo { color: var(--dim); }

.kinds { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 12px; }
.kcard { background: var(--panel, #11161b); border: 1px solid var(--line); border-radius: 9px; padding: 15px 16px; }
.khead { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 7px; }
.khead b { font-size: 13px; }
.khead span { font-size: 11.5px; }
.kcard p { margin: 0; font-size: 12px; color: var(--muted); line-height: 1.7; }

/* 一条都没检出时的空位：用虚线框标出来，读起来是「这一格空着」而不是「忘了做」 */
.empty { border: 1px dashed var(--line); border-radius: 9px; padding: 18px 20px; }
.empty .note { margin: 8px 0 0; max-width: 860px; }
.empty .note b { color: var(--text); }

.limits { padding: 20px 22px; }
.limits ul { margin: 10px 0 0; padding-left: 18px; color: var(--muted); font-size: 12.5px; line-height: 1.85; }
.limits li { margin-bottom: 6px; }
.limits b { color: var(--text); }

.cta { display: flex; gap: 10px; margin: 24px 0 40px; flex-wrap: wrap; }

@media (max-width: 820px) {
  .vs { grid-template-columns: 1fr; }
  .vsarrow { padding: 6px 0; }
  .scgrid, .kinds { grid-template-columns: 1fr; }
}
</style>
