<script setup>
import { useI18n } from "vue-i18n";
import PageHero from "../components/PageHero.vue";
import CodeWindow from "../components/CodeWindow.vue";
import UiIcon from "../components/UiIcon.vue";
import { useLocalePath } from "../composables/useLocalePath.js";
import { github, releases } from "../content.js";

const { t } = useI18n();
const lp = useLocalePath();

/**
 * `abbr` is a literal two-letter mark derived from the OS brand name. It is
 * hardcoded rather than sliced from the translated string, because slicing
 * produces nonsense in non-Latin scripts.
 */
const platforms = [
  { id: "linux", abbr: "LI" },
  { id: "macos", abbr: "MA" },
  { id: "windows", abbr: "WI" },
];
const requirements = ["req1", "req2", "req3"];
</script>

<template>
  <div>
    <PageHero
      :eyebrow="t('download.hero.eyebrow')"
      :title="t('download.hero.title')"
      :lead="t('download.hero.lead')"
    >
      <div class="hero-actions center">
        <a class="button primary large" :href="releases" target="_blank" rel="noreferrer">
          <UiIcon name="download" /> {{ t("download.hero.ctaPrimary") }}
        </a>
        <a class="button ghost large" :href="github" target="_blank" rel="noreferrer">
          <UiIcon name="github" /> {{ t("download.hero.ctaSecondary") }}
        </a>
      </div>
    </PageHero>

    <section class="section no-top">
      <div class="container">
        <div class="install-feature">
          <div>
            <span class="recommended">{{ t("download.pipx.recommended") }}</span>
            <h2>{{ t("download.pipx.title") }}</h2>
            <!-- Split around <code>ua</code>: the command name is never translated. -->
            <p>{{ t("download.pipx.textBefore") }} <code dir="ltr">ua</code> {{ t("download.pipx.textAfter") }}</p>
          </div>
          <CodeWindow command='pipx install "git+https://github.com/flormariap/unknown-agent"' />
        </div>
        <div class="after-install">
          <p>{{ t("download.pipx.thenRun") }}</p>
          <CodeWindow command="ua setup" :label="t('download.pipx.setupLabel')" />
          <p>{{ t("download.pipx.setupText") }}</p>
        </div>
      </div>
    </section>

    <section class="section dark-panel">
      <div class="container">
        <div class="section-head">
          <p class="eyebrow">{{ t("download.platforms.eyebrow") }}</p>
          <h2>{{ t("download.platforms.title") }}</h2>
        </div>
        <div class="platform-grid">
          <article v-for="p in platforms" :key="p.id">
            <span class="platform-icon">{{ p.abbr }}</span>
            <h3>{{ t(`download.platforms.${p.id}.name`) }}</h3>
            <b>{{ t(`download.platforms.${p.id}.status`) }}</b>
            <p>{{ t(`download.platforms.${p.id}.text`) }}</p>
          </article>
        </div>
        <div class="requirements">
          <span v-for="key in requirements" :key="key">{{ t(`download.platforms.${key}`) }}</span>
        </div>
      </div>
    </section>

    <section class="section">
      <div class="container split">
        <div>
          <p class="eyebrow">{{ t("download.installer.eyebrow") }}</p>
          <h2>{{ t("download.installer.title") }}</h2>
          <p class="lede-sm">{{ t("download.installer.lead") }}</p>
        </div>
        <CodeWindow
          command="curl -fsSLO https://raw.githubusercontent.com/flormariap/unknown-agent/main/scripts/install.sh"
          :label="t('download.installer.label')"
        />
      </div>
    </section>

    <section class="section dark-panel">
      <div class="container split">
        <div>
          <p class="eyebrow">{{ t("download.client.eyebrow") }}</p>
          <h2>{{ t("download.client.title") }}</h2>
          <p class="lede-sm">{{ t("download.client.lead") }}</p>
          <div class="hero-actions">
            <a class="button primary" :href="releases" target="_blank" rel="noreferrer">
              {{ t("download.client.button") }}
            </a>
          </div>
        </div>
        <CodeWindow command="ua client" :label="t('download.client.label')" />
      </div>
    </section>

    <section class="section mini-cta">
      <div class="container">
        <h2>{{ t("download.cta.title") }}</h2>
        <RouterLink class="button primary" :to="lp('developers')">
          {{ t("download.cta.button") }} <UiIcon name="arrow" class="icon-directional" />
        </RouterLink>
      </div>
    </section>
  </div>
</template>
