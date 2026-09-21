<script setup>
import { onBeforeUnmount, ref } from "vue";
import { api } from "../api.js";

const emit = defineEmits(["authed"]);
const mode = ref("token"); // token | pair
const token = ref("");
const error = ref("");
const code = ref("");
const polling = ref(null);

async function submitToken() {
  error.value = "";
  try {
    await api.login(token.value.trim());
    emit("authed", { authenticated: true, role: "owner" });
  } catch (e) {
    error.value = e.status === 429 ? "Too many attempts. Wait a minute." : "Invalid token.";
  }
}

async function startPairing() {
  error.value = "";
  mode.value = "pair";
  try {
    const r = await api.requestDevice();
    code.value = r.code;
    polling.value = setInterval(async () => {
      try {
        const claim = await api.claimDevice(code.value);
        if (claim.status === "approved") {
          clearInterval(polling.value);
          emit("authed", { authenticated: true, role: "device" });
        }
      } catch {
        /* still pending */
      }
    }, 1500);
  } catch (e) {
    error.value = e.message;
    mode.value = "token";
  }
}

onBeforeUnmount(() => polling.value && clearInterval(polling.value));
</script>

<template>
  <div class="login-wrap">
    <div class="login-card">
      <div class="wordmark">Unknown<span>&nbsp;Agent</span></div>
      <span class="micro">Control panel</span>

      <template v-if="mode === 'token'">
        <label class="micro" for="tok">Access token</label>
        <input
          id="tok"
          v-model.trim="token"
          type="password"
          autocomplete="off"
          spellcheck="false"
          placeholder="paste the token from ua web"
          @keyup.enter="submitToken"
        />
        <div class="error-line">{{ error }}</div>
        <button class="primary" :disabled="!token" @click="submitToken">Enter</button>
        <div class="login-alt">
          no token? <a @click="startPairing">request access from this device</a>
        </div>
      </template>

      <template v-else>
        <div class="pair-code">{{ code || "······" }}</div>
        <div class="pair-wait"><span class="pulse" /> waiting for approval</div>
        <p class="pair-hint">
          On the machine running the panel:<br />
          <code>ua approve device {{ code || "&lt;code&gt;" }}</code>
        </p>
        <div class="error-line">{{ error }}</div>
        <div class="login-alt"><a @click="mode = 'token'">back to token</a></div>
      </template>
    </div>
  </div>
</template>
