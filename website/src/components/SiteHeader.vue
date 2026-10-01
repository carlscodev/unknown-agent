<script setup>
import { ref, watch } from "vue";
import { useRoute } from "vue-router";
import { useI18n } from "vue-i18n";
import LogoMark from "./LogoMark.vue";
import UiIcon from "./UiIcon.vue";
import LanguageSwitcher from "./LanguageSwitcher.vue";
import { useLocalePath } from "../composables/useLocalePath.js";
import { github } from "../content.js";

const { t } = useI18n();
const lp = useLocalePath();

const open = ref(false);
const route = useRoute();
watch(() => route.path, () => (open.value = false));

/** Canonical page ids; labels come from the `nav.*` namespace. */
const links = ["product", "capabilities", "developers", "security", "roadmap"];
</script>

<template>
  <header class="site-header">
    <div class="container nav-wrap">
      <RouterLink :to="lp('home')" class="wordmark" :aria-label="t('a11y.homeLink')">
        <LogoMark :size="30" /><span>UNKNOWN<br /><b>AGENT</b></span>
      </RouterLink>

      <nav :class="{ open }" :aria-label="t('a11y.mainNav')">
        <RouterLink v-for="name in links" :key="name" :to="lp(name)">{{ t(`nav.${name}`) }}</RouterLink>
        <RouterLink :to="lp('compare')">{{ t("nav.compare") }}</RouterLink>
        <div class="mobile-actions">
          <a :href="github" target="_blank" rel="noreferrer">{{ t("nav.github") }}</a>
          <RouterLink class="button primary" :to="lp('download')">{{ t("nav.download") }}</RouterLink>
        </div>
        <div class="mobile-lang"><LanguageSwitcher /></div>
      </nav>

      <div class="nav-actions">
        <LanguageSwitcher />
        <a class="icon-link" :href="github" target="_blank" rel="noreferrer" :aria-label="t('a11y.github')">
          <UiIcon name="github" />
        </a>
        <RouterLink class="button small primary" :to="lp('download')">{{ t("nav.download") }}</RouterLink>
      </div>

      <button class="menu-button" :aria-expanded="open" :aria-label="t('a11y.toggleMenu')" @click="open = !open">
        <UiIcon :name="open ? 'close' : 'menu'" />
      </button>
    </div>
  </header>
</template>
