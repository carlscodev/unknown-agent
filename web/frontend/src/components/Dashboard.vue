<script setup>
import { onMounted, reactive, ref } from "vue";
import { api } from "../api.js";
import { Icon } from "../icons.js";

const emit = defineEmits(["toast"]);
const tab = ref("overview");
const d = ref(null);
const modal = ref(null);

const provForm = reactive({ name: "", type: "openai-compatible", model: "", base_url: "", api_key: "" });
const mcpForm = reactive({ name: "", command: "", args: "", url: "" });
const filesForm = reactive({ max_upload_mb: 25, auto_extract: true });
const agentForm = reactive({ mode: "auto", max_steps: 60, reasoning: "" });

const TABS = [
  ["overview", "Overview"],
  ["providers", "Providers"],
  ["agent", "Agent"],
  ["mcp", "MCP tools"],
  ["files", "Files"],
  ["usage", "Usage"],
  ["security", "Security"],
];

async function load() {
  try {
    d.value = await api.dashboard();
    Object.assign(filesForm, d.value.files);
    Object.assign(agentForm, d.value.agent);
  } catch (e) {
    emit("toast", e.message);
  }
}

function ok(msg) {
  emit("toast", msg);
  load();
}

async function addProvider() {
  try {
    await api.addProvider({
      name: provForm.name,
      type: provForm.type,
      model: provForm.model || "auto",
      base_url: provForm.base_url || null,
      api_key: provForm.api_key || null,
    });
    provForm.name = provForm.base_url = provForm.api_key = "";
    ok("provider saved");
  } catch (e) {
    emit("toast", e.message);
  }
}
async function removeProvider(name) {
  try {
    await api.removeProvider(name);
    ok("provider removed");
  } catch (e) {
    emit("toast", e.message);
  }
}
async function saveAgent() {
  try {
    await api.setAgent(agentForm);
    ok("agent settings saved");
  } catch (e) {
    emit("toast", e.message);
  }
}
async function addMcp() {
  try {
    await api.mcpAdd({
      name: mcpForm.name,
      command: mcpForm.command || null,
      args: mcpForm.args ? mcpForm.args.split(/\s+/) : [],
      url: mcpForm.url || null,
    });
    mcpForm.name = mcpForm.command = mcpForm.args = mcpForm.url = "";
    ok("mcp server added");
  } catch (e) {
    emit("toast", e.message);
  }
}
async function toggleMcp(name, enabled) {
  try {
    await api.mcpToggle(name, enabled);
    ok(enabled ? "server enabled" : "server disabled");
  } catch (e) {
    emit("toast", e.message);
  }
}
async function removeMcp(name) {
  try {
    await api.mcpRemove(name);
    ok("server removed");
  } catch (e) {
    emit("toast", e.message);
  }
}
async function saveFiles() {
  try {
    await api.setFiles(filesForm);
    ok("file options saved");
  } catch (e) {
    emit("toast", e.message);
  }
}
async function rotate() {
  try {
    const r = await api.rotateToken();
    modal.value = { token: r.token };
    load();
  } catch (e) {
    emit("toast", e.message);
  }
}
async function revoke(code) {
  try {
    await api.revokeDevice(code);
    ok("device revoked");
  } catch (e) {
    emit("toast", e.message);
  }
}

onMounted(load);
</script>

<template>
  <div class="dash">
    <div class="wrap" v-if="d">
      <h1>Console</h1>
      <p class="sub">Configuration and control surface. Changes persist to the same config the CLI and TUI read.</p>

      <div class="tabs">
        <button v-for="[id, label] in TABS" :key="id" :class="{ on: tab === id }" @click="tab = id">
          {{ label }}
        </button>
      </div>

      <!-- overview ------------------------------------------------------- -->
      <div v-if="tab === 'overview'" class="grid">
        <div class="card">
          <h3><Icon name="bolt" /> Active runtime</h3>
          <div class="row"><span class="k">provider</span><span class="v">{{ d.active }}</span></div>
          <div class="row">
            <span class="k">model</span>
            <span class="v">{{ (d.providers.find((p) => p.active) || {}).model || "unset" }}</span>
          </div>
          <div class="row"><span class="k">agent mode</span><span class="v">{{ d.agent.mode }}</span></div>
          <div class="row"><span class="k">reasoning</span><span class="v">{{ d.agent.reasoning || "default" }}</span></div>
        </div>
        <div class="card">
          <h3><Icon name="cpu" /> Capabilities</h3>
          <div class="row"><span class="k">skill pack</span><span class="v">{{ d.skills.length }} skills</span></div>
          <div class="row"><span class="k">mcp servers</span><span class="v">{{ d.mcp.servers.length }}</span></div>
          <div class="row"><span class="k">paired devices</span><span class="v">{{ Object.keys(d.devices.devices).length }}</span></div>
          <div class="row"><span class="k">upload cap</span><span class="v">{{ d.files.max_upload_mb }} MB</span></div>
        </div>
        <div class="card">
          <h3><Icon name="wave" /> Session usage</h3>
          <div class="row"><span class="k">requests</span><span class="v num">{{ d.usage.requests }}</span></div>
          <div class="row"><span class="k">output chars</span><span class="v num">{{ d.usage.chars }}</span></div>
          <div class="row"><span class="k">uploads</span><span class="v num">{{ d.usage.uploads }}</span></div>
          <div class="row"><span class="k">commands</span><span class="v num">{{ d.usage.commands }}</span></div>
        </div>
      </div>

      <!-- providers ------------------------------------------------------- -->
      <div v-else-if="tab === 'providers'" class="grid">
        <div class="card" style="grid-column: 1 / -1">
          <h3><Icon name="plug" /> Provider profiles</h3>
          <div v-for="p in d.providers" :key="p.name" class="row">
            <span class="k" style="display: flex; align-items: center; gap: 8px">
              <span class="dot" :style="{ width: '6px', height: '6px', borderRadius: '50%', background: p.active ? 'var(--ok)' : 'var(--t4)' }" />
              {{ p.name }} <span style="color: var(--t4)">[{{ p.type }}]</span>
            </span>
            <span class="v">{{ p.model }}{{ p.base_url ? " · " + p.base_url : "" }}</span>
            <button v-if="d.providers.length > 1" class="btn-ghost btn-danger" style="padding: 4px 8px" @click="removeProvider(p.name)">
              <Icon name="trash" :size="14" />
            </button>
          </div>
          <div class="frow cols-3" style="margin-top: 14px">
            <div class="field"><label>name</label><input v-model.trim="provForm.name" placeholder="deepseek" /></div>
            <div class="field">
              <label>type</label>
              <select v-model="provForm.type">
                <option>openai-compatible</option>
                <option>anthropic</option>
                <option>ollama</option>
                <option>unli</option>
                <option>openrouter</option>
                <option>groq</option>
              </select>
            </div>
            <div class="field"><label>model</label><input v-model.trim="provForm.model" placeholder="auto" /></div>
          </div>
          <div class="frow cols-2">
            <div class="field"><label>base url (optional)</label><input v-model.trim="provForm.base_url" placeholder="https://…" /></div>
            <div class="field"><label>api key (optional)</label><input v-model.trim="provForm.api_key" type="password" placeholder="env:VAR or literal" /></div>
          </div>
          <div class="actions"><button class="btn-acc" :disabled="!provForm.name" @click="addProvider">Save profile</button></div>
        </div>
      </div>

      <!-- agent ----------------------------------------------------------- -->
      <div v-else-if="tab === 'agent'" class="grid">
        <div class="card">
          <h3><Icon name="cpu" /> Agent policy</h3>
          <div class="field">
            <label>mode</label>
            <div class="seg">
              <button v-for="m in ['auto', 'plan', 'react']" :key="m" :class="{ on: agentForm.mode === m }" @click="agentForm.mode = m">{{ m }}</button>
            </div>
          </div>
          <div class="frow cols-2">
            <div class="field"><label>max steps</label><input v-model.number="agentForm.max_steps" type="number" min="1" max="200" /></div>
            <div class="field"><label>reasoning level</label><input v-model.trim="agentForm.reasoning" placeholder="default" /></div>
          </div>
          <div class="actions"><button class="btn-acc" @click="saveAgent">Save agent</button></div>
        </div>
        <div class="card">
          <h3><Icon name="bolt" /> Active skill pack</h3>
          <div class="row" v-for="s in d.skills" :key="s"><span class="k mono">{{ s }}</span></div>
          <div v-if="!d.skills.length" class="row"><span class="k">pack disabled</span></div>
        </div>
      </div>

      <!-- mcp -------------------------------------------------------------- -->
      <div v-else-if="tab === 'mcp'" class="grid">
        <div class="card" style="grid-column: 1 / -1">
          <h3><Icon name="plug" /> MCP servers</h3>
          <div v-for="s in d.mcp.servers" :key="s.name" class="srv">
            <div class="grow">
              <div class="n">{{ s.name }}</div>
              <div class="d">{{ s.transport }} · {{ s.command ? s.command + " " + s.args.join(" ") : s.url }}</div>
            </div>
            <button class="switch" :aria-checked="String(s.enabled)" aria-label="toggle server" @click="toggleMcp(s.name, !s.enabled)" />
            <button class="icon-btn btn-danger" aria-label="remove server" @click="removeMcp(s.name)"><Icon name="trash" :size="15" /></button>
          </div>
          <div v-if="!d.mcp.servers.length" class="row"><span class="k">no servers configured</span></div>
          <div class="frow cols-3" style="margin-top: 14px">
            <div class="field"><label>name</label><input v-model.trim="mcpForm.name" placeholder="github" /></div>
            <div class="field"><label>command</label><input v-model.trim="mcpForm.command" placeholder="uvx mcp-server-github" /></div>
            <div class="field"><label>args</label><input v-model.trim="mcpForm.args" placeholder="--flag value" /></div>
          </div>
          <div class="field"><label>or url (http transport)</label><input v-model.trim="mcpForm.url" placeholder="https://…" /></div>
          <div class="actions"><button class="btn-acc" :disabled="!mcpForm.name || (!mcpForm.command && !mcpForm.url)" @click="addMcp">Add server</button></div>
        </div>
      </div>

      <!-- files -------------------------------------------------------------- -->
      <div v-else-if="tab === 'files'" class="grid">
        <div class="card">
          <h3><Icon name="file" /> File handling</h3>
          <div class="field">
            <label>max upload size (MB)</label>
            <input v-model.number="filesForm.max_upload_mb" type="number" min="1" max="200" />
          </div>
          <div class="row">
            <span class="k">extract text and PDF content into context</span>
            <button class="switch" :aria-checked="String(filesForm.auto_extract)" aria-label="auto extract" @click="filesForm.auto_extract = !filesForm.auto_extract" />
          </div>
          <div class="actions"><button class="btn-acc" @click="saveFiles">Save file options</button></div>
        </div>
        <div class="card">
          <h3><Icon name="doc" /> How attachments are consumed</h3>
          <div class="row"><span class="k">text and PDF</span><span class="v">extracted into the prompt</span></div>
          <div class="row"><span class="k">images</span><span class="v">native vision parts</span></div>
          <div class="row"><span class="k">audio and video</span><span class="v">stored, path passed to tooling</span></div>
        </div>
      </div>

      <!-- usage --------------------------------------------------------------- -->
      <div v-else-if="tab === 'usage'" class="grid">
        <div class="card stat"><span class="n num">{{ d.usage.requests }}</span><span class="l">model requests</span></div>
        <div class="card stat"><span class="n num">{{ d.usage.chars }}</span><span class="l">output characters</span></div>
        <div class="card stat"><span class="n num">{{ d.usage.uploads }}</span><span class="l">uploads</span></div>
        <div class="card stat"><span class="n num">{{ d.usage.commands }}</span><span class="l">slash commands</span></div>
      </div>

      <!-- security --------------------------------------------------------------- -->
      <div v-else-if="tab === 'security'" class="grid">
        <div class="card">
          <h3><Icon name="shield" /> Access token</h3>
          <p class="sub" style="margin: 0 0 12px">Rotating closes every owner session and prints the new token once.</p>
          <div class="actions"><button class="btn-acc" @click="rotate">Rotate token</button></div>
        </div>
        <div class="card">
          <h3><Icon name="user" /> Devices</h3>
          <div v-if="Object.keys(d.devices.pending).length" class="row">
            <span class="k">pending codes</span>
            <span class="v">{{ Object.keys(d.devices.pending).join(", ") }}</span>
          </div>
          <div v-for="(meta, code) in d.devices.devices" :key="code" class="srv">
            <div class="grow">
              <div class="n num">{{ code }}</div>
              <div class="d">{{ meta.name || meta.label || "" }}</div>
            </div>
            <button class="btn-ghost btn-danger" style="padding: 4px 9px" @click="revoke(code)">revoke</button>
          </div>
          <div v-if="!Object.keys(d.devices.devices).length" class="row"><span class="k">no paired devices</span></div>
        </div>
      </div>
    </div>

    <div v-if="modal" class="scrim" @click.self="modal = null">
      <div class="modal">
        <h3>New access token</h3>
        <p>Shown once. Sessions using the previous token are closed.</p>
        <div class="token-out">{{ modal.token }}</div>
        <div class="rowend"><button class="btn-acc" @click="modal = null">Done</button></div>
      </div>
    </div>
  </div>
</template>
