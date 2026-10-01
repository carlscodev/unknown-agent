/**
 * Locale-aware internal links.
 *
 * Every `<RouterLink>` in the site must point at the current locale, otherwise
 * clicking a nav item would silently throw the user back into English. This
 * composable returns `lp(pageName)`, which resolves a canonical page id to the
 * right path for the active locale:
 *
 *   lp('product')  // 'en' -> '/product'
 *   lp('product')  // 'es' -> '/es/product'
 *
 * Using page ids rather than raw paths also means the URL scheme can change in
 * one place (`router.js`) without touching any template.
 */

import { useI18n } from "vue-i18n";
import { pathFor } from "../router.js";

export function useLocalePath() {
  const { locale } = useI18n();
  return (pageName) => pathFor(pageName, locale.value);
}
