<script setup>
import { ref } from "vue";
import UiIcon from "./UiIcon.vue";
const props = defineProps({ command: { type: String, required: true }, label: { type: String, default: "terminal" } });
const copied = ref(false);
async function copy() {
  await navigator.clipboard.writeText(props.command);
  copied.value = true;
  setTimeout(() => (copied.value = false), 1600);
}
</script>
<template>
  <div class="code-window">
    <div class="code-bar"><span><i></i><i></i><i></i></span><b>{{ label }}</b><button @click="copy" :aria-label="copied ? 'Copied' : 'Copy command'"><UiIcon :name="copied ? 'check' : 'copy'" :size="15" /> {{ copied ? "Copied" : "Copy" }}</button></div>
    <pre><span>$</span> {{ command }}</pre>
  </div>
</template>
