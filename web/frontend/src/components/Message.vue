<script setup>
import { computed } from "vue";
import { renderMarkdown } from "../format.js";
import { Icon } from "../icons.js";

const props = defineProps({ msg: { type: Object, required: true } });
const blocks = computed(() => renderMarkdown(props.msg.content));

function copy(code) {
  if (navigator.clipboard) navigator.clipboard.writeText(code).catch(() => {});
}
</script>

<template>
  <div class="msg" :class="[msg.role, { command: msg.command, error: msg.error }]">
    <div class="ava">
      <Icon :name="msg.role === 'user' ? 'user' : msg.error ? 'shield' : 'logo'" :size="15" />
    </div>
    <div class="bodywrap">
      <div class="meta">
        <span class="who">{{ msg.role === "user" ? "You" : msg.error ? "Error" : "Agent" }}</span>
        <span class="when" v-if="msg.model">{{ msg.provider }} / {{ msg.model }}</span>
        <span class="when" v-else-if="msg.command">command</span>
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
  </div>
</template>
