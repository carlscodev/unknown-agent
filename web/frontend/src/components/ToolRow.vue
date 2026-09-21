<script setup>
/* One tool call: icon, name, arguments, status and — on demand — the exact
   output the tool returned.  Running calls pulse; finished ones show how long
   they took. */
import { computed, ref } from "vue";
import { Icon } from "../icons.js";

const props = defineProps({
  item: { type: Object, required: true },
});

const open = ref(false);

const status = computed(() => {
  if (props.item.status === "start") return "running";
  return props.item.ok ? "done" : "failed";
});

const argLine = computed(() => {
  const args = props.item.arguments || {};
  return Object.entries(args)
    .map(([k, v]) => {
      const text = typeof v === "string" ? v : JSON.stringify(v);
      const short = text.length > 60 ? text.slice(0, 60) + "…" : text;
      return `${k}=${short}`;
    })
    .join("  ");
});

function ms(v) {
  const n = Number(v || 0);
  return n >= 1000 ? (n / 1000).toFixed(1) + "s" : Math.round(n) + "ms";
}
</script>

<template>
  <div class="tool" :class="status">
    <button class="thead" @click="open = !open">
      <span v-if="status === 'running'" class="spin" />
      <Icon v-else :name="status === 'done' ? 'check' : 'x'" :size="12" />
      <span class="name mono">{{ item.name }}</span>
      <span v-if="argLine" class="args mono">{{ argLine }}</span>
      <span class="grow" />
      <span v-if="status !== 'running'" class="dur mono num">{{ ms(item.duration_ms) }}</span>
      <Icon name="chevron" :size="12" :class="{ flip: open }" />
    </button>
    <div v-if="status === 'running'" class="tbar"><span /></div>
    <pre v-if="open" class="tout mono">{{ item.summary || item.output || "(sin salida)" }}</pre>
  </div>
</template>
