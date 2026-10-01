/**
 * Locale-aware formatting helpers built on the platform `Intl` APIs.
 *
 * Every helper takes an explicit locale code so it is pure and unit-testable,
 * and maps our locale id to the richer `intl` tag from the registry
 * (`zh-CN` -> `zh-Hans-CN`). Formatter instances are cached: constructing an
 * `Intl.*` formatter is comparatively expensive and these run inside renders.
 *
 * No string concatenation is used for user-visible text anywhere — plurals,
 * genders and variables go through ICU MessageFormat in the locale files, and
 * anything numeric or temporal goes through the helpers below.
 */

import { getLocale, SOURCE_LOCALE } from "./locales.js";

/** Maps a shipped locale code to the tag handed to `Intl`. */
function intlTag(code) {
  return getLocale(code)?.intl ?? getLocale(SOURCE_LOCALE).intl;
}

/** Generic memoiser keyed by locale + serialised options. */
function cached(factory) {
  const store = new Map();
  return (code, options) => {
    const key = `${code}|${JSON.stringify(options ?? {})}`;
    let instance = store.get(key);
    if (!instance) {
      instance = factory(intlTag(code), options);
      store.set(key, instance);
    }
    return instance;
  };
}

const dateFormatter = cached((tag, options) => new Intl.DateTimeFormat(tag, options));
const numberFormatter = cached((tag, options) => new Intl.NumberFormat(tag, options));
const listFormatter = cached((tag, options) => new Intl.ListFormat(tag, options));
const relativeFormatter = cached(
  (tag, options) => new Intl.RelativeTimeFormat(tag, { numeric: "auto", ...options }),
);
const pluralRules = cached((tag, options) => new Intl.PluralRules(tag, options));

/**
 * Formats a date. Accepts a `Date`, a timestamp, or an ISO string.
 * @example formatDate('de', '2026-09-21') // '21. September 2026'
 */
export function formatDate(code, value, options = { dateStyle: "long" }) {
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return dateFormatter(code, options).format(date);
}

/** Formats a number using the locale's digit grouping and decimal separator. */
export function formatNumber(code, value, options) {
  if (typeof value !== "number" || Number.isNaN(value)) return "";
  return numberFormatter(code, options).format(value);
}

/** Formats a currency amount. ISO 4217 code required. */
export function formatCurrency(code, value, currency, options) {
  if (typeof value !== "number" || Number.isNaN(value)) return "";
  return numberFormatter(code, {
    style: "currency",
    currency,
    ...options,
  }).format(value);
}

/**
 * Formats a relative time, e.g. `-3` + `'day'` -> "3 days ago" / "hace 3 días".
 */
export function formatRelativeTime(code, value, unit, options) {
  if (typeof value !== "number" || Number.isNaN(value)) return "";
  return relativeFormatter(code, options).format(value, unit);
}

/**
 * Formats a list with the locale's own conjunction and separators.
 * @example formatList('es', ['a','b','c']) // 'a, b y c'
 */
export function formatList(code, items, options = { style: "long", type: "conjunction" }) {
  const list = (items ?? []).filter((item) => typeof item === "string" && item !== "");
  if (list.length === 0) return "";
  return listFormatter(code, options).format(list);
}

/**
 * The CLDR plural category a count falls into for a locale.
 * Exposed so tests can pin plural behaviour for languages with more than the
 * two English forms (Arabic has six: zero/one/two/few/many/other).
 */
export function pluralCategory(code, count, options) {
  return pluralRules(code, options).select(count);
}

/** Every plural category a locale actually uses, in CLDR order. */
export function pluralCategories(code, options) {
  const order = ["zero", "one", "two", "few", "many", "other"];
  const supported = pluralRules(code, options).resolvedOptions().pluralCategories;
  return order.filter((category) => supported.includes(category));
}
