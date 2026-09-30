<script setup>
import UiIcon from "../components/UiIcon.vue";
import CodeWindow from "../components/CodeWindow.vue";
import { capabilities, terminalLines, phases, github } from "../content.js";
</script>
<template>
  <div>
    <section class="hero"><div class="hero-grid" aria-hidden="true"></div><div class="container hero-inner">
      <div class="hero-copy"><a class="release-pill" :href="`${github}/commits/main`" target="_blank"><span>NEW</span> Extensible runtime phase complete · ACP + native sandboxes <UiIcon name="arrow" :size="14" /></a>
        <h1>Build beyond<br><em>the prompt.</em></h1><p class="hero-lead">A sovereign AI engineering runtime that plans deeply, acts through real tools, and keeps working until the proof is green.</p>
        <div class="hero-actions"><RouterLink class="button primary large" to="/download">Get Unknown Agent <UiIcon name="arrow" :size="18" /></RouterLink><RouterLink class="button ghost large" to="/product">Explore the runtime</RouterLink></div>
        <div class="hero-note"><span>No cloud lock-in</span><span>MIT licensed</span><span>Runs where your code lives</span></div>
      </div>
      <div class="terminal-card"><div class="terminal-top"><span class="dots"><i></i><i></i><i></i></span><span>mission / billing-portal</span><span class="live-dot">LIVE</span></div><div class="terminal-body"><div v-for="(line, i) in terminalLines" :key="i" :class="line[0]">{{ line[1] }}</div><span class="cursor"></span></div><div class="terminal-foot"><span>tokens 28.4k</span><span>elapsed 06:42</span><span>branch main</span></div></div>
    </div></section>

    <section class="trust-strip"><div class="container"><span>PROVIDER INDEPENDENT</span><b>OPENAI</b><b>ANTHROPIC</b><b>OPENROUTER</b><b>OLLAMA</b><b>+17 MORE</b></div></section>

    <section class="section"><div class="container"><div class="section-head"><p class="eyebrow">THE DIFFERENCE</p><h2>Not a chatbot.<br>A completion system.</h2><p>Unknown Agent owns the distance between a request and a verified result.</p></div>
      <div class="cap-grid"><article v-for="item in capabilities" :key="item.label" class="cap-card"><div class="cap-icon"><UiIcon :name="item.icon" /></div><h3>{{ item.label }}</h3><p>{{ item.text }}</p><RouterLink to="/capabilities">Learn more <UiIcon name="arrow" :size="14" /></RouterLink></article></div>
    </div></section>

    <section class="section proof-section"><div class="container split"><div><p class="eyebrow">VERIFICATION, NOT VIBES</p><h2>The model proposes.<br>The runtime proves.</h2><p class="lede-sm">Every mission ends at objective gates: tests, builds, lint, diagnostics and real browser flows. Failed evidence sends the agent back into the loop.</p><ul class="check-list"><li>Repository-aware plans with explicit definitions of done</li><li>Isolated Git worktrees for safe parallel execution</li><li>Failure-aware retries that freeze unproductive loops</li><li>Replayable evidence and full event provenance</li></ul></div>
      <div class="loop-diagram"><div class="loop-center"><b>SHIP</b><span>only when green</span></div><div v-for="(x,i) in ['PLAN','ACT','OBSERVE','VERIFY']" :key="x" class="loop-node" :class="`n${i}`"><span>{{ String(i+1).padStart(2,'0') }}</span>{{ x }}</div><svg viewBox="0 0 400 400"><circle cx="200" cy="200" r="145"/><path d="M200 55a145 145 0 0 1 145 145"/></svg></div>
    </div></section>

    <section class="section"><div class="container"><div class="section-head row"><div><p class="eyebrow">ONE RUNTIME, EVERY SURFACE</p><h2>Start in the terminal.<br>Continue anywhere.</h2></div><p>CLI, terminal UI, browser control plane and API share the same policies, tools, sessions and durable state.</p></div>
      <div class="surface-grid"><div class="surface-main"><div class="surface-label">CLI / TUI</div><CodeWindow command='ua build "refactor auth and prove every flow"' label="~/your-project" /><div class="metric-row"><div><b>74</b><span>native tools</span></div><div><b>21</b><span>providers</span></div><div><b>804</b><span>tests passing</span></div></div></div><div class="surface-side"><span class="status-orb"></span><p class="eyebrow">WEB CONTROL PLANE</p><h3>Your agents, live.</h3><p>Stream reasoning, inspect calls, preview artifacts and approve devices from a secure browser console.</p><RouterLink to="/product">See all surfaces <UiIcon name="arrow" :size="15" /></RouterLink></div></div>
    </div></section>

    <section class="section roadmap-preview"><div class="container"><div class="section-head row"><div><p class="eyebrow">THE ROAD AHEAD</p><h2>A complete agent platform.</h2></div><RouterLink class="text-link" to="/roadmap">Full roadmap <UiIcon name="arrow" :size="15" /></RouterLink></div><div class="phase-row"><article v-for="phase in phases" :key="phase.n"><span class="phase-n">{{ phase.n }}</span><span class="phase-status">{{ phase.status }}</span><h3>{{ phase.title }}</h3><p>{{ phase.text }}</p></article></div></div></section>

    <section class="section final-cta"><div class="container"><div class="cta-box"><p class="eyebrow">YOUR CODE. YOUR MODELS. YOUR AGENT.</p><h2>Give the work to something<br>that can finish it.</h2><CodeWindow command='pipx install "git+https://github.com/flormariap/unknown-agent"' label="install" /><div class="cta-links"><RouterLink class="button primary large" to="/download">Start building</RouterLink><a :href="github" class="button ghost large" target="_blank">View on GitHub</a></div></div></div></section>
  </div>
</template>
