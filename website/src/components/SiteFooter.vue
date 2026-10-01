<script setup>
import { computed } from "vue";
import { useI18n } from "vue-i18n";
import LogoMark from "./LogoMark.vue";
import { useLocalePath } from "../composables/useLocalePath.js";
import { github } from "../content.js";

const { t, locale } = useI18n();
const lp = useLocalePath();

/**
 * Footer columns. Internal entries carry a canonical page id so the link
 * follows the active locale; external entries carry an absolute href.
 */
const columns = computed(() => [
  {
    key: "product",
    links: [
      { key: "overview", page: "product" },
      { key: "capabilities", page: "capabilities" },
      { key: "compare", page: "compare" },
      { key: "download", page: "download" },
    ],
  },
  {
    key: "resources",
    links: [
      { key: "developers", page: "developers" },
      { key: "security", page: "security" },
      { key: "changelog", page: "changelog" },
      { key: "roadmap", page: "roadmap" },
    ],
  },
  {
    key: "project",
    links: [
      { key: "company", page: "company" },
      { key: "github", href: github },
      { key: "issues", href: `${github}/issues` },
      { key: "license", href: `${github}/blob/main/LICENSE` },
    ],
  },
]);

/** Year rendered with the active locale's own numbering system. */
const year = computed(() =>
  new Intl.NumberFormat(locale.value, { useGrouping: false }).format(new Date().getFullYear()),
);
</script>

<template>
  <footer class="site-footer">
    <div class="container">
      <div class="footer-grid">
        <div class="footer-brand">
          <RouterLink :to="lp('home')" class="wordmark">
            <LogoMark /><span>UNKNOWN<br /><b>AGENT</b></span>
          </RouterLink>
          <p>{{ t("footer.tagline") }}</p>
          <span class="availability"><i></i> {{ t("footer.availability") }}</span>
        </div>

        <nav v-for="col in columns" :key="col.key" class="footer-col" :aria-label="t(`footer.columns.${col.key}.title`)">
          <h3>{{ t(`footer.columns.${col.key}.title`) }}</h3>
          <template v-for="link in col.links" :key="link.key">
            <a v-if="link.href" :href="link.href" target="_blank" rel="noreferrer">
              {{ t(`footer.columns.${col.key}.${link.key}`) }}
            </a>
            <RouterLink v-else :to="lp(link.page)">
              {{ t(`footer.columns.${col.key}.${link.key}`) }}
            </RouterLink>
          </template>
        </nav>
      </div>

      <div class="footer-bottom">
        <span>{{ t("footer.copyright", { year }) }}</span>
        <span>{{ t("footer.built") }}</span>
      </div>
    </div>
  </footer>
</template>
