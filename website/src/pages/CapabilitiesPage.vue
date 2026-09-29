<script setup>
import PageHero from "../components/PageHero.vue";
import UiIcon from "../components/UiIcon.vue";
import { capabilities } from "../content.js";
const groups = [
  { n:"01", name:"Code intelligence", items:["Persistent multi-language LSP supervisors", "Definitions, references and workspace symbols", "Diagnostics with restart recovery", "AST-aware search and structured edits"] },
  { n:"02", name:"Autonomous execution", items:["Durable mission DAGs", "Role-specialized subagents", "Mission-owned Git worktrees", "Cancellation through process groups"] },
  { n:"03", name:"Reasoning control", items:["Dynamic thinking budgets by phase", "Provider-native prompt caching", "Typed Pydantic v2 decisions", "Stall detection and forced replanning"] },
  { n:"04", name:"Evidence & safety", items:["Build, test and lint gates", "Headless Chromium verification", "Deny-by-default path policy", "Append-only event telemetry"] },
  { n:"05", name:"Durable memory", items:["Working, session and project layers", "Event-sourced session forks", "Hash-chain integrity checks", "Semantic merge conflict previews"] },
  { n:"06", name:"Provider fabric", items:["21 provider adapters", "OpenAI and Anthropic native APIs", "OpenRouter and compatible gateways", "Ollama and offline fallback"] },
];
</script>
<template><div><PageHero eyebrow="CAPABILITIES" title="A serious toolbelt for serious work." lead="Every capability is built as a typed, policy-aware primitive that can be tested, observed, cancelled and composed." />
<section class="section no-top"><div class="container"><div class="cap-grid wide"><article v-for="item in capabilities" :key="item.label" class="cap-card"><div class="cap-icon"><UiIcon :name="item.icon" /></div><h3>{{ item.label }}</h3><p>{{ item.text }}</p></article></div></div></section>
<section class="section dark-panel"><div class="container"><div class="section-head row"><div><p class="eyebrow">FULL STACK</p><h2>Depth at every layer.</h2></div><p>Not a list of prompts. A set of interoperable runtime systems.</p></div><div class="cap-groups"><article v-for="group in groups" :key="group.n"><div class="group-head"><span>{{ group.n }}</span><h3>{{ group.name }}</h3></div><ul><li v-for="item in group.items" :key="item"><UiIcon name="check" :size="15" />{{ item }}</li></ul></article></div></div></section>
<section class="section mini-cta"><div class="container"><h2>Put the full runtime on your machine.</h2><RouterLink class="button primary" to="/download">Download Unknown Agent <UiIcon name="download" /></RouterLink></div></section></div></template>
