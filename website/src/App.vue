<script setup>
import SiteHeader from "./components/SiteHeader.vue";
import SiteFooter from "./components/SiteFooter.vue";
import { useSeo } from "./composables/useSeo.js";
import { useI18n } from "vue-i18n";

// Keeps <title>, meta description, canonical, hreflang, Open Graph and
// <html lang/dir> in sync with the active route and locale.
useSeo();

const { t } = useI18n();
</script>

<template>
  <div class="site-shell">
    <a class="skip-link" href="#main">{{ t("a11y.skipToContent") }}</a>
    <SiteHeader />
    <main id="main"><RouterView /></main>
    <SiteFooter />
  </div>
</template>
