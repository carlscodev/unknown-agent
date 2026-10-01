<script setup>
import { useI18n } from "vue-i18n";
import PageHero from "../components/PageHero.vue";
import UiIcon from "../components/UiIcon.vue";
import { github } from "../content.js";

const { t } = useI18n();

const controls = [
  "policy", "filesystem", "secrets", "process", "audit", "access",
  "extensions", "hooks", "automation", "sandbox", "acp", "messaging",
];
const flow = ["flow1", "flow2", "flow3", "flow4", "flow5"];
</script>

<template>
  <div>
    <PageHero
      :eyebrow="t('security.hero.eyebrow')"
      :title="t('security.hero.title')"
      :lead="t('security.hero.lead')"
    >
      <div class="security-badge">
        <UiIcon name="shield" :size="26" />
        <div>
          <b>{{ t("security.hero.badgeTitle") }}</b>
          <span>{{ t("security.hero.badgeText") }}</span>
        </div>
      </div>
    </PageHero>

    <section class="section no-top">
      <div class="container">
        <div class="control-grid">
          <article v-for="(id, i) in controls" :key="id">
            <span>{{ String(i + 1).padStart(2, "0") }}</span>
            <h3>{{ t(`security.controls.${id}.title`) }}</h3>
            <p>{{ t(`security.controls.${id}.text`) }}</p>
          </article>
        </div>
      </div>
    </section>

    <section class="section dark-panel">
      <div class="container split">
        <div>
          <p class="eyebrow">{{ t("security.threat.eyebrow") }}</p>
          <h2>{{ t("security.threat.title") }}</h2>
          <p class="lede-sm">{{ t("security.threat.lead") }}</p>
        </div>
        <div class="security-flow">
          <template v-for="(key, i) in flow" :key="key">
            <div>{{ t(`security.threat.${key}`) }}</div>
            <i v-if="i < flow.length - 1" aria-hidden="true">↓</i>
          </template>
        </div>
      </div>
    </section>

    <section class="section">
      <div class="container split">
        <div>
          <p class="eyebrow">{{ t("security.reporting.eyebrow") }}</p>
          <h2>{{ t("security.reporting.title") }}</h2>
          <p class="lede-sm">{{ t("security.reporting.lead") }}</p>
          <a class="button primary" :href="`${github}/security/advisories/new`" target="_blank" rel="noreferrer">
            {{ t("security.reporting.button") }} <UiIcon name="arrow" class="icon-directional" />
          </a>
        </div>
        <div class="policy-card">
          <span>{{ t("security.reporting.cardLabel") }}</span>
          <p>{{ t("security.reporting.cardText") }}</p>
          <a :href="`${github}/blob/main/SECURITY.md`" target="_blank" rel="noreferrer">
            {{ t("security.reporting.cardLink") }}
          </a>
        </div>
      </div>
    </section>
  </div>
</template>
