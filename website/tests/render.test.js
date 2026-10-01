/**
 * Smoke test: the real application must mount and render localized copy in
 * every shipped locale, through the real router and the real message files.
 * This is the test that makes "the site works in nine languages" a claim with
 * evidence rather than an assertion.
 */
import { describe, expect, it, beforeEach } from "vitest";
import { mount } from "@vue/test-utils";
import { createRouter, createMemoryHistory } from "vue-router";
import App from "../src/App.vue";
import i18n, { setLocale } from "../src/i18n/index.js";
import { buildRoutes } from "../src/router.js";
import { LOCALES } from "../src/i18n/locales.js";
import en from "../src/i18n/locales/en.json";

function makeRouter() {
  const router = createRouter({ history: createMemoryHistory(), routes: buildRoutes() });
  router.beforeEach(async (to) => {
    await setLocale(to.meta.locale);
  });
  return router;
}

describe("application renders in every locale", () => {
  let router;
  beforeEach(() => {
    router = makeRouter();
  });

  for (const locale of LOCALES) {
    it(`renders the home page in ${locale.code}`, async () => {
      const path = locale.code === "en" ? "/" : `/${locale.code}`;
      router.push(path);
      await router.isReady();
      const wrapper = mount(App, { global: { plugins: [i18n, router] } });
      await new Promise((resolve) => setTimeout(resolve, 0));

      const messages = i18n.global.getLocaleMessage(locale.code);
      expect(messages.home.hero.lead, `${locale.code} must have its own hero copy`).toBeTruthy();
      expect(wrapper.text()).toContain(messages.home.hero.lead);
      expect(document.documentElement.getAttribute("lang")).toBe(locale.htmlLang);
      expect(document.documentElement.getAttribute("dir")).toBe(locale.dir);

      // No locale other than English may render the English hero lead.
      if (locale.code !== "en") {
        expect(wrapper.text()).not.toContain(en.home.hero.lead);
      }
      wrapper.unmount();
    });
  }
});
