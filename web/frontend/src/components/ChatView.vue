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
const agentMode = ref("auto"); // "auto" | "on"
let ws = null;
let reconnectTimer = 0;
let reconnectDelay = 1000;
let closed = false;

/* internal-activity window + real phase timers */
const activity = ref([]);
const phase = ref("");
const thinkSecs = ref(0);
const genSecs = ref(0);
let t0 = 0;
let genT = 0;
let tickId = 0;

defineExpose({ live });

function startTimers() {
  t0 = performance.now();
  genT = 0;
  thinkSecs.value = 0;
  genSecs.value = 0;
  clearInterval(tickId);
  tickId = setInterval(() => {
    const now = performance.now();
    thinkSecs.value = ((genT || now) - t0) / 1000;
    genSecs.value = genT ? (now - genT) / 1000 : 0;
  }, 100);
}
function stopTimers() {
  clearInterval(tickId);
  const now = performance.now();
  thinkSecs.value = ((genT || now) - t0) / 1000;
  genSecs.value = genT ? (now - genT) / 1000 : 0;
}

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

const pendingMode = ref("");

function lastTyping() {
  for (let i = messages.value.length - 1; i >= 0; i--) {
    if (messages.value[i].typing) return messages.value[i];
  }
  return null;
}

function applyWs(msg) {
  if (msg.type === "mode") {
    pendingMode.value = msg.mode;
  } else if (msg.type === "phase") {
    phase.value = "thinking";
  } else if (msg.type === "thought") {
    activity.value.push({ kind: "thought", text: msg.text });
  } else if (msg.type === "tool") {
    if (msg.status === "start") {
      activity.value.push({ kind: "tool", text: `${msg.name} running…` });
    } else {
      activity.value.push({
        kind: "tool",
        text: `${msg.name}${msg.summary ? " — " + msg.summary : ""}${msg.ok ? "" : " (failed)"}`,
      });
    }
  } else if (msg.type === "verify") {
    activity.value.push({
      kind: "verify",
      text: msg.ok ? "verification passed" : "verification failed",
    });
  } else if (msg.type === "delta") {
    if (!genT) {
      phase.value = "generating";
      genT = performance.now();
    }
    streamText.value += msg.delta;
    let last = messages.value[messages.value.length - 1];
    if (!last || last.role !== "assistant" || !last.typing) {
      last = { role: "assistant", content: "", typing: true, mode: pendingMode.value };
      messages.value.push(last);
    }
    last.content += msg.delta;
  } else if (msg.type === "artifact") {
    messages.value.push({ role: "assistant", content: "", artifact: msg });
  } else if (msg.type === "message") {
    messages.value.push({
      role: "assistant",
      content: msg.content,
      command: true,
      mode: pendingMode.value,
    });
  } else if (msg.type === "done") {
    const last = lastTyping();
    if (last) {
      last.typing = false;
      last.model = msg.model;
      last.provider = msg.provider;
    }
    finishRun();
  } else if (msg.type === "stopped") {
    const last = lastTyping();
    if (last) {
      last.typing = false;
      last.content = (last.content || "") + "\n\n[stopped]";
    }
    finishRun();
  } else if (msg.type === "error") {
    const last = lastTyping();
    if (last) last.typing = false;
    messages.value.push({ role: "assistant", content: msg.error, error: true });
    finishRun();
  }
  scrollDown();
}

function finishRun() {
  busy.value = false;
  stopTimers();
  // Let the activity window linger so the run reads as intentional, then clear.
  setTimeout(() => {
    streamText.value = "";
    activity.value = [];
    phase.value = "";
  }, 1800);
}

function connect() {
  try {
    ws = chatSocket();
  } catch {
    ws = null;
    return;
  }
  ws.onopen = () => {
    live.value = true;
    reconnectDelay = 1000;
  };
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
    if (!closed) {
      reconnectTimer = setTimeout(connect, reconnectDelay);
      reconnectDelay = Math.min(reconnectDelay * 2, 15000);
    }
  };
}

async function send(text, attachIds) {
  messages.value.push({ role: "user", content: text });
  sent.value.push(text);
  busy.value = true;
  streamText.value = "";
  activity.value = [];
  phase.value = "thinking";
  startTimers();
  scrollDown();
  const agent = agentMode.value === "on" ? true : null;
  const runProvider = status.value.active || null;
  const runModel = status.value.model || null;
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(
      JSON.stringify({
        text,
        attachments: attachIds,
        agent,
        provider: runProvider,
        model: runModel,
      })
    );
    return;
  }
  try {
    const r = await api.chat(text, attachIds, agent, runProvider, runModel);
    applyWs({ type: "message", content: r.content || r.deltas.join("") });
    for (const a of r.artifacts || []) applyWs({ type: "artifact", ...a });
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
onBeforeUnmount(() => {
  clearInterval(tickId);
  closed = true;
  clearTimeout(reconnectTimer);
  ws && ws.close();
});
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
              'Connected. Send a message, attach files from the clip menu, or type `/help` for the command palette. Ask it to create a file or a page and it runs the full agent with real tools on the server.',
          }"
        />
        <Message v-for="(m, i) in messages" :key="i" :msg="m" />
      </div>
    </div>

    <div class="composer-zone">
      <StreamMonitor
        :active="busy"
        :activity="activity"
        :phase="phase"
        :think-secs="thinkSecs"
        :gen-secs="genSecs"
        :text="streamText"
      />
      <Composer
        :commands="commands"
        :busy="busy"
        :providers="status.providers"
        :models="models"
        :active-provider="status.active"
        :active-model="status.model"
        :is-owner="isOwner"
        :history="sent"
        :agent-mode="agentMode"
        @send="send"
        @stop="stop"
        @provider="pickProvider"
        @model="pickModel"
        @agentmode="(m) => (agentMode = m)"
        @toast="(t) => emit('toast', t)"
      />
    </div>
  </div>
</template>
