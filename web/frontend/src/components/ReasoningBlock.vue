<script setup>
/* Internal reasoning, rendered where the answer is being written — not above
   the composer.  While the model is thinking it shows an animated pulse and a
   live counter; once the first visible token lands it collapses to a single
   line with the exact time it took to think. */
import { computed, ref } from "vue";
import { Icon } from "../icons.js";
import { renderMarkdown } from "../format.js";
import Blocks from "./Blocks.vue";

const props = defineProps({
  text: { type: String, default: "" },
  thinking: { type: Boolean, default: false },
  secs: { type: Number, default: 0 },
});

const open = ref(false);
const blocks = computed(() => renderMarkdown(props.text || ""));
const hasText = computed(() => (props.text || "").trim().length > 0);
const shown = computed(() => hasText.value && (props.thinking || open.value));

function fmt(s) {
  return s >= 10 ? s.toFixed(0) + "s" : s.toFixed(1) + "s";
}
</script>

<template>
  <div v-if="thinking || hasText" class="reasoning" :class="{ live: thinking }">
    <button class="rhead" @click="open = !open">
      <span v-if="thinking" class="pulse" />
      <Icon v-else name="chevron" :size="12" :class="{ flip: open }" />
      <Icon name="cpu" :size="12" />
      <span class="t">
        {{ thinking ? "pensando" : "razonamiento" }}
      </span>
      <span v-if="thinking" class="dots"><i /><i /><i /></span>
      <span class="grow" />
      <span class="clock mono num">{{ fmt(secs) }}</span>
    </button>
    <div v-if="shown" class="rwin">
      <Blocks :blocks="blocks" />
      <div v-if="thinking" class="tail" />
    </div>
  </div>
</template>
