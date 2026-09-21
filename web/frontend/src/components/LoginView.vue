<script setup>
import { onBeforeUnmount, ref } from "vue";
import { api } from "../api.js";
import { Icon } from "../icons.js";

const emit = defineEmits(["authed"]);
const mode = ref("token");
const token = ref("");
const error = ref("");
const code = ref("");
let polling = null;

async function submitToken() {
  error.value = "";
  try {
    await api.login(token.value.trim());
    emit("authed", { authenticated: true, role: "owner" });
  } catch (e) {
    error.value = e.status === 429 ? "Too many attempts — wait a minute." : "Invalid token.";
  }
}

async function startPairing() {
  error.value = "";
  mode.value = "pair";
  try {
    const r = await api.requestDevice();
    code.value = r.code;
    polling = setInterval(async () => {
      try {
        const claim = await api.claimDevice(code.value);
        if (claim.status === "approved") {
          clearInterval(polling);
          emit("authed", { authenticated: true, role: "device" });
        }
      } catch {
        /* pending */
      }
    }, 1500);
  } catch (e) {
    error.value = e.message;
    mode.value = "token";
  }
}

onBeforeUnmount(() => polling && clearInterval(polling));
</script>

<template>
  <div class="login">
    <div class="card">
      <div class="mark"><Icon name="logo" :size="30" /></div>
      <div class="wordmark">Unknown Agent</div>
      <p class="tag">Hyperagent console — chat, providers, tools and runs in one surface.</p>

      <template v-if="mode === 'token'">
        <div class="field">
          <label for="tok">Access token</label>
          <input
            id="tok"
            v-model.trim="token"
            type="password"
            autocomplete="off"
            spellcheck="false"
            placeholder="from ua web on the host"
            @keyup.enter="submitToken"
          />
        </div>
        <div class="err">{{ error }}</div>
        <button class="primary" :disabled="!token" @click="submitToken">Enter console</button>
        <div class="alt">no token? <a @click="startPairing">request access from this device</a></div>
      </template>

      <template v-else>
        <div class="pair-code num">{{ code || "······" }}</div>
        <div class="pair-wait"><span class="pulse" style="width:6px;height:6px;border-radius:50%;background:var(--acc)" /> waiting for terminal approval</div>
        <p class="pair-hint">On the host machine run<br /><code>ua approve device {{ code || "…" }}</code></p>
        <div class="err">{{ error }}</div>
        <div class="alt"><a @click="mode = 'token'">back to token</a></div>
      </template>
    </div>
  </div>
</template>
