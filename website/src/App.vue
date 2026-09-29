<script setup>
import SiteHeader from "./components/SiteHeader.vue";
import SiteFooter from "./components/SiteFooter.vue";
</script>

<template>
  <div class="site-shell">
    <SiteHeader />
    <main id="main"><RouterView /></main>
    <SiteFooter />
  </div>
</template>
