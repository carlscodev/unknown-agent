<script setup>
import { useI18n } from "vue-i18n";
import PageHero from "../components/PageHero.vue";
import UiIcon from "../components/UiIcon.vue";
import { github } from "../content.js";

const { t } = useI18n();

const principles = ["proof", "sovereignty", "systems", "open"];
</script>

<template>
  <div>
    <PageHero
      :eyebrow="t('company.hero.eyebrow')"
      :title="t('company.hero.title')"
      :lead="t('company.hero.lead')"
    />

    <section class="section no-top">
      <div class="container split">
        <div>
          <p class="eyebrow">{{ t("company.mission.eyebrow") }}</p>
          <h2>{{ t("company.mission.title") }}</h2>
        </div>
        <p class="lede-sm">{{ t("company.mission.lead") }}</p>
      </div>
    </section>

    <section class="section dark-panel">
      <div class="container">
        <div class="section-head">
          <p class="eyebrow">{{ t("company.principles.eyebrow") }}</p>
          <h2>{{ t("company.principles.title") }}</h2>
        </div>
        <div class="values-grid">
          <article v-for="(id, i) in principles" :key="id">
            <span>{{ String(i + 1).padStart(2, "0") }}</span>
            <h3>{{ t(`company.principles.${id}.title`) }}</h3>
            <p>{{ t(`company.principles.${id}.text`) }}</p>
          </article>
        </div>
      </div>
    </section>

    <section class="section">
      <div class="container split">
        <div>
          <p class="eyebrow">{{ t("company.build.eyebrow") }}</p>
          <h2>{{ t("company.build.title") }}</h2>
          <p class="lede-sm">{{ t("company.build.lead") }}</p>
        </div>
        <div class="stack-actions">
          <a class="button primary large" :href="github" target="_blank" rel="noreferrer">
            <UiIcon name="github" /> {{ t("company.build.primary") }}
          </a>
          <a class="button ghost large" :href="`${github}/blob/main/CONTRIBUTING.md`" target="_blank" rel="noreferrer">
            {{ t("company.build.secondary") }}
          </a>
        </div>
      </div>
    </section>
  </div>
</template>
