<script setup>
import { useI18n } from "vue-i18n";
import PageHero from "../components/PageHero.vue";
import { github, phases } from "../content.js";

const { t } = useI18n();

const detail = ["foundation", "codingCore", "extensible", "omnichannel"];
const itemKeys = ["i1", "i2", "i3", "i4", "i5"];
</script>

<template>
  <div>
    <PageHero
      :eyebrow="t('roadmap.hero.eyebrow')"
      :title="t('roadmap.hero.title')"
      :lead="t('roadmap.hero.lead')"
    />

    <section class="section no-top">
      <div class="container">
        <div class="roadmap-line">
          <article v-for="(phase, i) in phases" :key="phase.id" :class="{ active: i === 0 }">
            <div class="road-dot"></div>
            <span>{{ phase.n }} · {{ t(`phases.${phase.id}.status`) }}</span>
            <h2>{{ t(`phases.${phase.id}.title`) }}</h2>
            <p>{{ t(`phases.${phase.id}.text`) }}</p>
          </article>
        </div>
      </div>
    </section>

    <section class="section dark-panel">
      <div class="container">
        <div class="section-head">
          <p class="eyebrow">{{ t("roadmap.map.eyebrow") }}</p>
          <h2>{{ t("roadmap.map.title") }}</h2>
        </div>
        <div class="road-detail">
          <article v-for="(id, i) in detail" :key="id">
            <div>
              <span>{{ String(i + 1).padStart(2, "0") }}</span>
              <h3>{{ t(`roadmap.detail.${id}.title`) }}</h3>
            </div>
            <ul>
              <li v-for="key in itemKeys" :key="key">{{ t(`roadmap.detail.${id}.${key}`) }}</li>
            </ul>
          </article>
        </div>
      </div>
    </section>

    <section class="section">
      <div class="container narrow center-copy">
        <p class="eyebrow">{{ t("roadmap.principle.eyebrow") }}</p>
        <h2>{{ t("roadmap.principle.title") }}</h2>
        <p class="lede">{{ t("roadmap.principle.lead") }}</p>
        <a class="button ghost" :href="`${github}/issues`" target="_blank" rel="noreferrer">
          {{ t("roadmap.principle.button") }}
        </a>
      </div>
    </section>
  </div>
</template>
