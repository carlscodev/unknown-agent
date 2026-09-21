<script setup>
import { nextTick, onBeforeUnmount, onMounted, ref } from "vue";
import { api, chatSocket } from "../api.js";
import Composer from "./Composer.vue";
import Message from "./Message.vue";

const props = defineProps({ session: { type: Object, required: true } });
const emit = defineEmits(["logout"]);

const isOwner = props.session.role === "owner";
const status = ref({ providers: [], active: "", model: "", version: "" });
const models = ref([]);
const commands = ref([]);
const devices = ref({ pending: {}, devices: {} });
const messages = ref([]);
const sent = ref([]);
const busy = ref(false);
const live = ref(false);
const menuOpen = ref(false);
const modal = ref(null); // {kind: 'token', token}
const transcript = ref(null);

let ws = null;

async function refresh() {
  try {
    status.value = await api.status();
    commands.value = await api.commands();
    if (isOwner) {
      devices.value = await api.devices();
    }
    const m = await api.models();
    models.value = m.models || [];
    if (status.value.model && !models.value.includes(status.value.model)) {
      models.value = [status.value.model, ...models.value];
    }
  } catch {
    /* session may have died elsewhere */
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
    let last = messages.value[messages.value.length - 1];
    if (!last || last.role !== "assistant" || !last.typing) {
      last = { role: "assistant", content: "", typing: true };
      messages.value.push(last);
    }
    last.content += msg.delta;
  } else if (msg.type === "message") {
    messages.value.push({
      role: "assistant",
      content: msg.content,
      command: true,
      typing: false,
    });
  } else if (msg.type === "done") {
    const last = messages.value[messages.value.length - 1];
    if (last && last.typing) {
      last.typing = false;
      last.model = msg.model;
      last.provider = msg.provider;
    }
    busy.value = false;
  } else if (msg.type === "error") {
    const last = messages.value[messages.value.length - 1];
    if (last && last.typing) last.typing = false;
    messages.value.push({ role: "assistant", content: msg.error, error: true });
    busy.value = false;
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
  ws.onopen = () => {
    live.value = true;
  };
  ws.onmessage = (ev) => {
    try {
      applyWs(JSON.parse(ev.data));
    } catch {
      /* ignore malformed frames */
    }
  };
  ws.onclose = () => {
    live.value = false;
    ws = null;
  };
}

async function send(text) {
  messages.value.push({ role: "user", content: text });
  sent.value.push(text);
  busy.value = true;
  scrollDown();
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ text }));
    return;
  }
  try {
    const r = await api.chat(text);
    applyWs({ type: "message", content: r.content || r.deltas.join("") });
    applyWs({ type: "done" });
  } catch (e) {
    applyWs({ type: "error", error: e.message });
  }
}

async function pickProvider(name) {
  status.value = await api.setProvider(name);
}
async function pickModel(id) {
  status.value = await api.setModel(id);
}
async function revoke(code) {
  await api.revokeDevice(code);
  devices.value = await api.devices();
}
async function rotate() {
  const r = await api.rotateToken();
  modal.value = { kind: "token", token: r.token };
}
async function logout() {
  await api.logout();
  emit("logout");
}

onMounted(() => {
  refresh();
  connect();
});
onBeforeUnmount(() => ws && ws.close());
</script>

<template>
  <div class="shell" :class="{ 'menu-open': menuOpen }">
    <aside class="sidebar">
      <div>
        <div class="wordmark">Unknown<span>&nbsp;Agent</span></div>
        <span class="micro">Control panel</span>
      </div>

      <div class="side-block">
        <span class="micro">Session</span>
        <select
          :value="status.active"
          :disabled="!isOwner"
          title="Active provider profile"
          @change="pickProvider($event.target.value)"
        >
          <option v-for="p in status.providers" :key="p.name" :value="p.name">
            {{ p.name }}
          </option>
        </select>
        <select
          :value="status.model"
          :disabled="!isOwner"
          title="Active model"
          @change="pickModel($event.target.value)"
        >
          <option v-for="m in models" :key="m" :value="m">{{ m }}</option>
        </select>
      </div>

      <div class="side-block">
        <span class="micro">Status</span>
        <div class="side-row"><span>version</span><b>{{ status.version }}</b></div>
        <div class="side-row"><span>provider</span><b>{{ status.active }}</b></div>
        <div class="side-row"><span>model</span><b>{{ status.model || "unset" }}</b></div>
        <div class="side-row"><span>role</span><b>{{ props.session.role }}</b></div>
      </div>

      <div v-if="isOwner" class="side-block">
        <span class="micro">Devices</span>
        <div v-if="Object.keys(devices.pending).length" class="side-row">
          <span>pending</span>
          <b>{{ Object.keys(devices.pending).join(", ") }}</b>
        </div>
        <div v-for="(meta, code) in devices.devices" :key="code" class="device-item">
          <span class="mono">{{ code }}</span>
          <span style="flex: 1; overflow: hidden; text-overflow: ellipsis">
            {{ meta.name || meta.label || "" }}
          </span>
          <button class="danger" @click="revoke(code)">revoke</button>
        </div>
        <div v-if="!Object.keys(devices.devices).length" class="side-row">
          <span>no paired devices</span>
        </div>
      </div>

      <div class="side-foot">
        <button v-if="isOwner" @click="rotate">Rotate access token</button>
        <button @click="logout">Sign out</button>
      </div>
    </aside>

    <main class="main">
      <div class="topbar">
        <button class="menu-btn" @click="menuOpen = !menuOpen">menu</button>
        <div class="who">
          <b>{{ status.active || "no provider" }}</b>
          <span>{{ status.model || "run ua setup" }}</span>
        </div>
        <span class="spacer" />
        <span class="dot" :class="{ off: !live }" :title="live ? 'live' : 'offline'"></span>
        <span class="micro">{{ live ? "live" : "reconnecting" }}</span>
      </div>

      <div ref="transcript" class="transcript">
        <div v-if="!messages.length" class="msg assistant">
          <div class="bubble">
            <p>
              Connected. Type a message, or <code>/help</code> for what this panel
              understands. The active provider and model follow the sidebar.
            </p>
          </div>
        </div>
        <Message v-for="(m, i) in messages" :key="i" :msg="m" />
      </div>

      <div class="composer-zone">
        <Composer :commands="commands" :history="sent" :disabled="busy" @send="send" />
      </div>
    </main>

    <div v-if="modal" class="modal-scrim" @click.self="modal = null">
      <div class="modal">
        <h3>New access token</h3>
        <p>Shown once. Old sessions signed in with the previous token are closed.</p>
        <div class="token-out">{{ modal.token }}</div>
        <div class="row">
          <button class="primary" @click="modal = null">Done</button>
        </div>
      </div>
    </div>
  </div>
</template>
