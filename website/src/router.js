import { createRouter, createWebHistory } from "vue-router";
import HomePage from "./pages/HomePage.vue";
import ProductPage from "./pages/ProductPage.vue";
import CapabilitiesPage from "./pages/CapabilitiesPage.vue";
import SecurityPage from "./pages/SecurityPage.vue";
import DevelopersPage from "./pages/DevelopersPage.vue";
import DownloadPage from "./pages/DownloadPage.vue";
import ComparePage from "./pages/ComparePage.vue";
import RoadmapPage from "./pages/RoadmapPage.vue";
import ChangelogPage from "./pages/ChangelogPage.vue";
import CompanyPage from "./pages/CompanyPage.vue";
import NotFoundPage from "./pages/NotFoundPage.vue";

import { DEFAULT_LOCALE, LOCALE_CODES, getLocale, isSupportedLocale } from "./i18n/locales.js";
import { detectLocale, localeFromPath, storeLocale, stripLocaleFromPath } from "./i18n/detect.js";
import { setLocale } from "./i18n/index.js";

/**
 * Canonical page definitions.
 *
 * `name` is the stable route id used across locales, `path` is the
 * locale-independent path, and `metaKey` points at the `meta.*` namespace that
 * supplies the localized <title> and description.
 */
export const PAGES = [
  { name: "home", path: "", component: HomePage, metaKey: "home" },
  { name: "product", path: "product", component: ProductPage, metaKey: "product" },
  { name: "capabilities", path: "capabilities", component: CapabilitiesPage, metaKey: "capabilities" },
  { name: "security", path: "security", component: SecurityPage, metaKey: "security" },
  { name: "developers", path: "developers", component: DevelopersPage, metaKey: "developers" },
  { name: "download", path: "download", component: DownloadPage, metaKey: "download" },
  { name: "compare", path: "compare", component: ComparePage, metaKey: "compare" },
  { name: "roadmap", path: "roadmap", component: RoadmapPage, metaKey: "roadmap" },
  { name: "changelog", path: "changelog", component: ChangelogPage, metaKey: "changelog" },
  { name: "company", path: "company", component: CompanyPage, metaKey: "company" },
];

/** Every non-default locale, used to build the prefixed route group. */
const PREFIXED = LOCALE_CODES.filter((code) => code !== DEFAULT_LOCALE);

/**
 * URL STRATEGY
 *
 * The default locale stays unprefixed so every URL that exists today keeps
 * working and keeps its accumulated SEO value:
 *
 *   /product        -> English (canonical)
 *   /es/product     -> Spanish
 *   /zh-CN/product  -> Simplified Chinese
 *
 * Route names are suffixed with the locale (`product`, `product__es`) so
 * `router.resolve({ name })` can target a specific locale without string
 * surgery on paths.
 */
export function buildRoutes() {
  const routes = [];

  for (const page of PAGES) {
    routes.push({
      name: page.name,
      path: `/${page.path}`,
      component: page.component,
      meta: { metaKey: page.metaKey, locale: DEFAULT_LOCALE, page: page.name },
    });
  }

  for (const locale of PREFIXED) {
    for (const page of PAGES) {
      routes.push({
        name: `${page.name}__${locale}`,
        path: `/${locale}${page.path ? `/${page.path}` : ""}`,
        component: page.component,
        meta: { metaKey: page.metaKey, locale, page: page.name },
      });
    }
  }

  // A locale prefix followed by an unknown path 404s *within* that locale, so
  // the user keeps their language instead of being dumped back into English.
  for (const locale of PREFIXED) {
    routes.push({
      name: `notFound__${locale}`,
      path: `/${locale}/:pathMatch(.*)*`,
      component: NotFoundPage,
      meta: { metaKey: "notFound", locale, page: "notFound" },
    });
  }

  routes.push({
    name: "notFound",
    path: "/:pathMatch(.*)*",
    component: NotFoundPage,
    meta: { metaKey: "notFound", locale: DEFAULT_LOCALE, page: "notFound" },
  });

  return routes;
}

const router = createRouter({
  history: createWebHistory(),
  routes: buildRoutes(),
  scrollBehavior(to, from, saved) {
    if (saved) return saved;
    if (to.hash) return { el: to.hash, behavior: "smooth" };
    return { top: 0 };
  },
});

/**
 * Resolves the path for `pageName` in `locale`, preserving hash and query.
 * Used by the language switcher and by the hreflang tag generator.
 */
export function pathFor(pageName, locale) {
  const page = PAGES.find((candidate) => candidate.name === pageName);
  if (!page) return locale === DEFAULT_LOCALE ? "/" : `/${locale}`;
  if (locale === DEFAULT_LOCALE) return `/${page.path}`;
  return `/${locale}${page.path ? `/${page.path}` : ""}`;
}

/**
 * Before each navigation: decide the locale and make sure its messages are
 * loaded BEFORE the component renders, so there is no flash of English.
 */
router.beforeEach(async (to) => {
  const urlLocale = localeFromPath(to.path);

  // A bare visit to "/" (or any unprefixed path) is the only case where we are
  // allowed to redirect based on a stored or browser preference. Prefixed URLs
  // are explicit and must never be overridden — deep links have to be stable.
  if (!urlLocale) {
    const { locale, source } = detectLocale({ pathname: to.path });
    if (locale !== DEFAULT_LOCALE && (source === "storage" || source === "navigator")) {
      const page = to.meta?.page ?? "home";
      // Only redirect for real pages; a 404 stays a 404.
      if (page !== "notFound") {
        await setLocale(locale);
        return { path: pathFor(page, locale), query: to.query, hash: to.hash, replace: true };
      }
    }
  }

  const target = urlLocale && isSupportedLocale(urlLocale) ? urlLocale : DEFAULT_LOCALE;
  await setLocale(target);
  return true;
});

/** After navigation: remember the locale the user is actually looking at. */
router.afterEach((to) => {
  const locale = to.meta?.locale ?? DEFAULT_LOCALE;
  if (getLocale(locale)) storeLocale(locale);
});

export { stripLocaleFromPath };
export default router;
