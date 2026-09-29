<script setup>
import PageHero from "../components/PageHero.vue";
const changes=[
 {date:'Current',version:'main',title:'Durable session branches',items:['Event-sourced forks at any visible event','SHA-256 integrity chains and deterministic replay','Semantic merge previews with provenance','Seven new agent-native session tools']},
 {date:'Phase 1',version:'coding core',title:'LSP intelligence & worktrees',items:['Persistent language server supervision','Eight code intelligence tools','Mission-owned isolated worktrees','Cleanup, stale-lease recovery and ownership checks']},
 {date:'Foundation',version:'sovereign runtime',title:'Long-horizon execution',items:['Durable mission engine and typed reasoning','Frontier model routing with dynamic budgets','Failure classification and stall freezing','Mission API, SSE and web console integration']},
];
</script>
<template><div><PageHero eyebrow="CHANGELOG" title="What changed, and why it matters." lead="A human-readable record of meaningful runtime improvements. Commit-level history remains available in the repository." />
<section class="section no-top"><div class="container narrow"><div class="change-list"><article v-for="change in changes" :key="change.title"><div class="change-meta"><span>{{change.date}}</span><code>{{change.version}}</code></div><div><h2>{{change.title}}</h2><ul><li v-for="item in change.items" :key="item">{{item}}</li></ul></div></article></div><div class="history-link"><a href="https://github.com/flormariap/unknown-agent/commits/main" target="_blank">Browse complete commit history →</a></div></div></section></div></template>
