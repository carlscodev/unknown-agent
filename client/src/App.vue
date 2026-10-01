<script setup>
import {computed,onMounted,ref} from 'vue'
import * as client from './runtime.js'
const tab=ref('inbox'), items=ref([]), cfg=ref({}), busy=ref(false), error=ref(''), endpoint=ref(''), token=ref(''), draft=ref('')
const approvals=computed(()=>items.value.filter(x=>x.kind==='approval'&&!x.payload.resolved))
const messages=computed(()=>items.value.filter(x=>x.kind==='message'))
async function load(){cfg.value=await client.settings();endpoint.value=cfg.value.endpoint;items.value=await client.events()}
async function act(fn){busy.value=true;error.value='';try{await fn();await load()}catch(e){error.value=e.message||String(e)}finally{busy.value=false}}
const save=()=>act(()=>client.configure(endpoint.value))
const enroll=()=>act(()=>client.enroll(token.value))
const sync=()=>act(()=>client.sync())
const send=()=>act(async()=>{if(!draft.value.trim())return;await client.queue('message.send',{text:draft.value.trim()});draft.value='';await client.sync()})
const decide=(id,approved)=>act(async()=>{await client.resolveApproval(id,approved);await client.sync()})
onMounted(load)
</script>
<template><div class="shell">
<aside><div class="brand"><span class="mark">U</span><div><b>Unknown Agent</b><small>Sovereign client</small></div></div><nav><button v-for="x in [['inbox','Inbox'],['approvals','Approvals'],['device','Device'],['settings','Settings']]" :class="{active:tab===x[0]}" @click="tab=x[0]"><span>{{x[0]==='inbox'?'◫':x[0]==='approvals'?'✓':x[0]==='device'?'◇':'⚙'}}</span>{{x[1]}}<i v-if="x[0]==='approvals'&&approvals.length">{{approvals.length}}</i></button></nav><div class="connection"><span :class="['dot',cfg.enrolled?'online':'']"></span>{{cfg.enrolled?'Enrolled':'Setup required'}}</div></aside>
<main><header><div><small>WORKSPACE</small><h1>{{tab[0]?.toUpperCase()+tab.slice(1)}}</h1></div><button class="sync" :disabled="busy||!cfg.enrolled" @click="sync">{{busy?'Working…':'↻ Sync'}}</button></header><p v-if="error" class="error">{{error}}</p>
<section v-if="tab==='inbox'" class="panel"><div v-if="!messages.length" class="empty"><div class="orb">✦</div><h2>Your agent is ready</h2><p>Messages and mission updates remain available offline after synchronization.</p></div><article v-for="m in messages" class="message"><small>#{{m.sequence}} · {{new Date(m.created_at).toLocaleString()}}</small><p>{{m.payload.text||m.payload.message}}</p></article><form class="composer" @submit.prevent="send"><input v-model="draft" placeholder="Message your agent…" maxlength="200000"><button :disabled="busy||!cfg.enrolled">Send</button></form></section>
<section v-else-if="tab==='approvals'" class="cards"><div v-if="!approvals.length" class="empty"><div class="orb safe">✓</div><h2>No pending approvals</h2><p>Risky actions wait here for an explicit decision.</p></div><article v-for="a in approvals" class="approval"><div><small>{{a.payload.risk||'APPROVAL'}}</small><h3>{{a.payload.title||a.payload.action}}</h3><p>{{a.payload.description}}</p></div><pre v-if="a.payload.details">{{JSON.stringify(a.payload.details,null,2)}}</pre><footer><button class="deny" @click="decide(a.id,false)">Deny</button><button class="approve" @click="decide(a.id,true)">Approve</button></footer></article></section>
<section v-else-if="tab==='device'" class="cards"><article class="device"><div class="phone">◇</div><div><small>DEVICE IDENTITY</small><h3>{{cfg.deviceId}}</h3><p>Non-exportable Ed25519 signing key · encrypted local storage</p><code>{{cfg.publicKey}}</code></div></article><article class="stat"><span>Enrollment</span><b>{{cfg.enrolled?'Active':'Pending'}}</b><span>Sync cursor</span><b>{{cfg.cursor||'Not synchronized'}}</b></article></section>
<section v-else class="settings"><label>Agent endpoint<input v-model="endpoint" placeholder="https://agent.example"></label><button @click="save" :disabled="busy">Save endpoint</button><label>One-time enrollment token<input v-model="token" type="password" autocomplete="off" placeholder="Paste token"></label><button @click="enroll" :disabled="busy||!endpoint||!token">Enroll this device</button><div class="danger"><h3>Reset local client</h3><p>Deletes encrypted local events, queued operations and this device's local keys.</p><button @click="client.reset()">Erase local data</button></div></section>
</main></div></template>
