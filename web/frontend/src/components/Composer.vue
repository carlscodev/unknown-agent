<script setup>
import { computed, nextTick, ref } from "vue";

const props = defineProps({
  commands: { type: Array, default: () => [] },
  disabled: { type: Boolean, default: false },
  history: { type: Array, default: () => [] }, // oldest..newest sent prompts
});
const emit = defineEmits(["send"]);

const text = ref("");
const area = ref(null);
const popOpen = ref(false);
const popIndex = ref(0);
const histIndex = ref(-1); // -1 = live text
const draft = ref("");

const filtered = computed(() => {
  const raw = text.value;
  if (!raw.startsWith("/")) return [];
  const q = raw.slice(1).split(" ")[0].toLowerCase();
  return props.commands.filter((c) => c.name.startsWith(q)).slice(0, 7);
});

function syncPop() {
  popOpen.value = text.value.startsWith("/") && filtered.value.length > 0;
  if (popIndex.value >= filtered.value.length) popIndex.value = 0;
}

function resize() {
  const el = area.value;
  if (!el) return;
  el.style.height = "auto";
  el.style.height = Math.min(el.scrollHeight, 180) + "px";
}

function choose(cmd) {
  text.value = "/" + cmd.name + (cmd.name === "help" ? "" : " ");
  popOpen.value = false;
  focus();
}

function focus() {
  nextTick(() => area.value && area.value.focus());
}

function recall(delta) {
  const h = props.history;
  if (!h.length) return;
  if (histIndex.value === -1) {
    draft.value = text.value;
    histIndex.value = h.length - (delta < 0 ? 1 : 0);
    if (delta > 0) {
      histIndex.value = -1;
      return;
    }
  } else {
    histIndex.value += delta;
    if (histIndex.value >= h.length) {
      histIndex.value = -1;
      text.value = draft.value;
      syncPop();
      resize();
      return;
    }
    if (histIndex.value < 0) histIndex.value = 0;
  }
  text.value = h[histIndex.value];
  popOpen.value = false;
  resize();
}

function clearLine() {
  if (popOpen.value) {
    popOpen.value = false;
    return;
  }
  text.value = "";
  histIndex.value = -1;
  resize();
}

function submit() {
  if (popOpen.value && filtered.value[popIndex.value]) {
    choose(filtered.value[popIndex.value]);
    return;
  }
  const value = text.value.trim();
  if (!value || props.disabled) return;
  emit("send", value);
  text.value = "";
  histIndex.value = -1;
  popOpen.value = false;
  resize();
  focus();
}

function onKey(e) {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    submit();
    return;
  }
  if (popOpen.value) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      popIndex.value = (popIndex.value + 1) % filtered.value.length;
      return;
    }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      popIndex.value = (popIndex.value - 1 + filtered.value.length) % filtered.value.length;
      return;
    }
    if (e.key === "Tab") {
      e.preventDefault();
      choose(filtered.value[popIndex.value]);
      return;
    }
    if (e.key === "Escape") {
      e.preventDefault();
      popOpen.value = false;
      return;
    }
  } else if (e.key === "ArrowUp" && !e.shiftKey) {
    const el = area.value;
    if (el && el.selectionStart === 0) {
      e.preventDefault();
      recall(-1);
      return;
    }
  } else if (e.key === "ArrowDown" && !e.shiftKey) {
    if (histIndex.value !== -1) {
      e.preventDefault();
      recall(1);
      return;
    }
  } else if (e.key === "Escape") {
    e.preventDefault();
    clearLine();
    return;
  }
  nextTick(syncPop);
}
</script>

<template>
  <div class="composer">
    <div v-if="popOpen" class="slash-pop">
      <div
        v-for="(c, i) in filtered"
        :key="c.name"
        class="slash-item"
        :class="{ sel: i === popIndex }"
        @mousedown.prevent="choose(c)"
      >
        <span class="mono">{{ c.usage }}</span>
        <span>{{ c.description }}</span>
      </div>
    </div>
    <textarea
      ref="area"
      v-model="text"
      rows="1"
      :disabled="disabled"
      placeholder="Message the agent, or / for commands"
      spellcheck="false"
      @keydown="onKey"
      @input="() => { syncPop(); resize(); }"
    />
    <div class="composer-bar">
      <span class="hint">
        <span><kbd>enter</kbd> send</span>
        <span><kbd>shift+enter</kbd> newline</span>
        <span><kbd>&#8593;</kbd><kbd>&#8595;</kbd> history</span>
        <span><kbd>esc</kbd> clear</span>
      </span>
      <span class="grow" />
      <span class="micro">{{ text.length ? text.length : "" }}</span>
    </div>
  </div>
</template>
