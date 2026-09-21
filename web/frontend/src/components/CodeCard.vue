<script setup>
/* A code block as a small window: fixed height, its own scroll, copy and
   download — plus a live preview when the content can be rendered (HTML, SVG,
   Markdown).  No more walls of code dumped into the transcript. */
import { computed, onBeforeUnmount, ref } from "vue";
import { Icon } from "../icons.js";

const props = defineProps({ block: { type: Object, required: true } });

const PREVIEWABLE = { html: "html", htm: "html", svg: "image", markdown: "markdown", md: "markdown" };

const expanded = ref(false);
const preview = ref(false);
const copied = ref(false);
const lines = computed(() => props.block.code.split("\n").length);
const long = computed(() => lines.value > 14);
const kind = computed(() => PREVIEWABLE[(props.block.lang || "").toLowerCase()] || null);
const src = ref("");

function toggle() {
  expanded.value = !expanded.value;
}

async function copy() {
  try {
    await navigator.clipboard.writeText(props.block.code);
    copied.value = true;
    setTimeout(() => (copied.value = false), 1400);
  } catch {
    /* clipboard blocked */
  }
}

function download() {
  const ext = (props.block.lang || "txt").toLowerCase();
  const name = `snippet.${ext === "markdown" ? "md" : ext}`;
  const url = URL.createObjectURL(new Blob([props.block.code], { type: "text/plain;charset=utf-8" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}

function togglePreview() {
  preview.value = !preview.value;
  if (!preview.value) return;
  const blob = new Blob([props.block.code], {
    type: kind.value === "image" ? "image/svg+xml" : "text/html;charset=utf-8",
  });
  if (src.value) URL.revokeObjectURL(src.value);
  src.value = URL.createObjectURL(blob);
}

onBeforeUnmount(() => {
  if (src.value) URL.revokeObjectURL(src.value);
});
</script>

<template>
  <div class="codeblock" :class="{ collapsed: long && !expanded }">
    <div class="bar">
      <span>{{ block.lang }} · {{ lines }} líneas</span>
      <span class="acts">
        <button v-if="kind" :class="{ on: preview }" title="vista previa en vivo" @click="togglePreview">
          <Icon :name="preview ? 'x' : 'eye'" :size="12" />
          preview
        </button>
        <button v-if="long" @click="toggle">
          {{ expanded ? "colapsar" : "expandir" }}
          <Icon name="chevron" :size="12" :class="{ flip: expanded }" />
        </button>
        <button title="copiar" @click="copy">
          <Icon :name="copied ? 'check' : 'copy'" :size="12" />
          {{ copied ? "copiado" : "copy" }}
        </button>
        <button title="descargar" @click="download"><Icon name="doc" :size="12" /></button>
      </span>
    </div>

    <div v-if="preview && kind === 'html'" class="cpreview">
      <iframe :key="src" :src="src" title="vista previa" sandbox="allow-scripts" />
    </div>
    <div v-else-if="preview && kind === 'image'" class="cpreview image">
      <img :src="src" alt="vista previa" />
    </div>

    <pre v-show="!preview">{{ block.code }}</pre>
  </div>
</template>
