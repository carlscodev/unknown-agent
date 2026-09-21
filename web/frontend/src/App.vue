<script setup>
import { onMounted, ref } from "vue";
import { api } from "./api.js";
import LoginView from "./components/LoginView.vue";
import ChatView from "./components/ChatView.vue";

const session = ref(null); // null = unknown, false = logged out, object = in
const ready = ref(false);

onMounted(async () => {
  try {
    const s = await api.session();
    session.value = s.authenticated ? s : false;
  } catch {
    session.value = false;
  }
  ready.value = true;
});

function onAuthed(s) {
  session.value = s;
}
function onLogout() {
  session.value = false;
}
</script>

<template>
  <div v-if="!ready" class="login-wrap"><div class="pulse" /></div>
  <LoginView v-else-if="!session" @authed="onAuthed" />
  <ChatView v-else :session="session" @logout="onLogout" />
</template>
