<script setup>
import { ref, watch } from "vue";
import { useRoute } from "vue-router";
import LogoMark from "./LogoMark.vue";
import UiIcon from "./UiIcon.vue";
import { github } from "../content.js";

const open = ref(false);
const route = useRoute();
watch(() => route.path, () => (open.value = false));
const links = [
  ["Product", "/product"], ["Capabilities", "/capabilities"], ["Developers", "/developers"],
  ["Security", "/security"], ["Roadmap", "/roadmap"],
];
</script>
<template>
  <header class="site-header">
    <div class="container nav-wrap">
      <RouterLink to="/" class="wordmark" aria-label="Unknown Agent home"><LogoMark :size="30" /><span>UNKNOWN<br><b>AGENT</b></span></RouterLink>
      <nav :class="{ open }" aria-label="Main navigation">
        <RouterLink v-for="link in links" :key="link[1]" :to="link[1]">{{ link[0] }}</RouterLink>
        <RouterLink to="/compare">Compare</RouterLink>
        <div class="mobile-actions"><a :href="github" target="_blank" rel="noreferrer">GitHub</a><RouterLink class="button primary" to="/download">Download</RouterLink></div>
      </nav>
      <div class="nav-actions"><a class="icon-link" :href="github" target="_blank" rel="noreferrer" aria-label="GitHub"><UiIcon name="github" /></a><RouterLink class="button small primary" to="/download">Download</RouterLink></div>
      <button class="menu-button" @click="open = !open" :aria-expanded="open" aria-label="Toggle menu"><UiIcon :name="open ? 'close' : 'menu'" /></button>
    </div>
  </header>
</template>
