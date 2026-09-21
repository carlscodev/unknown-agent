<script setup>
/* A file the agent actually wrote on the server, previewed in place: HTML and
   PDF in an iframe, images inline, Markdown rendered, everything else as text.
   Reload refetches (the server sends no-store); open uses a real browser tab. */
import { onMounted, ref } from "vue";
import { Icon } from "../icons.js";
import { renderMarkdown } from "../format.js";
import Blocks from "./Blocks.vue";

const props = defineProps({ artifact: { type: Object, required: true } });

const key = ref(0);
const text = ref("");
const blocks = ref([]);

async function loadText() {
  try {
    const res = await fetch(props.artifact.url, { cache: "no-store" });
    text.value = await res.text();
    blocks.value = renderMarkdown(text.value);
  } catch {
    text.value = "(no se pudo leer el archivo)";
  }
}

onMounted(() => {
  if (props.artifact.kind === "markdown" || props.artifact.kind === "text") loadText();
});

function reload() {
  key.value += 1;
  if (props.artifact.kind === "markdown" || props.artifact.kind === "text") loadText();
}
function open() {
  window.open(props.artifact.url, "_blank", "noopener");
}
function download() {
  window.open(`${props.artifact.url}?download=1`, "_blank", "noopener");
}
function human(n) {
  const v = Number(n || 0);
  if (v < 1024) return `${v} B`;
  if (v < 1024 * 1024) return `${(v / 1024).toFixed(1)} KB`;
  return `${(v / 1024 / 1024).toFixed(1)} MB`;
}
</script>

<template>
  <div class="artifact">
    <div class="abar">
      <Icon name="file" :size="13" />
      <span class="name">{{ artifact.name }}</span>
      <span class="live">escrito por el agente</span>
      <span v-if="artifact.size" class="size mono">{{ human(artifact.size) }}</span>
      <span class="grow" />
      <button title="recargar" @click="reload"><Icon name="refresh" :size="13" /></button>
      <button title="descargar" @click="download"><Icon name="doc" :size="13" /></button>
      <button title="abrir en pestaña" @click="open"><Icon name="eye" :size="13" /></button>
    </div>

    <iframe
      v-if="artifact.kind === 'html'"
      :key="key"
      :src="artifact.url"
      :title="artifact.name"
      class="frame"
      sandbox="allow-scripts allow-same-origin allow-forms allow-modals"
    />
    <iframe v-else-if="artifact.kind === 'pdf'" :key="key" :src="artifact.url" class="frame pdf" />
    <img v-else-if="artifact.kind === 'image'" :key="key" :src="artifact.url" class="img" :alt="artifact.name" />
    <div v-else-if="artifact.kind === 'markdown'" class="md">
      <Blocks :blocks="blocks" />
    </div>
    <pre v-else class="plain mono">{{ text }}</pre>
  </div>
</template>
