<script setup>
import LogoMark from "./LogoMark.vue";
import { github } from "../content.js";
const columns = [
  { title: "Product", links: [["Overview", "/product"], ["Capabilities", "/capabilities"], ["Compare", "/compare"], ["Download", "/download"]] },
  { title: "Resources", links: [["Developers", "/developers"], ["Security", "/security"], ["Changelog", "/changelog"], ["Roadmap", "/roadmap"]] },
  { title: "Project", links: [["Company", "/company"], ["GitHub", github], ["Issues", `${github}/issues`], ["License", `${github}/blob/main/LICENSE`]] },
];
</script>
<template>
  <footer class="site-footer"><div class="container">
    <div class="footer-grid"><div class="footer-brand"><RouterLink to="/" class="wordmark"><LogoMark /><span>UNKNOWN<br><b>AGENT</b></span></RouterLink><p>The sovereign runtime for software that has to work.</p><span class="availability"><i></i> Development builds available</span></div>
      <div v-for="col in columns" :key="col.title" class="footer-col"><h3>{{ col.title }}</h3><template v-for="link in col.links" :key="link[0]"><a v-if="link[1].startsWith('http')" :href="link[1]" target="_blank" rel="noreferrer">{{ link[0] }}</a><RouterLink v-else :to="link[1]">{{ link[0] }}</RouterLink></template></div>
    </div>
    <div class="footer-bottom"><span>© {{ new Date().getFullYear() }} Unknown Agent.</span><span>Built in the open · MIT licensed</span></div>
  </div></footer>
</template>
