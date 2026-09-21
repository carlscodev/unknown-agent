<script setup>
import { onMounted, ref } from "vue";
import { api } from "./api.js";
import { Icon } from "./icons.js";
import LoginView from "./components/LoginView.vue";
import ChatView from "./components/ChatView.vue";
import Dashboard from "./components/Dashboard.vue";

const session = ref(null);
const ready = ref(false);
const view = ref("chat");
const version = ref("");
const live = ref(false);
const toast = ref("");
let toastTimer = null;
const chatRef = ref(null);

function showToast(msg) {
  toast.value = msg;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (toast.value = ""), 2600);
}

async function pollHealth() {
  try {
    const h = await api.health();
    version.value = h.version;
    live.value = true;
  } catch {
    live.value = false;
  }
}

onMounted(async () => {
  pollHealth();
  setInterval(pollHealth, 8000);
  try {
    const s = await api.session();
    session.value = s.authenticated ? s : false;
  } catch {
    session.value = false;
  }
  ready.value = true;
});

async function logout() {
  await api.logout();
  session.value = false;
}
</script>

<template>
  <div v-if="!ready" class="login"><div class="pulse" style="width:8px;height:8px;border-radius:50%;background:var(--acc)" /></div>
  <LoginView v-else-if="!session" @authed="(s) => (session = s)" />

  <div v-else class="shell">
    <header class="topbar">
      <div class="brand">
        <span class="mark"><Icon name="logo" :size="22" /></span>
        <span class="name">Unknown Agent</span>
        <span class="ver">v{{ version }}</span>
      </div>
      <span class="grow" />
      <span class="livechip" :class="{ off: !live }">
        <span class="dot" />
        {{ live ? "connected" : "offline" }}
      </span>
    </header>

    <div class="body">
      <nav class="rail">
        <button :class="{ on: view === 'chat' }" title="Chat" aria-label="Chat" @click="view = 'chat'">
          <Icon name="chat" :size="19" />
        </button>
        <button
          v-if="session.role === 'owner'"
          :class="{ on: view === 'dash' }"
          title="Console"
          aria-label="Console"
          @click="view = 'dash'"
        >
          <Icon name="gauge" :size="19" />
        </button>
        <span class="grow" />
        <button title="Sign out" aria-label="Sign out" @click="logout">
          <Icon name="logout" :size="18" />
        </button>
      </nav>

      <ChatView v-if="view === 'chat'" ref="chatRef" :session="session" @toast="showToast" />
      <Dashboard v-else @toast="showToast" />
    </div>

    <div v-if="toast" class="toast">{{ toast }}</div>
  </div>
</template>
