<script setup>
import { computed, ref } from "vue";
import { renderMarkdown } from "../format.js";
import { Icon } from "../icons.js";

const props = defineProps({ msg: { type: Object, required: true } });
const blocks = computed(() => renderMarkdown(props.msg.content));

/* Code blocks stay collapsed unless they are short; long dumps expand on
   demand so the transcript stays scannable. */
const COLLAPSE_LINES = 8;
const expanded = ref({});
function isLong(b) {
  return b.code.split("\n").length > COLLAPSE_LINES;
}
function toggle(i) {
  expanded.value = { ...expanded.value, [i]: !expanded.value[i] };
}
function copy(code) {
  if (navigator.clipboard) navigator.clipboard.writeText(code).catch(() => {});
}

/* Artifact preview: iframe served from the agent host. Reload swaps the key
   so the browser refetches the latest content (server sends no-store). */
const previewKey = ref(0);
function reload() {
  previewKey.value += 1;
}
function open() {
  window.open(props.msg.artifact.url, "_blank", "noopener");
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
        <span class="when chip-agent" v-if="msg.mode === 'agent'">full agent</span>
      </div>
      <div v-if="blocks.length || msg.typing" class="bubble" :class="{ typing: msg.typing }">
        <template v-for="(b, i) in blocks" :key="i">
          <div v-if="b.kind === 'code'" class="codeblock" :class="{ collapsed: isLong(b) && !expanded[i] }">
            <div class="bar">
              <span>{{ b.lang }} · {{ b.code.split("\n").length }} lines</span>
              <span class="acts">
                <button v-if="isLong(b)" @click="toggle(i)">
                  {{ expanded[i] ? "collapse" : "expand" }}
                  <Icon name="chevron" :size="12" />
                </button>
                <button @click="copy(b.code)">copy</button>
              </span>
            </div>
            <pre>{{ b.code }}</pre>
          </div>
          <p v-else v-html="b.html" />
        </template>
      </div>

      <div v-if="msg.artifact" class="artifact">
        <div class="abar">
          <Icon :name="msg.artifact.kind === 'image' ? 'image' : 'file'" :size="13" />
          <span class="name">{{ msg.artifact.name }}</span>
          <span class="live">live preview</span>
          <span class="grow" />
          <button
            v-if="msg.artifact.kind === 'html' || msg.artifact.kind === 'image'"
            title="reload preview"
            @click="reload"
          >
            <Icon name="refresh" :size="13" />
          </button>
          <button title="open in new tab" @click="open"><Icon name="eye" :size="13" /></button>
        </div>
        <iframe
          v-if="msg.artifact.kind === 'html'"
          :key="previewKey"
          :src="msg.artifact.url"
          :title="msg.artifact.name"
          class="frame"
          sandbox="allow-scripts allow-same-origin allow-forms"
        />
        <img
          v-else-if="msg.artifact.kind === 'image'"
          :key="previewKey"
          :src="msg.artifact.url"
          :alt="msg.artifact.name"
          class="imgprev"
        />
        <div v-else class="filecard">
          <Icon name="doc" :size="15" />
          <span>{{ msg.artifact.kind === "pdf" ? "PDF document" : "file" }} written by the agent</span>
          <button @click="open">open</button>
        </div>
      </div>
    </div>
  </div>
</template>
