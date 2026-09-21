<script setup>
/* One-line run status, sitting right above the composer: what the agent is
   doing right now and how long each phase has taken.  It never repeats the
   answer — the text, the reasoning and the tool log all live in the message. */
import { computed } from "vue";
import { Icon } from "../icons.js";

const props = defineProps({
  active: { type: Boolean, default: false },
  phase: { type: String, default: "" },
  thinkSecs: { type: Number, default: 0 },
  genSecs: { type: Number, default: 0 },
  tools: { type: Number, default: 0 },
  files: { type: Number, default: 0 },
  toolTotal: { type: Number, default: 0 },
  live: { type: Boolean, default: false },
  reconnecting: { type: Boolean, default: false },
  timing: { type: Object, default: null },
});

const label = computed(() => {
  if (!props.active) return "";
  if (props.phase === "generating") return "generando";
  if (props.phase === "tools") return "ejecutando herramientas";
  return "pensando";
});
const visible = computed(
  () => props.active || props.reconnecting || (!props.live && !props.active) || props.timing,
);

function fmt(s) {
  const v = Number(s || 0);
  return v >= 10 ? v.toFixed(0) + "s" : v.toFixed(1) + "s";
}
function ms(v) {
  return (Number(v || 0) / 1000).toFixed(1) + "s";
}
</script>

<template>
  <div v-if="visible" class="runstatus" :class="{ on: active }" aria-live="polite">
    <span v-if="active" class="pulse" />
    <Icon v-else-if="reconnecting" name="refresh" :size="12" />
    <span v-else class="dot" :class="{ off: !live }" />

    <span v-if="active" class="t">{{ label }}</span>
    <span v-else-if="reconnecting" class="t">reconectando…</span>
    <span v-else-if="!live" class="t">sin socket</span>
    <span v-else class="t">listo</span>

    <template v-if="active">
      <span class="sep" />
      <span class="mono num" :class="{ dim: phase !== 'thinking' }">pensando {{ fmt(thinkSecs) }}</span>
      <span class="sep" />
      <span class="mono num" :class="{ dim: !genSecs }">generando {{ fmt(genSecs) }}</span>
      <span v-if="tools" class="sep" />
      <span v-if="tools" class="mono num">{{ tools }} herramienta{{ tools === 1 ? "" : "s" }}</span>
      <span v-if="files" class="sep" />
      <span v-if="files" class="mono num">{{ files }} archivo{{ files === 1 ? "" : "s" }}</span>
    </template>
    <template v-else-if="timing">
      <span class="sep" />
      <span class="mono num">pensó {{ ms(timing.think_ms) }}</span>
      <span class="sep" />
      <span class="mono num">generó {{ ms(timing.gen_ms) }}</span>
      <span v-if="timing.tool_ms" class="sep" />
      <span v-if="timing.tool_ms" class="mono num">herramientas {{ ms(timing.tool_ms) }}</span>
      <span class="sep" />
      <span class="mono num">total {{ ms(timing.total_ms) }}</span>
    </template>
    <template v-else-if="toolTotal">
      <span class="sep" />
      <span class="mono num">{{ toolTotal }} herramientas conectadas</span>
    </template>
  </div>
</template>
