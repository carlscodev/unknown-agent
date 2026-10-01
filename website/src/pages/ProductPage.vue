<script setup>
import { useI18n } from "vue-i18n";
import PageHero from "../components/PageHero.vue";
import UiIcon from "../components/UiIcon.vue";
import { useLocalePath } from "../composables/useLocalePath.js";

const { t } = useI18n();
const lp = useLocalePath();

const stages = ["understand", "plan", "execute", "verify", "recover"];
const surfaces = ["terminal", "web", "pythonApi", "events"];
const principles = ["portable", "local", "policy", "open"];
</script>

<template>
  <div>
    <PageHero
      :eyebrow="t('product.hero.eyebrow')"
      :title="t('product.hero.title')"
      :lead="t('product.hero.lead')"
    >
      <div class="hero-actions center">
        <RouterLink class="button primary large" :to="lp('download')">
          {{ t("product.hero.ctaPrimary") }} <UiIcon name="arrow" class="icon-directional" />
        </RouterLink>
        <RouterLink class="button ghost large" :to="lp('developers')">{{ t("product.hero.ctaSecondary") }}</RouterLink>
      </div>
    </PageHero>

    <section class="section dark-panel">
      <div class="container">
        <p class="eyebrow">{{ t("product.loop.eyebrow") }}</p>
        <div class="stage-list">
          <article v-for="id in stages" :key="id">
            <span>{{ t(`product.stages.${id}.step`) }}</span>
            <h2>{{ t(`product.stages.${id}.title`) }}</h2>
            <p>{{ t(`product.stages.${id}.text`) }}</p>
          </article>
        </div>
      </div>
    </section>

    <section class="section">
      <div class="container split">
        <div>
          <p class="eyebrow">{{ t("product.sovereign.eyebrow") }}</p>
          <h2>{{ t("product.sovereign.title") }}</h2>
          <p class="lede-sm">{{ t("product.sovereign.lead") }}</p>
        </div>
        <div class="principle-stack">
          <div v-for="id in principles" :key="id">
            <b>{{ t(`product.sovereign.${id}Title`) }}</b>
            <span>{{ t(`product.sovereign.${id}Text`) }}</span>
          </div>
        </div>
      </div>
    </section>

    <section class="section">
      <div class="container">
        <div class="section-head">
          <p class="eyebrow">{{ t("product.shared.eyebrow") }}</p>
          <h2>{{ t("product.shared.title") }}</h2>
        </div>
        <div class="four-grid">
          <article v-for="id in surfaces" :key="id">
            <span class="signal"></span>
            <h3>{{ t(`product.surfaces.${id}.title`) }}</h3>
            <p>{{ t(`product.surfaces.${id}.text`) }}</p>
          </article>
        </div>
      </div>
    </section>

    <section class="section mini-cta">
      <div class="container">
        <h2>{{ t("product.cta.title") }}</h2>
        <RouterLink class="button primary" :to="lp('capabilities')">
          {{ t("product.cta.button") }} <UiIcon name="arrow" class="icon-directional" />
        </RouterLink>
      </div>
    </section>
  </div>
</template>
