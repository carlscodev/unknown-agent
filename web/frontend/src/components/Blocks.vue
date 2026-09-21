<script setup>
/* Renders the block list produced by format.js: headings, lists, paragraphs
   and code cards (code is delegated so previews/copy work everywhere). */
import CodeCard from "./CodeCard.vue";

defineProps({ blocks: { type: Array, default: () => [] } });
</script>

<template>
  <template v-for="(b, i) in blocks" :key="i">
    <CodeCard v-if="b.kind === 'code'" :block="b" />
    <component :is="'h' + (b.level + 2)" v-else-if="b.kind === 'h'" v-html="b.html" />
    <ul v-else-if="b.kind === 'list' && b.tag === 'ul'">
      <li v-for="(it, j) in b.items" :key="j" v-html="it" />
    </ul>
    <ol v-else-if="b.kind === 'list'">
      <li v-for="(it, j) in b.items" :key="j" v-html="it" />
    </ol>
    <p v-else v-html="b.html" />
  </template>
</template>
