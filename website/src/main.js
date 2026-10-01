import { createApp } from "vue";
import App from "./App.vue";
import router from "./router.js";
import i18n, { applyDocumentLocale } from "./i18n/index.js";
import { DEFAULT_LOCALE } from "./i18n/locales.js";
import "./styles.css";

// Set lang/dir before the first paint so RTL does not flash as LTR.
applyDocumentLocale(DEFAULT_LOCALE);

createApp(App).use(i18n).use(router).mount("#app");
