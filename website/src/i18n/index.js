/**
 * i18n core.
 *
 * Replaces the previous `src/i18n/index.js`, which was dead code: it imported
 * `./locales/*.json` files that never existed, `vue-i18n` was not a dependency,
 * `main.js` never imported it, and `onLanguageChanged` is not a vue-i18n API.
 * Nothing referenced that module, so removing it changes no behaviour.
 *
 * Design:
 * - Only the active locale's messages are in memory. Every locale is a separate
 *   lazily-imported chunk (`import.meta.glob`), so adding languages does not
 *   grow the initial bundle.
 * - The source locale (`en`) is bundled eagerly: it is the terminal fallback,
 *   so it must be present before any other locale can render a missing key.
 * - Missing keys warn loudly in dev and are counted so CI/tests can fail on them.
 */

import { createI18n } from "vue-i18n";
import en from "./locales/en.json";
import {
  DEFAULT_LOCALE,
  SOURCE_LOCALE,
  buildFallbackLocaleMap,
  dirFor,
  getLocale,
  isSupportedLocale,
} from "./locales.js";

/**
 * Lazy chunks for every non-source locale.
 * `eager: false` keeps each JSON file in its own async chunk.
 */
// Negative glob patterns are resolved by Vite at build time, so `en.json` is
// never emitted as a dynamic chunk — it is statically imported above as the
// terminal fallback and must be present before any other locale can render.
const localeLoaders = import.meta.glob(["./locales/*.json", "!./locales/en.json"]);

/** Missing keys seen at runtime; drained by tests and the dev overlay. */
const missingKeys = [];

export function getMissingKeys() {
  return [...missingKeys];
}

export function clearMissingKeys() {
  missingKeys.length = 0;
}

const isDev = Boolean(import.meta.env?.DEV);

/**
 * vue-i18n missing-key handler.
 *
 * Returning `undefined` lets vue-i18n continue down the fallback chain, so the
 * user still sees real English copy instead of a raw key. The miss is always
 * recorded; in dev it is also warned about so it cannot pass unnoticed.
 */
function onMissing(locale, key) {
  const record = { locale, key };
  missingKeys.push(record);
  if (isDev) {
    console.warn(
      `[i18n] missing key "${key}" for locale "${locale}" — falling back. ` +
        `Add it to src/i18n/locales/${locale}.json and run \`npm run i18n:check\`.`,
    );
  }
  return undefined;
}

/**
 * CLDR plural categories in canonical order. Pipe-separated plural messages in
 * the locale files are written in exactly this order, omitting the categories
 * a language does not use.
 */
const CLDR_ORDER = ["zero", "one", "two", "few", "many", "other"];

const pluralRulesCache = new Map();

function rulesFor(intlTag) {
  let rules = pluralRulesCache.get(intlTag);
  if (!rules) {
    rules = new Intl.PluralRules(intlTag);
    pluralRulesCache.set(intlTag, rules);
  }
  return rules;
}

/**
 * Builds a vue-i18n plural resolver backed by `Intl.PluralRules`, so languages
 * with more than two forms select correctly. Arabic, for example, uses all six
 * categories: 0 -> zero, 1 -> one, 2 -> two, 3-10 -> few, 11-99 -> many,
 * 100+ -> other. vue-i18n's built-in rule only knows the English two-form case.
 */
function intlPluralRule(intlTag) {
  return (choice, choicesLength) => {
    const rules = rulesFor(intlTag);
    const used = CLDR_ORDER.filter((category) =>
      rules.resolvedOptions().pluralCategories.includes(category),
    );
    const category = rules.select(choice);
    const index = used.indexOf(category);
    // Clamp so a message with fewer forms than the language uses still renders
    // the closest available one instead of an empty string.
    if (index < 0 || index >= choicesLength) return Math.max(0, choicesLength - 1);
    return index;
  };
}

/** Per-locale plural resolvers for every language whose rules differ from English. */
const pluralRules = {
  ar: intlPluralRule("ar"),
  // Japanese and Chinese have a single CLDR category ("other"); the messages
  // still ship a zero form for readable empty states, so resolve explicitly.
  ja: (choice, choicesLength) => (choice === 0 ? 0 : Math.min(1, choicesLength - 1)),
  "zh-CN": (choice, choicesLength) => (choice === 0 ? 0 : Math.min(1, choicesLength - 1)),
};

export const i18n = createI18n({
  legacy: false,
  globalInjection: true,
  locale: DEFAULT_LOCALE,
  fallbackLocale: buildFallbackLocaleMap(),
  messages: { [SOURCE_LOCALE]: en },
  pluralRules,
  missing: onMissing,
  // Fallback is expected for untranslated long-form content (changelog entries),
  // so the per-miss console noise from vue-i18n itself is suppressed; our own
  // `missing` handler above still reports every real miss.
  fallbackWarn: false,
  missingWarn: false,
});

/** Locales whose messages are already loaded. */
const loaded = new Set([SOURCE_LOCALE]);

export function isLocaleLoaded(code) {
  return loaded.has(code);
}

/**
 * Loads a locale's messages if not already present. Idempotent.
 * Resolves to the locale actually loaded, or the source locale on failure.
 */
export async function loadLocaleMessages(code) {
  if (!isSupportedLocale(code)) return SOURCE_LOCALE;
  const canonical = getLocale(code).code;
  if (loaded.has(canonical)) return canonical;

  const loader = localeLoaders[`./locales/${canonical}.json`];
  if (!loader) {
    console.error(`[i18n] no message file for locale "${canonical}"`);
    return SOURCE_LOCALE;
  }

  try {
    const module = await loader();
    i18n.global.setLocaleMessage(canonical, module.default ?? module);
    loaded.add(canonical);
    return canonical;
  } catch (error) {
    // A failed chunk must never blank the page: stay on the source locale.
    console.error(`[i18n] failed to load locale "${canonical}"`, error);
    return SOURCE_LOCALE;
  }
}

/**
 * Switches the active locale: loads messages, updates vue-i18n, and syncs the
 * document's `lang`/`dir` so assistive tech and the CSS logical properties
 * follow immediately. Does NOT touch the URL — the router owns that.
 */
export async function setLocale(code) {
  const active = await loadLocaleMessages(code);
  i18n.global.locale.value = active;
  applyDocumentLocale(active);
  return active;
}

/** Mirrors the active locale onto `<html lang>` and `<html dir>`. */
export function applyDocumentLocale(code) {
  if (typeof document === "undefined") return;
  const locale = getLocale(code);
  if (!locale) return;
  const root = document.documentElement;
  root.setAttribute("lang", locale.htmlLang);
  root.setAttribute("dir", locale.dir);
  // Exposed as a hook for direction-specific CSS that logical properties
  // cannot express (e.g. mirroring a directional glyph).
  root.dataset.dir = locale.dir;
}

export { dirFor };
export default i18n;
