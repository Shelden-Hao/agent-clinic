<script setup lang="ts">
import { NAV_GROUPS, activeNav, go, route } from "./router";
import Overview from "./views/Overview.vue";
import Experiments from "./views/Experiments.vue";
import Explorer from "./views/Explorer.vue";
import RunDetailView from "./views/RunDetail.vue";
import Patterns from "./views/Patterns.vue";
import Sessions from "./views/Sessions.vue";
import SessionDetailView from "./views/SessionDetail.vue";
import Prompts from "./views/Prompts.vue";
import Review from "./views/Review.vue";
import Live from "./views/Live.vue";
import Intake from "./views/Intake.vue";
</script>

<template>
  <div class="shell">
    <aside class="side">
      <div class="brand">
        <b>Agent 会诊室</b>
        <span>TRACE DIAGNOSTICS</span>
      </div>

      <nav class="nav">
        <template v-for="g in NAV_GROUPS" :key="g.group">
          <div class="nav-g">{{ g.group }}</div>
          <a
            v-for="n in g.items"
            :key="n.key"
            :href="'#' + n.path"
            :class="{ on: activeNav === n.key }"
            @click.prevent="go(n.path)"
          >
            {{ n.label }}
            <i>{{ n.hint }}</i>
          </a>
        </template>
      </nav>

      <div class="side-foot">
        <div>48 条真实轨迹 · 全部离线分析</div>
        <div>
          <a href="/api/docs" target="_blank" rel="noreferrer">接口文档</a> ·
          <a href="/lab/collect.html" target="_blank" rel="noreferrer">采集台</a> ·
          <a href="/lab/session.html" target="_blank" rel="noreferrer">多轮采集</a>
        </div>
      </div>
    </aside>

    <main class="main">
      <Overview v-if="route.name === 'overview'" />
      <Experiments v-else-if="route.name === 'experiments'" />
      <Explorer v-else-if="route.name === 'runs'" />
      <RunDetailView v-else-if="route.name === 'run'" :run-id="route.param!" />
      <Patterns v-else-if="route.name === 'patterns'" />
      <Sessions v-else-if="route.name === 'sessions'" />
      <SessionDetailView v-else-if="route.name === 'session'" :session-id="route.param!" />
      <Prompts v-else-if="route.name === 'prompts'" />
      <Review v-else-if="route.name === 'review'" />
      <Live v-else-if="route.name === 'live'" />
      <Intake v-else-if="route.name === 'intake'" />
      <p v-else class="loading">页面不存在。<a href="#/">返回会诊报告</a></p>
    </main>
  </div>
</template>
