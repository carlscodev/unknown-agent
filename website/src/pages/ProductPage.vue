<script setup>
import PageHero from "../components/PageHero.vue";
import UiIcon from "../components/UiIcon.vue";
const stages = [
  ["01", "Understand", "Maps the repository, stack, conventions, commands, risks and relevant project memory before touching code."],
  ["02", "Plan", "Turns intent into typed, reviewable work with dependencies, acceptance criteria and verification gates."],
  ["03", "Execute", "Runs tools behind policy checks inside mission-owned worktrees. Every mutation is attributable."],
  ["04", "Verify", "Builds, tests, lints and drives Chromium. Evidence—not confidence—controls completion."],
  ["05", "Recover", "Classifies failures, replans counterfactuals and freezes loops after repeated non-progress."],
];
const surfaces = [
  ["Terminal", "A fast, adaptive TUI with streaming, model switching, command palette and machine-clean pipe output."],
  ["Web console", "A self-hosted control plane with live tool inspection, artifact previews and device pairing."],
  ["Python API", "Typed primitives for embedding the runtime, mission engine, providers and policy layer."],
  ["Event stream", "Durable mission events for observability, resumption, external automation and audit."],
];
</script>
<template><div>
  <PageHero eyebrow="PRODUCT" title="The runtime between intent and evidence." lead="Unknown Agent coordinates models, code intelligence, isolated execution, durable memory and verification into one coherent engineering loop."><div class="hero-actions center"><RouterLink class="button primary large" to="/download">Install the runtime <UiIcon name="arrow" /></RouterLink><RouterLink class="button ghost large" to="/developers">Read developer guide</RouterLink></div></PageHero>
  <section class="section dark-panel"><div class="container"><p class="eyebrow">MISSION LOOP</p><div class="stage-list"><article v-for="stage in stages" :key="stage[0]"><span>{{ stage[0] }}</span><h2>{{ stage[1] }}</h2><p>{{ stage[2] }}</p></article></div></div></section>
  <section class="section"><div class="container split"><div><p class="eyebrow">SOVEREIGN BY DESIGN</p><h2>Your infrastructure is the product boundary.</h2><p class="lede-sm">Run local, choose any supported model, keep source and telemetry under your own controls. Unknown Agent is architecture—not a rented worker hidden behind an API.</p></div><div class="principle-stack"><div><b>Provider-portable</b><span>Route each phase to the right model.</span></div><div><b>Local-first</b><span>Ollama and offline operation are first-class.</span></div><div><b>Policy-enforced</b><span>Tools cannot skip the permission layer.</span></div><div><b>Open source</b><span>Inspect the runtime under the MIT license.</span></div></div></div></section>
  <section class="section"><div class="container"><div class="section-head"><p class="eyebrow">SHARED STATE</p><h2>One agent. Four ways in.</h2></div><div class="four-grid"><article v-for="surface in surfaces" :key="surface[0]"><span class="signal"></span><h3>{{ surface[0] }}</h3><p>{{ surface[1] }}</p></article></div></div></section>
  <section class="section mini-cta"><div class="container"><h2>See what the runtime can do.</h2><RouterLink class="button primary" to="/capabilities">Explore capabilities <UiIcon name="arrow" /></RouterLink></div></section>
</div></template>
