<script setup>
import { computed } from "vue";

/* Rolling window over the live stream: keeps a small readable tail of the
   generation, newest at the bottom, older lines sliding up under a fade mask. */
const props = defineProps({ text: { type: String, default: "" } });

const WINDOW = 340;
const lines = computed(() => {
  const tail = props.text.slice(-WINDOW);
  return tail.split("\n").slice(-6);
});
</script>

<template>
  <div class="stream" aria-live="polite">
    <div class="head">
      <span class="pulse" />
      <span class="t">live generation</span>
    </div>
    <div class="win">
      <div v-for="(ln, i) in lines" :key="i" class="ln">{{ ln || " " }}</div>
    </div>
  </div>
</template>
