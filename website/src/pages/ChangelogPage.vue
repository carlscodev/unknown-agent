<script setup>
/**
 * Changelog with a partial-translation strategy.
 *
 * The changelog grows with every release, so requiring all 20 entries in all
 * nine languages before a release would make translation a release blocker.
 * Instead each locale ships the five newest entries (the ones a visitor is
 * most likely to care about) and older entries fall back to English with a
 * visible notice — never a silent swap.
 *
 * The canonical entry list and its order always come from the English source,
 * so a locale can neither reorder nor drop entries. Whether a given entry is
 * translated is decided by looking at the locale's OWN message object via
 * `getLocaleMessage`, not via `t()`: `t()` would transparently resolve the
 * fallback and report every entry as translated.
 */
import { computed } from "vue";
import { useI18n } from "vue-i18n";
import PageHero from "../components/PageHero.vue";
import { DEFAULT_LOCALE, getLocale } from "../i18n/locales.js";
import { github } from "../content.js";

const { t, locale, getLocaleMessage } = useI18n();

/** Canonical ids in English source order — the single ordering authority. */
const canonicalIds = computed(() =>
  Object.keys(getLocaleMessage(DEFAULT_LOCALE)?.changelog?.entries ?? {}),
);

/** The native name of the active language, used in the fallback notice. */
const languageName = computed(() => getLocale(locale.value)?.name ?? locale.value);

const entries = computed(() => {
  const own = getLocaleMessage(locale.value)?.changelog?.entries ?? {};
  const english = getLocaleMessage(DEFAULT_LOCALE)?.changelog?.entries ?? {};
  return canonicalIds.value.map((id) => {
    const translated = Boolean(own[id]);
    const source = translated ? own[id] : english[id];
    return {
      id,
      translated,
      date: source.date,
      version: source.version,
      title: source.title,
      items: source.items ?? [],
    };
  });
});
</script>

<template>
  <div>
    <PageHero
      :eyebrow="t('changelog.hero.eyebrow')"
      :title="t('changelog.hero.title')"
      :lead="t('changelog.hero.lead')"
    >
      <!-- Pluralized count, driven by the locale's own CLDR plural rule. -->
      <p class="entry-count">{{ t("changelog.entryCount", entries.length, { count: entries.length }) }}</p>
    </PageHero>

    <section class="section no-top">
      <div class="container narrow">
        <div class="change-list">
          <article
            v-for="entry in entries"
            :key="entry.id"
            :class="{ 'is-untranslated': !entry.translated }"
          >
            <div class="change-meta">
              <span>{{ entry.date }}</span>
              <code dir="ltr">{{ entry.version }}</code>
            </div>
            <div>
              <h2>
                {{ entry.title }}
                <span
                  v-if="!entry.translated"
                  class="untranslated-badge"
                  lang="en"
                  dir="ltr"
                  :title="t('changelog.untranslatedNotice', { language: languageName })"
                >{{ t("changelog.untranslatedLabel") }}</span>
              </h2>
              <p v-if="!entry.translated" class="untranslated-notice">
                {{ t("changelog.untranslatedNotice", { language: languageName }) }}
              </p>
              <!-- English fallback content is marked up as English for screen readers. -->
              <ul :lang="entry.translated ? undefined : 'en'" :dir="entry.translated ? undefined : 'ltr'">
                <li v-for="(item, i) in entry.items" :key="i">{{ item }}</li>
              </ul>
            </div>
          </article>
        </div>

        <div class="history-link">
          <a :href="`${github}/commits/main`" target="_blank" rel="noreferrer">
            {{ t("changelog.historyLink") }}
          </a>
        </div>
      </div>
    </section>
  </div>
</template>
