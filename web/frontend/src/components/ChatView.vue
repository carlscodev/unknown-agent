<script setup>
import { nextTick, onBeforeUnmount, onMounted, ref } from "vue";
import { api, chatSocket } from "../api.js";
import Composer from "./Composer.vue";
import Message from "./Message.vue";
import RunStatus from "./RunStatus.vue";

const props = defineProps({ session: { type: Object, required: true } });
const emit = defineEmits(["toast"]);

const isOwner = props.session.role === "owner";
const status = ref({ providers: [], active: "", model: "", version: "", files: {}, tools: [] });
const models = ref([]);
const commands = ref([]);
const messages = ref([]);
const sent = ref([]);
const busy = ref(false);
const live = ref(false);
const reconnecting = ref(false);
const transcript = ref(null);
const agentMode = ref("auto"); // "auto" (herramientas) | "on" (agente completo)
const pendingMode = ref(""); // "tools" | "agent" — why this message ran the way it did

/* run state: phase + live timers */
const phase = ref(""); // "" | thinking | generating | tools
const thinkSecs = ref(0);
const genSecs = ref(0);
const toolCount = ref(0);
const fileCount = ref(0);
const lastTiming = ref(null);

let ws = null;
let cur = null; // assistant message being built
let thinkT0 = 0;
let genT0 = 0;
let tickId = 0;
let retry = 0;
let retryId = 0;

defineExpose({ live });

/* ------------------------------------------------------------------ timers */
function startTicking() {
  clearInterval(tickId);
  tickId = setInterval(() => {
    const now = performance.now();
    if (phase.value === "thinking" && thinkT0) thinkSecs.value = (now - thinkT0) / 1000;
    else if (genT0) genSecs.value = (now - genT0) / 1000;
  }, 100);
}
function stopTicking() {
  clearInterval(tickId);
  tickId = 0;
}
function beginThinking() {
  phase.value = "thinking";
  thinkT0 = performance.now();
  startTicking();
}
function beginGenerating() {
  if (genT0) return;
  phase.value = "generating";
  if (thinkT0) thinkSecs.value = (performance.now() - thinkT0) / 1000;
  genT0 = performance.now();
  startTicking();
}
function finishTimers() {
  const now = performance.now();
  if (!genT0 && thinkT0) thinkSecs.value = (now - thinkT0) / 1000;
  if (genT0) genSecs.value = (now - genT0) / 1000;
  stopTicking();
}

/* --------------------------------------------------------------- segments */
function pushSeg(seg) {
  if (!cur) return null;
  cur.segs.push(seg);
  return seg;
}
function lastSeg(kind) {
  if (!cur) return null;
  for (let i = cur.segs.length - 1; i >= 0; i--) {
    if (cur.segs[i].kind === kind) return cur.segs[i];
  }
  return null;
}
function textSeg() {
  let seg = null;
  if (cur) {
    const last = cur.segs[cur.segs.length - 1];
    if (last && last.kind === "text") seg = last;
  }
  if (!seg) seg = pushSeg({ kind: "text", text: "" });
  return seg;
}
function reasoningSeg() {
  let seg = lastSeg("reasoning");
  if (!seg || !seg.thinking) {
    seg = pushSeg({ kind: "reasoning", text: "", thinking: true, secs: 0 });
    seg.t0 = performance.now();
  }
  return seg;
}
function closeReasoning() {
  const seg = lastSeg("reasoning");
  if (seg && seg.thinking) {
    seg.thinking = false;
    seg.secs = seg.t0 ? (performance.now() - seg.t0) / 1000 : 0;
  }
}
function toolSeg(id) {
  if (!cur) return null;
  for (let i = cur.segs.length - 1; i >= 0; i--) {
    const s = cur.segs[i];
    if (s.kind === "tool" && s.item.id === id) return s;
  }
  return null;
}

/* ------------------------------------------------------------ event intake */
function applyWs(msg) {
  switch (msg.type) {
    case "mode":
      // why this message ran the way it did: tools loop or full agent
      pendingMode.value = msg.mode || "";
      if (cur) cur.mode = pendingMode.value;
      break;
    case "tools":
      status.value.tools = msg.tools || [];
      break;
    case "phase":
      if (msg.phase === "thinking") beginThinking();
      else if (msg.phase === "generating") {
        closeReasoning();
        beginGenerating();
      } else if (msg.phase === "tools") phase.value = "tools";
      break;
    case "reasoning": {
      const seg = reasoningSeg();
      seg.text += msg.delta || "";
      break;
    }
    case "delta": {
      beginGenerating();
      const seg = textSeg();
      seg.text += msg.delta || "";
      if (cur) cur.content += msg.delta || "";
      break;
    }
    case "tool": {
      if (!cur) break;
      if (msg.status === "start") {
        pushSeg({ kind: "tool", item: { ...msg } });
        toolCount.value += 1;
      } else {
        const seg = toolSeg(msg.id);
        if (seg) seg.item = { ...seg.item, ...msg };
        else pushSeg({ kind: "tool", item: { ...msg } });
      }
      break;
    }
    case "artifact":
      if (!cur) break;
      pushSeg({ kind: "artifact", artifact: msg });
      fileCount.value += 1;
      break;
    case "thought":
      // full-Runner mode (bolt): internal narration
      if (cur) {
        const seg = reasoningSeg();
        seg.text += (seg.text ? "\n" : "") + (msg.text || "");
      }
      break;
    case "verify":
      if (cur) {
        pushSeg({
          kind: "tool",
          item: { name: "verificación", status: "end", ok: msg.ok, duration_ms: 0 },
        });
      }
      break;
    case "message":
      messages.value.push({
        role: "assistant",
        content: msg.content,
        command: true,
        mode: pendingMode.value,
      });
      break;
    case "done":
      if (cur) {
        closeReasoning();
        cur.typing = false;
        cur.model = msg.model;
        cur.provider = msg.provider;
      }
      lastTiming.value = msg.timing || null;
      finishRun();
      break;
    case "stopped":
      if (cur) {
        closeReasoning();
        cur.typing = false;
        if (!cur.segs.some((s) => s.kind === "text" && s.text.trim())) textSeg().text = "(detenido)";
      }
      finishRun();
      break;
    case "error":
      if (cur) {
        closeReasoning();
        cur.typing = false;
      }
      messages.value.push({ role: "assistant", content: msg.error, error: true });
      finishRun();
      break;
  }
  scrollDown();
}

function finishRun() {
  busy.value = false;
  finishTimers();
  cur = null;
  setTimeout(() => {
    if (!busy.value) phase.value = "";
  }, 900);
}

function scrollDown() {
  nextTick(() => {
    const el = transcript.value;
    if (el) el.scrollTop = el.scrollHeight;
  });
}

/* --------------------------------------------------------------- transport */
function connect() {
  try {
    ws = chatSocket();
  } catch {
    ws = null;
    return;
  }
  ws.onopen = () => {
    live.value = true;
    reconnecting.value = false;
    retry = 0;
  };
  ws.onmessage = (ev) => {
    try {
      applyWs(JSON.parse(ev.data));
    } catch {
      /* ignore */
    }
  };
  ws.onerror = () => {
    live.value = false;
  };
  ws.onclose = () => {
    live.value = false;
    ws = null;
    // exponential backoff: a dropped socket must never need a manual refresh
    retry = Math.min(retry + 1, 6);
    const delay = Math.min(1000 * 2 ** retry, 15000);
    reconnecting.value = true;
    clearTimeout(retryId);
    retryId = setTimeout(connect, delay);
  };
}

async function send(text, attachIds) {
  messages.value.push({ role: "user", content: text });
  sent.value.push(text);
  cur = { role: "assistant", content: "", segs: [], typing: true, mode: pendingMode.value };
  messages.value.push(cur);
  busy.value = true;
  thinkSecs.value = 0;
  genSecs.value = 0;
  toolCount.value = 0;
  fileCount.value = 0;
  lastTiming.value = null;
  thinkT0 = performance.now();
  genT0 = 0;
  phase.value = "thinking";
  startTicking();
  scrollDown();

  const agent = agentMode.value === "on" ? true : null;
  // per-message provider/model: the composer's current pair travels with the
  // message instead of mutating the server's global active profile.
  const runProvider = status.value.active || null;
  const runModel = status.value.model || null;

  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(
      JSON.stringify({ text, attachments: attachIds, agent, provider: runProvider, model: runModel }),
    );
    return;
  }
  // no socket: fall back to the REST endpoint and replay its payload as events
  try {
    const r = await api.chat(text, attachIds, agent, runProvider, runModel);
    for (const d of r.deltas || []) applyWs({ type: "delta", delta: d });
    for (const a of r.artifacts || []) applyWs({ type: "artifact", ...a });
    applyWs({
      type: "done",
      content: r.content,
      timing: r.timing,
      model: r.model,
      provider: r.provider,
    });
  } catch (e) {
    applyWs({ type: "error", error: e.message });
  }
}

function stop() {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "stop" }));
  api.stop().catch(() => {});
}

async function refresh() {
  try {
    status.value = await api.status();
    commands.value = await api.commands();
    models.value = status.value.model ? [status.value.model] : [];
    const m = await api.models();
    models.value = [...new Set([...models.value, ...(m.models || [])])];
  } catch {
    /* session died */
  }
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
  stopTicking();
  clearTimeout(retryId);
  if (ws) ws.close();
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
              'Conectado. Este agente tiene herramientas reales en el servidor: lee, escribe, busca y ejecuta dentro de ' +
              (status.cwd || 'el directorio de trabajo') +
              '. Pídele una landing page y la va a escribir de verdad, con vista previa en vivo. Escribe `/help` para los comandos.',
          }"
        />
        <Message v-for="(m, i) in messages" :key="i" :msg="m" />
      </div>
    </div>

    <div class="composer-zone">
      <RunStatus
        :active="busy"
        :phase="phase"
        :think-secs="thinkSecs"
        :gen-secs="genSecs"
        :tools="toolCount"
        :files="fileCount"
        :timing="lastTiming"
        :tool-total="(status.tools || []).length"
        :live="live"
        :reconnecting="reconnecting"
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
