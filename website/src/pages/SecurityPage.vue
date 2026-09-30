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
 ["Signed extensions", "Ed25519 manifests bind publisher trust, exact source hashes, runtime compatibility, permissions and declared capabilities before code import."],
 ["Contained hooks", "Runtime hooks receive redacted events through timeout, retry, circuit-breaker and dead-letter boundaries; malformed output cannot stop a mission."],
 ["Signed automation", "Webhook secrets remain references; constant-time HMAC verification, idempotency keys, expiring leases, bounded retries and hash-linked audit records contain autonomous scheduling."],
 ["Native containment", "Workspace and isolated profiles use platform-native process, mount and network boundaries; required mode fails closed when its backend is unavailable."],
 ["Bounded ACP", "ACP sessions use strict JSON-RPC schemas, project-scoped working directories, atomic private metadata, concurrent cancellation and a five-minute request ceiling."],
 ["Contained messaging", "Inbound deliveries are deduplicated and redacted before persistence; integrity checks, leases, bounded retries and a transactional outbox contain channel failures."],
];
</script>
<template><div><PageHero eyebrow="SECURITY" title="Agency without surrendering control." lead="Autonomy increases the cost of weak boundaries. Unknown Agent treats policy, provenance and cancellation as runtime fundamentals—not optional wrappers."><div class="security-badge"><UiIcon name="shield" :size="26" /><div><b>Open security model</b><span>Inspectable source · MIT license · self-hosted control plane</span></div></div></PageHero>
<section class="section no-top"><div class="container"><div class="control-grid"><article v-for="(item,i) in controls" :key="item[0]"><span>0{{ i+1 }}</span><h3>{{ item[0] }}</h3><p>{{ item[1] }}</p></article></div></div></section>
<section class="section dark-panel"><div class="container split"><div><p class="eyebrow">THREAT MODEL</p><h2>Assume output can be wrong.</h2><p class="lede-sm">Model text, tool arguments, external pages and subprocess output are untrusted. Typed validation and permission boundaries contain malformed or adversarial output before it becomes a side effect.</p></div><div class="security-flow"><div>Untrusted intent</div><i>↓</i><div>Schema validation</div><i>↓</i><div>Policy decision</div><i>↓</i><div>Bounded execution</div><i>↓</i><div>Recorded evidence</div></div></div></section>
<section class="section"><div class="container split"><div><p class="eyebrow">REPORTING</p><h2>Found a vulnerability?</h2><p class="lede-sm">Please use private vulnerability reporting rather than a public issue. Include reproduction details, affected versions and potential impact.</p><a class="button primary" href="https://github.com/flormariap/unknown-agent/security/advisories/new" target="_blank">Report privately <UiIcon name="arrow" /></a></div><div class="policy-card"><span>SECURITY.md</span><p>Our disclosure process, supported versions and response expectations live with the source.</p><a href="https://github.com/flormariap/unknown-agent/blob/main/SECURITY.md" target="_blank">Read security policy →</a></div></div></section></div></template>
