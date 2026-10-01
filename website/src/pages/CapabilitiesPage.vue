<script setup>
import { useI18n } from "vue-i18n";
import PageHero from "../components/PageHero.vue";
import UiIcon from "../components/UiIcon.vue";
import { useLocalePath } from "../composables/useLocalePath.js";
import { capabilityCards } from "../content.js";

const { t } = useI18n();
const lp = useLocalePath();

/** `n` is a language-neutral ordinal; `id` keys into `capabilities.groups`. */
const groups = [
  { n: "01", id: "codeIntelligence" },
  { n: "02", id: "autonomousExecution" },
  { n: "03", id: "reasoningControl" },
  { n: "04", id: "evidenceSafety" },
  { n: "05", id: "durableMemory" },
  { n: "06", id: "providerFabric" },
];
const itemKeys = ["i1", "i2", "i3", "i4"];
</script>

<template>
  <div>
    <PageHero
      :eyebrow="t('capabilities.hero.eyebrow')"
      :title="t('capabilities.hero.title')"
      :lead="t('capabilities.hero.lead')"
    />

    <section class="section no-top">
      <div class="container">
        <div class="cap-grid wide">
          <article v-for="card in capabilityCards" :key="card.id" class="cap-card">
            <div class="cap-icon"><UiIcon :name="card.icon" /></div>
            <h3>{{ t(`capabilityCards.${card.id}.label`) }}</h3>
            <p>{{ t(`capabilityCards.${card.id}.text`) }}</p>
          </article>
        </div>
      </div>
    </section>

    <section class="section dark-panel">
      <div class="container">
        <div class="section-head row">
          <div>
            <p class="eyebrow">{{ t("capabilities.stack.eyebrow") }}</p>
            <h2>{{ t("capabilities.stack.title") }}</h2>
          </div>
          <p>{{ t("capabilities.stack.lead") }}</p>
        </div>
        <div class="cap-groups">
          <article v-for="group in groups" :key="group.id">
            <div class="group-head">
              <span>{{ group.n }}</span>
              <h3>{{ t(`capabilities.groups.${group.id}.name`) }}</h3>
            </div>
            <ul>
              <li v-for="key in itemKeys" :key="key">
                <UiIcon name="check" :size="15" />{{ t(`capabilities.groups.${group.id}.${key}`) }}
              </li>
            </ul>
          </article>
        </div>
      </div>
    </section>

    <section class="section mini-cta">
      <div class="container">
        <h2>{{ t("capabilities.cta.title") }}</h2>
        <RouterLink class="button primary" :to="lp('download')">
          {{ t("capabilities.cta.button") }} <UiIcon name="download" />
        </RouterLink>
      </div>
    </section>
  </div>
</template>
