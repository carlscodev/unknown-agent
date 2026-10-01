/**
 * Locale detection and persistence.
 *
 * Detection order (highest priority first), as specified in the i18n brief:
 *   1. explicit user choice  — passed in by the language switcher
 *   2. URL locale segment    — `/es/product`
 *   3. saved preference      — localStorage
 *   4. Accept-Language       — `navigator.languages`
 *   5. default locale        — `en`
 *
 * PERSISTENCE DECISION: `localStorage`, not a cookie.
 * The site is a static SPA on Vercel with no server able to read a cookie
 * before responding, so a cookie would buy nothing while dragging the site
 * into consent-banner territory under the ePrivacy directive. The URL stays the
 * authoritative, shareable source of truth; localStorage only remembers the
 * choice for a later bare-domain visit. Storage access is fully guarded so the
 * site keeps working in private mode and with storage disabled.
 */

import {
  DEFAULT_LOCALE,
  isSupportedLocale,
  getLocale,
  resolveToSupported,
} from "./locales.js";

export const STORAGE_KEY = "unknown-agent:locale";

/**
 * Reads the first path segment and returns it when it is a shipped locale.
 * `/es/product` -> `es`; `/product` -> `null` (default locale is unprefixed).
 */
export function localeFromPath(pathname) {
  if (typeof pathname !== "string") return null;
  const segment = pathname.replace(/^\/+/, "").split("/")[0];
  if (!segment) return null;
  // Match case-insensitively but always return the canonical casing (`pt-BR`).
  return isSupportedLocale(segment) ? getLocale(segment).code : null;
}

/** Strips a leading locale segment: `/es/product` -> `/product`, `/es` -> `/`. */
export function stripLocaleFromPath(pathname) {
  const locale = localeFromPath(pathname);
  if (!locale) return pathname || "/";
  const rest = pathname.replace(/^\/+/, "").split("/").slice(1).join("/");
  return `/${rest}`;
}

/**
 * Builds the canonical path for `locale`.
 * The default locale is unprefixed; every other locale is prefixed.
 */
export function localizePath(pathname, locale) {
  const bare = stripLocaleFromPath(pathname || "/");
  const normalized = bare.startsWith("/") ? bare : `/${bare}`;
  if (locale === DEFAULT_LOCALE) return normalized;
  return normalized === "/" ? `/${locale}` : `/${locale}${normalized}`;
}

/** Reads the stored preference, or `null`. Never throws. */
export function readStoredLocale(storage = safeStorage()) {
  if (!storage) return null;
  try {
    const saved = storage.getItem(STORAGE_KEY);
    return saved && isSupportedLocale(saved) ? getLocale(saved).code : null;
  } catch {
    return null;
  }
}

/** Persists the preference. Returns `true` on success. Never throws. */
export function storeLocale(locale, storage = safeStorage()) {
  if (!storage || !isSupportedLocale(locale)) return false;
  try {
    storage.setItem(STORAGE_KEY, locale);
    return true;
  } catch {
    // Safari private mode, disabled storage, quota exceeded — all non-fatal.
    return false;
  }
}

/** `localStorage` when usable, otherwise `null`. */
export function safeStorage() {
  try {
    if (typeof localStorage === "undefined") return null;
    const probe = "__ua_i18n_probe__";
    localStorage.setItem(probe, "1");
    localStorage.removeItem(probe);
    return localStorage;
  } catch {
    return null;
  }
}

/** First shipped locale matching the browser's ordered language preferences. */
export function localeFromNavigator(languages) {
  const list =
    languages ??
    (typeof navigator !== "undefined"
      ? navigator.languages?.length
        ? navigator.languages
        : [navigator.language].filter(Boolean)
      : []);

  for (const tag of list ?? []) {
    const match = resolveToSupported(tag);
    if (match) return match;
  }
  return null;
}

/**
 * Applies the full detection order and returns both the winning locale and the
 * reason it won, so callers (and tests) can assert the precedence.
 *
 * @returns {{ locale: string, source: 'explicit'|'url'|'storage'|'navigator'|'default' }}
 */
export function detectLocale({
  explicit = null,
  pathname = typeof location !== "undefined" ? location.pathname : "/",
  storage = safeStorage(),
  languages = undefined,
} = {}) {
  if (explicit && isSupportedLocale(explicit)) {
    return { locale: getLocale(explicit).code, source: "explicit" };
  }

  const fromUrl = localeFromPath(pathname);
  if (fromUrl) return { locale: fromUrl, source: "url" };

  const fromStorage = readStoredLocale(storage);
  if (fromStorage) return { locale: fromStorage, source: "storage" };

  const fromNavigator = localeFromNavigator(languages);
  if (fromNavigator) return { locale: fromNavigator, source: "navigator" };

  return { locale: DEFAULT_LOCALE, source: "default" };
}
