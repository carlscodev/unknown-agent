<script setup>
import { computed } from "vue";
import { Icon } from "../icons.js";

/* Live window into the agent's internals: reasoning thoughts, tool calls and
   verification results as they happen, plus real elapsed timers for the
   thinking and generating phases. In plain chat mode (no agent events) it
   falls back to a rolling tail of the generation stream. */
const props = defineProps({
  active: { type: Boolean, default: false },
  activity: { type: Array, default: () => [] },
  phase: { type: String, default: "" },
  thinkSecs: { type: Number, default: 0 },
  genSecs: { type: Number, default: 0 },
  text: { type: String, default: "" },
});

const WINDOW = 340;
const tail = computed(() => {
  const t = props.text.slice(-WINDOW);
  return t.split("\n").slice(-6);
});

const recent = computed(() => props.activity.slice(-7));
const visible = computed(
  () => props.active || props.activity.length > 0 || props.text.length > 0
);
const label = computed(() => (props.phase === "generating" ? "generating" : "agent working"));

function fmt(s) {
  return s >= 10 ? s.toFixed(0) + "s" : s.toFixed(1) + "s";
}
function kindIcon(k) {
  return k === "thought" ? "cpu" : k === "tool" ? "term" : k === "verify" ? "check" : "bolt";
}
</script>

<template>
  <div v-if="visible" class="stream" aria-live="polite">
    <div class="head">
      <span class="pulse" />
      <span class="t">{{ label }}</span>
      <span class="timers">
        <span :class="{ dim: phase === 'generating' }">thinking {{ fmt(thinkSecs) }}</span>
        <span v-if="phase === 'generating'">generating {{ fmt(genSecs) }}</span>
      </span>
    </div>
    <div class="win">
      <template v-if="activity.length">
        <div v-for="(a, i) in recent" :key="i" class="ln act" :class="a.kind">
          <Icon :name="kindIcon(a.kind)" :size="12" />
          <span>{{ a.text }}</span>
        </div>
      </template>
      <template v-else-if="text.length">
        <div v-for="(ln, i) in tail" :key="i" class="ln">{{ ln || " " }}</div>
      </template>
      <template v-else>
        <div class="ln act thought">
          <Icon name="cpu" :size="12" />
          <span>thinking…</span>
        </div>
      </template>
    </div>
  </div>
</template>
