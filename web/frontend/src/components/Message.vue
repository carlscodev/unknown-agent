<script setup>
import { computed } from "vue";
import { renderMarkdown } from "../format.js";

const props = defineProps({
  msg: { type: Object, required: true },
});

const blocks = computed(() => renderMarkdown(props.msg.content));

function copy(code) {
  if (navigator.clipboard) navigator.clipboard.writeText(code).catch(() => {});
}
</script>

<template>
  <div class="msg" :class="[msg.role, { command: msg.command, error: msg.error }]">
    <div class="meta">
      <span class="micro">{{ msg.role === "user" ? "you" : msg.error ? "error" : "agent" }}</span>
      <span class="micro" v-if="msg.model">{{ msg.provider }} / {{ msg.model }}</span>
    </div>
    <div class="bubble" :class="{ typing: msg.typing }">
      <template v-for="(b, i) in blocks" :key="i">
        <div v-if="b.kind === 'code'" class="codeblock">
          <div class="bar">
            <span>{{ b.lang }}</span>
            <button @click="copy(b.code)">copy</button>
          </div>
          <pre>{{ b.code }}</pre>
        </div>
        <p v-else v-html="b.html" />
      </template>
    </div>
  </div>
</template>
