<script setup>
import { useI18n } from "vue-i18n";
import UiIcon from "../components/UiIcon.vue";
import { useLocalePath } from "../composables/useLocalePath.js";

const { t } = useI18n();
const lp = useLocalePath();
</script>

<template>
  <section class="not-found">
    <div>
      <span>{{ t("notFound.code") }}</span>
      <h1>{{ t("notFound.title") }}</h1>
      <p>{{ t("notFound.text") }}</p>
      <RouterLink class="button primary large" :to="lp('home')">
        {{ t("notFound.button") }} <UiIcon name="arrow" class="icon-directional" />
      </RouterLink>
    </div>
  </section>
</template>
