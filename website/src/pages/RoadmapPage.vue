<script setup>
import PageHero from "../components/PageHero.vue";
import { phases } from "../content.js";
const detail=[
 ['Foundation shipped',['Typed reasoning protocol','Durable mission engine','AST and repository intelligence','Provider routing and prompt caching','Web control plane and event stream']],
 ['Coding core shipped',['Persistent LSP tools','Mission-linked Git worktrees','Event-sourced session forks','Tiered context compaction','Transactional mutation queues']],
 ['Extensible runtime — shipped',['Signed capability manifests','Hook and event bus','Persistent cron and signed-webhook scheduler','Native cross-platform sandbox profiles','Stable JSON-RPC / ACP v1 service']],
 ['Omnichannel & ecosystem',['Channel-neutral durable gateway — shipped','Telegram, Discord and Slack','WhatsApp and Matrix','ACL pairing and device delegation','Desktop, mobile and marketplace']],
];
</script>
<template><div><PageHero eyebrow="ROADMAP" title="From coding agent to sovereign agent platform." lead="A staged program keeps every layer useful, testable and production-ready before the next one compounds it." />
<section class="section no-top"><div class="container"><div class="roadmap-line"><article v-for="(phase,i) in phases" :key="phase.n" :class="{active:i===0}"><div class="road-dot"></div><span>{{phase.n}} · {{phase.status}}</span><h2>{{phase.title}}</h2><p>{{phase.text}}</p></article></div></div></section>
<section class="section dark-panel"><div class="container"><div class="section-head"><p class="eyebrow">DELIVERY MAP</p><h2>What each horizon contains.</h2></div><div class="road-detail"><article v-for="(block,i) in detail" :key="block[0]"><div><span>0{{i+1}}</span><h3>{{block[0]}}</h3></div><ul><li v-for="item in block[1]" :key="item">{{item}}</li></ul></article></div></div></section>
<section class="section"><div class="container narrow center-copy"><p class="eyebrow">OPERATING PRINCIPLE</p><h2>No simulated checkmarks.</h2><p class="lede">A roadmap item ships only with typed contracts, permissions before side effects, durable state, real cancellation, telemetry, malformed-output containment and focused failure-path tests.</p><a class="button ghost" href="https://github.com/flormariap/unknown-agent/issues" target="_blank">Follow development</a></div></section></div></template>
