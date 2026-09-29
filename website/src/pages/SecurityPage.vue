<script setup>
import PageHero from "../components/PageHero.vue";
import UiIcon from "../components/UiIcon.vue";
const controls = [
 ["Policy before side effects", "Every tool declares risk and crosses a centralized policy decision before changing files, launching processes or reaching the network."],
 ["Bounded filesystem", "Canonical path validation and deny rules keep mutations inside explicitly allowed roots."],
 ["Secret hygiene", "Credential references are stored separately with restrictive permissions; logs and UI surfaces redact sensitive values."],
 ["Process containment", "No-shell execution by default, timeouts and process-group cancellation prevent orphan workloads."],
 ["Auditable history", "Mission and session events retain identity, causality, inputs, outputs and cryptographic integrity links."],
 ["Controlled access", "The web control plane uses rotating bearer tokens, role-aware sessions and explicit six-digit device pairing."],
];
</script>
<template><div><PageHero eyebrow="SECURITY" title="Agency without surrendering control." lead="Autonomy increases the cost of weak boundaries. Unknown Agent treats policy, provenance and cancellation as runtime fundamentals—not optional wrappers."><div class="security-badge"><UiIcon name="shield" :size="26" /><div><b>Open security model</b><span>Inspectable source · MIT license · self-hosted control plane</span></div></div></PageHero>
<section class="section no-top"><div class="container"><div class="control-grid"><article v-for="(item,i) in controls" :key="item[0]"><span>0{{ i+1 }}</span><h3>{{ item[0] }}</h3><p>{{ item[1] }}</p></article></div></div></section>
<section class="section dark-panel"><div class="container split"><div><p class="eyebrow">THREAT MODEL</p><h2>Assume output can be wrong.</h2><p class="lede-sm">Model text, tool arguments, external pages and subprocess output are untrusted. Typed validation and permission boundaries contain malformed or adversarial output before it becomes a side effect.</p></div><div class="security-flow"><div>Untrusted intent</div><i>↓</i><div>Schema validation</div><i>↓</i><div>Policy decision</div><i>↓</i><div>Bounded execution</div><i>↓</i><div>Recorded evidence</div></div></div></section>
<section class="section"><div class="container split"><div><p class="eyebrow">REPORTING</p><h2>Found a vulnerability?</h2><p class="lede-sm">Please use private vulnerability reporting rather than a public issue. Include reproduction details, affected versions and potential impact.</p><a class="button primary" href="https://github.com/klowerg/Unknown-Agent/security/advisories/new" target="_blank">Report privately <UiIcon name="arrow" /></a></div><div class="policy-card"><span>SECURITY.md</span><p>Our disclosure process, supported versions and response expectations live with the source.</p><a href="https://github.com/klowerg/Unknown-Agent/blob/main/SECURITY.md" target="_blank">Read security policy →</a></div></div></section></div></template>
