<script setup>
import { useI18n } from "vue-i18n";
import PageHero from "../components/PageHero.vue";
import UiIcon from "../components/UiIcon.vue";
import { useLocalePath } from "../composables/useLocalePath.js";
import { comparison } from "../content.js";

const { t } = useI18n();
const lp = useLocalePath();

const scenarios = ["migrations", "regulated", "parallel"];
</script>

<template>
  <div>
    <PageHero
      :eyebrow="t('compare.hero.eyebrow')"
      :title="t('compare.hero.title')"
      :lead="t('compare.hero.lead')"
    />

    <section class="section no-top">
      <div class="container">
        <div class="compare-table">
          <div class="tr head">
            <b>{{ t("compare.table.capability") }}</b>
            <b>{{ t("compare.table.unknownAgent") }}</b>
            <b>{{ t("compare.table.cloudAgent") }}</b>
            <b>{{ t("compare.table.chatTools") }}</b>
          </div>
          <div v-for="row in comparison" :key="row.id" class="tr">
            <strong>{{ t(`comparison.rows.${row.id}`) }}</strong>
            <span v-for="(value, i) in row.values" :key="i" :class="{ yes: value }">
              <UiIcon :name="value ? 'check' : 'close'" :size="17" />
              {{ value ? t("compare.table.included") : t("compare.table.limited") }}
            </span>
          </div>
        </div>
        <p class="table-note">{{ t("compare.table.note") }}</p>
      </div>
    </section>

    <section class="section dark-panel">
      <div class="container">
        <div class="section-head">
          <p class="eyebrow">{{ t("compare.scenarios.eyebrow") }}</p>
          <h2>{{ t("compare.scenarios.title") }}</h2>
        </div>
        <div class="scenario-grid">
          <article v-for="(id, i) in scenarios" :key="id">
            <span>{{ String(i + 1).padStart(2, "0") }}</span>
            <h3>{{ t(`compare.scenarios.${id}.title`) }}</h3>
            <p>{{ t(`compare.scenarios.${id}.text`) }}</p>
          </article>
        </div>
      </div>
    </section>

    <section class="section mini-cta">
      <div class="container">
        <h2>{{ t("compare.cta.title") }}</h2>
        <RouterLink class="button primary" :to="lp('download')">
          {{ t("compare.cta.button") }} <UiIcon name="arrow" class="icon-directional" />
        </RouterLink>
      </div>
    </section>
  </div>
</template>
