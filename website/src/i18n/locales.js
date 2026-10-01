/**
 * Single source of truth for every locale the public website ships.
 *
 * A locale only appears here when its message file is complete and validated by
 * `npm run i18n:check`. Incomplete languages must NOT be listed: the language
 * switcher renders this array verbatim.
 *
 * Fields
 * - `code`      Locale id used in URLs and message filenames (`/es/product`).
 * - `name`      Endonym shown in the switcher (native name, never translated).
 * - `english`   English exonym, used for `aria-label` and tooling output.
 * - `dir`       Writing direction applied to `<html dir>`.
 * - `htmlLang`  BCP-47 tag applied to `<html lang>`.
 * - `ogLocale`  Open Graph `og:locale` value (underscore form).
 * - `intl`      Tag handed to `Intl.*` formatters; may be more specific than `code`.
 */

/** Locale used as the translation source and as the final fallback. */
export const SOURCE_LOCALE = "en";

/**
 * The default locale is served WITHOUT a URL prefix so every pre-existing URL
 * (`/product`, `/security`, ...) keeps working and keeps its SEO value.
 */
export const DEFAULT_LOCALE = SOURCE_LOCALE;

export const LOCALES = [
  {
    code: "en",
    name: "English",
    english: "English",
    dir: "ltr",
    htmlLang: "en",
    ogLocale: "en_US",
    intl: "en-US",
  },
  {
    code: "es",
    name: "Español",
    english: "Spanish",
    dir: "ltr",
    htmlLang: "es",
    ogLocale: "es_ES",
    intl: "es-ES",
  },
  {
    code: "pt-BR",
    name: "Português (Brasil)",
    english: "Portuguese (Brazil)",
    dir: "ltr",
    htmlLang: "pt-BR",
    ogLocale: "pt_BR",
    intl: "pt-BR",
  },
  {
    code: "fr",
    name: "Français",
    english: "French",
    dir: "ltr",
    htmlLang: "fr",
    ogLocale: "fr_FR",
    intl: "fr-FR",
  },
  {
    code: "de",
    name: "Deutsch",
    english: "German",
    dir: "ltr",
    htmlLang: "de",
    ogLocale: "de_DE",
    intl: "de-DE",
  },
  {
    code: "it",
    name: "Italiano",
    english: "Italian",
    dir: "ltr",
    htmlLang: "it",
    ogLocale: "it_IT",
    intl: "it-IT",
  },
  {
    code: "ja",
    name: "日本語",
    english: "Japanese",
    dir: "ltr",
    htmlLang: "ja",
    ogLocale: "ja_JP",
    intl: "ja-JP",
  },
  {
    code: "zh-CN",
    name: "简体中文",
    english: "Chinese (Simplified)",
    dir: "ltr",
    htmlLang: "zh-Hans",
    ogLocale: "zh_CN",
    intl: "zh-Hans-CN",
  },
  {
    code: "ar",
    name: "العربية",
    english: "Arabic",
    dir: "rtl",
    htmlLang: "ar",
    ogLocale: "ar_AR",
    intl: "ar",
  },
];

/** Locale codes in switcher order. */
export const LOCALE_CODES = LOCALES.map((locale) => locale.code);

/** Lookup table so callers never scan the array. */
const BY_CODE = new Map(LOCALES.map((locale) => [locale.code.toLowerCase(), locale]));

/**
 * Explicit fallback chains for regional variants we do not ship a file for.
 * Keys are lowercase BCP-47 tags. Every chain terminates at the source locale.
 */
export const FALLBACK_CHAIN = {
  // Spanish regional variants resolve to European Spanish, then English.
  "es-mx": ["es", "en"],
  "es-ar": ["es", "en"],
  "es-co": ["es", "en"],
  "es-cl": ["es", "en"],
  "es-ve": ["es", "en"],
  "es-419": ["es", "en"],
  // Portuguese: we ship Brazilian Portuguese, so European Portuguese degrades to it.
  pt: ["pt-BR", "en"],
  "pt-pt": ["pt-BR", "en"],
  // Chinese: Simplified is shipped; Traditional variants fall back to it before English.
  zh: ["zh-CN", "en"],
  "zh-hans": ["zh-CN", "en"],
  "zh-sg": ["zh-CN", "en"],
  "zh-tw": ["zh-CN", "en"],
  "zh-hk": ["zh-CN", "en"],
  "zh-hant": ["zh-CN", "en"],
  // French / German / Italian regional variants.
  "fr-ca": ["fr", "en"],
  "fr-be": ["fr", "en"],
  "fr-ch": ["fr", "en"],
  "de-at": ["de", "en"],
  "de-ch": ["de", "en"],
  "it-ch": ["it", "en"],
  // Arabic regional variants all resolve to Modern Standard Arabic.
  "ar-eg": ["ar", "en"],
  "ar-sa": ["ar", "en"],
  "ar-ae": ["ar", "en"],
  "ar-ma": ["ar", "en"],
  "ar-dz": ["ar", "en"],
  // English regional variants.
  "en-gb": ["en"],
  "en-us": ["en"],
  "en-au": ["en"],
  "en-ca": ["en"],
};

/** True when `code` is a locale we actually ship a complete message file for. */
export function isSupportedLocale(code) {
  if (typeof code !== "string" || code === "") return false;
  return BY_CODE.has(code.toLowerCase());
}

/** Returns the locale descriptor for `code`, or `undefined`. */
export function getLocale(code) {
  if (typeof code !== "string") return undefined;
  return BY_CODE.get(code.toLowerCase());
}

/** Writing direction for `code`; defaults to `ltr` for unknown input. */
export function dirFor(code) {
  return getLocale(code)?.dir ?? "ltr";
}

/** True when `code` is written right-to-left. */
export function isRtl(code) {
  return dirFor(code) === "rtl";
}

/**
 * Resolves any BCP-47 tag to a shipped locale code, or `null` when nothing matches.
 *
 * Resolution order:
 *   1. exact match                      (`pt-BR` -> `pt-BR`)
 *   2. explicit chain in FALLBACK_CHAIN (`es-MX` -> `es`)
 *   3. primary subtag                   (`de-LI` -> `de`)
 */
export function resolveToSupported(tag) {
  if (typeof tag !== "string" || tag === "") return null;
  const lower = tag.toLowerCase();

  if (BY_CODE.has(lower)) return BY_CODE.get(lower).code;

  const chain = FALLBACK_CHAIN[lower];
  if (chain) {
    for (const candidate of chain) {
      if (isSupportedLocale(candidate)) return getLocale(candidate).code;
    }
  }

  const primary = lower.split("-")[0];
  if (primary && BY_CODE.has(primary)) return BY_CODE.get(primary).code;

  return null;
}

/**
 * Full fallback chain for a shipped locale, always ending at the source locale.
 * Shaped for vue-i18n's `fallbackLocale` map.
 */
export function fallbackChainFor(code) {
  const lower = String(code ?? "").toLowerCase();
  const explicit = FALLBACK_CHAIN[lower];
  if (explicit) return explicit.filter(isSupportedLocale);

  const primary = lower.split("-")[0];
  if (primary !== lower && isSupportedLocale(primary)) {
    return [getLocale(primary).code, SOURCE_LOCALE];
  }
  return [SOURCE_LOCALE];
}

/** `fallbackLocale` map consumed by `createI18n`. */
export function buildFallbackLocaleMap() {
  const map = {};
  for (const locale of LOCALES) {
    if (locale.code === SOURCE_LOCALE) continue;
    map[locale.code] = fallbackChainFor(locale.code);
  }
  for (const [tag, chain] of Object.entries(FALLBACK_CHAIN)) {
    map[tag] = chain.filter(isSupportedLocale);
  }
  map.default = [SOURCE_LOCALE];
  return map;
}
