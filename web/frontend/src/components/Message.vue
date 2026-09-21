<script setup>
/* One turn of the conversation.  An assistant turn is a *timeline*: reasoning,
   tool calls, text and artifacts, in the order they happened — the same order
   the server streamed them.  A user turn is just text. */
import { computed } from "vue";
import { renderMarkdown } from "../format.js";
import { Icon } from "../icons.js";
import Blocks from "./Blocks.vue";
import ReasoningBlock from "./ReasoningBlock.vue";
import ToolRow from "./ToolRow.vue";
import ArtifactCard from "./ArtifactCard.vue";

const props = defineProps({ msg: { type: Object, required: true } });

const legacy = computed(() => !props.msg.segs);
const blocks = computed(() => renderMarkdown(props.msg.content || ""));

</script>

<template>
  <div class="msg" :class="[msg.role, { command: msg.command, error: msg.error }]">
    <div class="ava">
      <Icon :name="msg.role === 'user' ? 'user' : msg.error ? 'shield' : 'logo'" :size="15" />
    </div>
    <div class="bodywrap">
      <div class="meta">
        <span class="who">{{ msg.role === "user" ? "You" : msg.error ? "Error" : "Agent" }}</span>
        <span class="when" v-if="msg.model">{{ msg.provider }} / {{ msg.model }}</span>
        <span class="when" v-else-if="msg.command">command</span>
        <span class="when chip-agent" v-if="msg.mode === 'agent'">full agent</span>
      </div>
      <div v-if="blocks.length || msg.typing" class="bubble" :class="{ typing: msg.typing }">
        <template v-for="(b, i) in blocks" :key="i">
          <div v-if="b.kind === 'code'" class="codeblock" :class="{ collapsed: isLong(b) && !expanded[i] }">
            <div class="bar">
              <span>{{ b.lang }} · {{ b.code.split("\n").length }} lines</span>
              <span class="acts">
                <button v-if="isLong(b)" @click="toggle(i)">
                  {{ expanded[i] ? "collapse" : "expand" }}
                  <Icon name="chevron" :size="12" />
                </button>
                <button @click="copy(b.code)">copy</button>
              </span>
            </div>
            <pre>{{ b.code }}</pre>
          </div>
          <p v-else v-html="b.html" />
        </template>
      </div>

      <div v-if="msg.artifact" class="artifact">
        <div class="abar">
          <Icon :name="msg.artifact.kind === 'image' ? 'image' : 'file'" :size="13" />
          <span class="name">{{ msg.artifact.name }}</span>
          <span class="live">live preview</span>
          <span class="grow" />
          <button
            v-if="msg.artifact.kind === 'html' || msg.artifact.kind === 'image'"
            title="reload preview"
            @click="reload"
          >
            <Icon name="refresh" :size="13" />
          </button>
          <button title="open in new tab" @click="open"><Icon name="eye" :size="13" /></button>
        </div>
        <iframe
          v-if="msg.artifact.kind === 'html'"
          :key="previewKey"
          :src="msg.artifact.url"
          :title="msg.artifact.name"
          class="frame"
          sandbox="allow-scripts allow-same-origin allow-forms"
        />
        <img
          v-else-if="msg.artifact.kind === 'image'"
          :key="previewKey"
          :src="msg.artifact.url"
          :alt="msg.artifact.name"
          class="imgprev"
        />
        <div v-else class="filecard">
          <Icon name="doc" :size="15" />
          <span>{{ msg.artifact.kind === "pdf" ? "PDF document" : "file" }} written by the agent</span>
          <button @click="open">open</button>
        </div>
        <span class="who">{{ msg.role === "user" ? "Tú" : msg.error ? "Error" : "Agent" }}</span>
        <span v-if="msg.model" class="when">{{ msg.provider }} / {{ msg.model }}</span>
        <span v-else-if="msg.command" class="when">command</span>
        <span v-if="msg.mode === 'agent'" class="when chip-agent">full agent</span>
        <span v-else-if="msg.mode === 'tools'" class="when chip-agent">tools</span>
      </div>

      <!-- assistant timeline -->
      <template v-if="!legacy">
        <template v-for="(seg, i) in msg.segs" :key="i">
          <ReasoningBlock
            v-if="seg.kind === 'reasoning'"
            :text="seg.text"
            :thinking="seg.thinking"
            :secs="seg.secs || 0"
          />
          <ToolRow v-else-if="seg.kind === 'tool'" :item="seg.item" />
          <ArtifactCard v-else-if="seg.kind === 'artifact'" :artifact="seg.artifact" />
          <div v-else-if="seg.kind === 'text'" class="bubble" :class="{ typing: msg.typing }">
            <Blocks :blocks="renderMarkdown(seg.text)" />
          </div>
        </template>
      </template>

      <!-- plain text turns (user, slash commands, legacy payloads) -->
      <div v-else-if="blocks.length || msg.typing" class="bubble" :class="{ typing: msg.typing }">
        <Blocks :blocks="blocks" />
      </div>
    </div>
  </div>
</template>
