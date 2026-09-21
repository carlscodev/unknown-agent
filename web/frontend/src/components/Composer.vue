<script setup>
import { computed, nextTick, ref } from "vue";
import { api } from "../api.js";
import { Icon } from "../icons.js";

const props = defineProps({
  commands: { type: Array, default: () => [] },
  busy: { type: Boolean, default: false },
  providers: { type: Array, default: () => [] },
  models: { type: Array, default: () => [] },
  activeProvider: { type: String, default: "" },
  activeModel: { type: String, default: "" },
  isOwner: { type: Boolean, default: false },
  history: { type: Array, default: () => [] },
});
const emit = defineEmits(["send", "stop", "provider", "model", "toast"]);

const text = ref("");
const area = ref(null);
const attachOpen = ref(false);
const attachments = ref([]);
const popIndex = ref(0);
const histIndex = ref(-1);
const draft = ref("");
const fileInputs = ref({});

const ATTACH_KINDS = [
  { key: "image", label: "Image", icon: "image", accept: "image/*" },
  { key: "video", label: "Video", icon: "film", accept: "video/*" },
  { key: "audio", label: "Audio", icon: "wave", accept: "audio/*" },
  { key: "pdf", label: "PDF", icon: "doc", accept: ".pdf,application/pdf" },
  { key: "doc", label: "Document", icon: "file", accept: ".txt,.md,.csv,.json,.log,.py,.js,.ts,.html,.yaml,.toml" },
];

const filtered = computed(() => {
  if (!text.value.startsWith("/")) return [];
  const q = text.value.slice(1).split(" ")[0].toLowerCase();
  return props.commands.filter((c) => c.name.startsWith(q)).slice(0, 7);
});
const popOpen = computed(() => text.value.startsWith("/") && filtered.value.length > 0);

function resize() {
  const el = area.value;
  if (!el) return;
  el.style.height = "auto";
  el.style.height = Math.min(el.scrollHeight, 200) + "px";
}
function focus() {
  nextTick(() => area.value && area.value.focus());
}
function syncPop() {
  if (popIndex.value >= filtered.value.length) popIndex.value = 0;
}

function choose(cmd) {
  text.value = "/" + cmd.name + (["help", "status", "pack", "providers", "models", "clear"].includes(cmd.name) ? "" : " ");
  focus();
}

function pick(kindKey) {
  attachOpen.value = false;
  const input = fileInputs.value[kindKey];
  if (input) input.click();
}

async function onFiles(kindKey, ev) {
  const files = Array.from(ev.target.files || []);
  ev.target.value = "";
  for (const file of files) {
    try {
      const meta = await api.upload(file);
      attachments.value.push(meta);
    } catch (e) {
      emit("toast", e.message);
    }
  }
}

function dropAttach(id) {
  attachments.value = attachments.value.filter((a) => a.id !== id);
}

/* history recall with draft restore */
function recall(delta) {
  const h = props.history;
  if (!h.length) return;
  if (histIndex.value === -1) {
    if (delta > 0) return;
    draft.value = text.value;
    histIndex.value = h.length - 1;
  } else {
    histIndex.value += delta;
    if (histIndex.value >= h.length) {
      histIndex.value = -1;
      text.value = draft.value;
      resize();
      return;
    }
    if (histIndex.value < 0) histIndex.value = 0;
  }
  text.value = h[histIndex.value];
  resize();
}

function submit() {
  if (popOpen.value && filtered.value[popIndex.value]) {
    // While the name is still partial, Enter completes it; an exact match runs.
    const typed = text.value.slice(1).split(" ")[0].toLowerCase();
    const cand = filtered.value[popIndex.value];
    if (typed !== cand.name) {
      choose(cand);
      return;
    }
  }
  const value = text.value.trim();
  if (!value || props.busy) return;
  const ids = attachments.value.map((a) => a.id);
  emit("send", value, ids);
  text.value = "";
  attachments.value = [];
  histIndex.value = -1;
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
    if (e.key === "ArrowDown") { e.preventDefault(); popIndex.value = (popIndex.value + 1) % filtered.value.length; return; }
    if (e.key === "ArrowUp") { e.preventDefault(); popIndex.value = (popIndex.value - 1 + filtered.value.length) % filtered.value.length; return; }
    if (e.key === "Tab" || e.key === "Enter") { e.preventDefault(); choose(filtered.value[popIndex.value]); return; }
    if (e.key === "Escape") { e.preventDefault(); text.value = ""; return; }
  } else if (e.key === "ArrowUp" && !e.shiftKey && area.value && area.value.selectionStart === 0) {
    e.preventDefault();
    recall(-1);
    return;
  } else if (e.key === "ArrowDown" && !e.shiftKey && histIndex.value !== -1) {
    e.preventDefault();
    recall(1);
    return;
  } else if (e.key === "Escape") {
    e.preventDefault();
    text.value = "";
    histIndex.value = -1;
    resize();
    return;
  }
  nextTick(syncPop);
}

defineExpose({ focus });
</script>

<template>
  <div class="composer">
    <!-- hidden file inputs, one per attach kind -->
    <input
      v-for="k in ATTACH_KINDS"
      :key="k.key"
      :ref="(el) => (fileInputs[k.key] = el)"
      type="file"
      :accept="k.accept"
      multiple
      style="display: none"
      @change="onFiles(k.key, $event)"
    />

    <div v-if="popOpen" class="pop slash">
      <button
        v-for="(c, i) in filtered"
        :key="c.name"
        class="item"
        :class="{ sel: i === popIndex }"
        @mousedown.prevent="choose(c)"
      >
        <span class="u">{{ c.usage }}</span>
        <span>{{ c.description }}</span>
      </button>
    </div>

    <div v-if="attachOpen" class="pop">
      <button v-for="k in ATTACH_KINDS" :key="k.key" class="item" @click="pick(k.key)">
        <Icon :name="k.icon" />
        {{ k.label }}
      </button>
    </div>

    <div v-if="attachments.length" class="attach-row">
      <span v-for="a in attachments" :key="a.id" class="chip">
        <Icon :name="a.kind === 'image' ? 'image' : a.kind === 'video' ? 'film' : a.kind === 'audio' ? 'wave' : a.kind === 'pdf' ? 'doc' : 'file'" />
        {{ a.name }}
        <span style="color: var(--t4)">{{ a.size_human }}</span>
        <button aria-label="remove attachment" @click="dropAttach(a.id)"><Icon name="x" :size="12" /></button>
      </span>
    </div>

    <textarea
      ref="area"
      v-model="text"
      rows="1"
      placeholder="Message the agent — / for commands"
      spellcheck="false"
      @keydown="onKey"
      @input="() => { syncPop(); resize(); }"
      @blur="attachOpen = false"
    />

    <div class="ctl-bar">
      <button
        class="icon-btn"
        aria-label="attach files"
        title="Attach image, video, audio, PDF or document"
        @click="attachOpen = !attachOpen"
      >
        <Icon name="clip" />
      </button>

      <span class="sel">
        <select
          :value="activeProvider"
          :disabled="!isOwner"
          aria-label="provider"
          @change="$emit('provider', $event.target.value)"
        >
          <option v-for="p in providers" :key="p.name" :value="p.name">{{ p.name }}</option>
        </select>
      </span>
      <span class="sel">
        <select
          :value="activeModel"
          :disabled="!isOwner"
          aria-label="model"
          @change="$emit('model', $event.target.value)"
        >
          <option v-for="m in models" :key="m" :value="m">{{ m }}</option>
        </select>
      </span>

      <span class="grow" />

      <button v-if="busy" class="stopbtn" @click="$emit('stop')">
        <Icon name="stop" />
        <span class="lbl">Stop</span>
      </button>
      <button class="send" :disabled="!text.trim() || busy" @click="submit">
        <Icon name="send" />
        <span class="lbl">Send</span>
      </button>
    </div>
  </div>

  <div class="hints">
    <span><kbd>enter</kbd> send</span>
    <span><kbd>shift enter</kbd> newline</span>
    <span><kbd>up</kbd> history</span>
    <span><kbd>esc</kbd> clear</span>
    <span><kbd>/</kbd> commands</span>
  </div>
</template>
