<script setup>
import { nextTick, onBeforeUnmount, onMounted, ref } from "vue";
import { api, chatSocket } from "../api.js";
import Composer from "./Composer.vue";
import Message from "./Message.vue";
import StreamMonitor from "./StreamMonitor.vue";

const props = defineProps({ session: { type: Object, required: true } });
const emit = defineEmits(["toast"]);

const isOwner = props.session.role === "owner";
const status = ref({ providers: [], active: "", model: "", version: "", files: {} });
const models = ref([]);
const commands = ref([]);
const messages = ref([]);
const sent = ref([]);
const busy = ref(false);
const streamText = ref("");
const live = ref(false);
const transcript = ref(null);
let ws = null;

defineExpose({ live });

async function refresh() {
  try {
    status.value = await api.status();
    commands.value = await api.commands();
    // The select must never render empty: seed with the active model, then
    // merge the live list once the gateway answers.
    models.value = status.value.model ? [status.value.model] : [];
    const m = await api.models();
    const live = m.models || [];
    models.value = [...new Set([...models.value, ...live])];
  } catch {
    /* session died */
  }
}

function scrollDown() {
  nextTick(() => {
    const el = transcript.value;
    if (el) el.scrollTop = el.scrollHeight;
  });
}

function applyWs(msg) {
  if (msg.type === "delta") {
    streamText.value += msg.delta;
    let last = messages.value[messages.value.length - 1];
    if (!last || last.role !== "assistant" || !last.typing) {
      last = { role: "assistant", content: "", typing: true };
      messages.value.push(last);
    }
    last.content += msg.delta;
  } else if (msg.type === "message") {
    messages.value.push({ role: "assistant", content: msg.content, command: true });
  } else if (msg.type === "done") {
    const last = messages.value[messages.value.length - 1];
    if (last && last.typing) {
      last.typing = false;
      last.model = msg.model;
      last.provider = msg.provider;
    }
    busy.value = false;
    // Let the rolling window linger so the generation reads as intentional,
    // then fade it out.
    setTimeout(() => (streamText.value = ""), 1500);
  } else if (msg.type === "stopped") {
    const last = messages.value[messages.value.length - 1];
    if (last && last.typing) {
      last.typing = false;
      last.content = (last.content || "") + "\n\n[stopped]";
    }
    busy.value = false;
    streamText.value = "";
  } else if (msg.type === "error") {
    const last = messages.value[messages.value.length - 1];
    if (last && last.typing) last.typing = false;
    messages.value.push({ role: "assistant", content: msg.error, error: true });
    busy.value = false;
    streamText.value = "";
  }
  scrollDown();
}

function connect() {
  try {
    ws = chatSocket();
  } catch {
    ws = null;
    return;
  }
  ws.onopen = () => (live.value = true);
  ws.onmessage = (ev) => {
    try {
      applyWs(JSON.parse(ev.data));
    } catch {
      /* ignore */
    }
  };
  ws.onclose = () => {
    live.value = false;
    ws = null;
  };
}

async function send(text, attachIds) {
  messages.value.push({ role: "user", content: text });
  sent.value.push(text);
  busy.value = true;
  streamText.value = "";
  scrollDown();
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ text, attachments: attachIds }));
    return;
  }
  try {
    const r = await api.chat(text, attachIds);
    applyWs({ type: "message", content: r.content || r.deltas.join("") });
    applyWs({ type: "done" });
  } catch (e) {
    applyWs({ type: "error", error: e.message });
  }
}

function stop() {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "stop" }));
  api.stop().catch(() => {});
}

async function pickProvider(name) {
  try {
    status.value = { ...status.value, ...(await api.setProvider(name)) };
    models.value = status.value.model ? [status.value.model] : [];
    const m = await api.models();
    models.value = [...new Set([...models.value, ...(m.models || [])])];
  } catch (e) {
    emit("toast", e.message);
  }
}
async function pickModel(id) {
  try {
    status.value = { ...status.value, ...(await api.setModel(id)) };
  } catch (e) {
    emit("toast", e.message);
  }
}

onMounted(() => {
  refresh();
  connect();
});
onBeforeUnmount(() => ws && ws.close());
</script>

<template>
  <div class="view">
    <div ref="transcript" class="transcript">
      <div class="col">
        <Message
          v-if="!messages.length"
          :msg="{
            role: 'assistant',
            content:
              'Connected. Send a message, attach files from the clip menu, or type `/help` for the command palette. Provider and model switch from the input bar.',
          }"
        />
        <Message v-for="(m, i) in messages" :key="i" :msg="m" />
      </div>
    </div>

    <div class="composer-zone">
      <StreamMonitor v-if="streamText" :text="streamText" />
      <Composer
        :commands="commands"
        :busy="busy"
        :providers="status.providers"
        :models="models"
        :active-provider="status.active"
        :active-model="status.model"
        :is-owner="isOwner"
        :history="sent"
        @send="send"
        @stop="stop"
        @provider="pickProvider"
        @model="pickModel"
        @toast="(t) => emit('toast', t)"
      />
    </div>
  </div>
</template>
