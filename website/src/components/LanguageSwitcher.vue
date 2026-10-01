<script setup>
/**
 * Accessible language switcher.
 *
 * Implements the WAI-ARIA collapsible listbox pattern:
 *   - the trigger is a <button> with aria-haspopup="listbox", aria-expanded
 *     and aria-controls
 *   - the popup is role="listbox" with roving aria-activedescendant
 *   - options are role="option" with aria-selected
 *
 * Keyboard: Enter/Space/ArrowDown/ArrowUp open the list, Arrow keys move,
 * Home/End jump, Enter/Space commit, Escape closes and returns focus, Tab
 * closes. Typing a letter jumps to the next language starting with it.
 *
 * Languages are listed by endonym (Español, 日本語, العربية) because a user
 * who cannot read the current language must still recognise their own. Flags
 * are deliberately not used: they denote countries, not languages.
 *
 * Changing language performs a router navigation to the same page in the new
 * locale. The messages for that locale are loaded in the router's `beforeEach`
 * guard before the view renders, so there is no flash of untranslated content.
 */
import { computed, nextTick, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
import { LOCALES, getLocale } from "../i18n/locales.js";
import { pathFor } from "../router.js";
import UiIcon from "./UiIcon.vue";

const { t, locale } = useI18n();
const route = useRoute();
const router = useRouter();

const open = ref(false);
const activeIndex = ref(0);
const triggerEl = ref(null);
const listEl = ref(null);
const rootEl = ref(null);

const current = computed(() => getLocale(locale.value) ?? LOCALES[0]);
const currentIndex = computed(() =>
  Math.max(
    0,
    LOCALES.findIndex((item) => item.code === current.value.code),
  ),
);

const optionId = (index) => `ua-lang-option-${index}`;

async function openList() {
  open.value = true;
  activeIndex.value = currentIndex.value;
  await nextTick();
  listEl.value?.focus();
}

function closeList({ focusTrigger = true } = {}) {
  if (!open.value) return;
  open.value = false;
  if (focusTrigger) triggerEl.value?.focus();
}

function toggle() {
  if (open.value) closeList();
  else openList();
}

async function choose(index) {
  const target = LOCALES[index];
  if (!target) return;
  closeList();
  if (target.code === current.value.code) return;
  const page = route.meta?.page ?? "home";
  await router.push({
    path: pathFor(page, target.code),
    query: route.query,
    hash: route.hash,
  });
}

function move(delta) {
  const count = LOCALES.length;
  activeIndex.value = (activeIndex.value + delta + count) % count;
}

/** Type-ahead: jump to the next locale whose endonym or English name matches. */
let typeBuffer = "";
let typeTimer = null;
function typeAhead(key) {
  typeBuffer += key.toLowerCase();
  clearTimeout(typeTimer);
  typeTimer = setTimeout(() => (typeBuffer = ""), 600);
  const found = LOCALES.findIndex(
    (item) =>
      item.name.toLowerCase().startsWith(typeBuffer) ||
      item.english.toLowerCase().startsWith(typeBuffer),
  );
  if (found >= 0) activeIndex.value = found;
}

function onTriggerKeydown(event) {
  if (["Enter", " ", "ArrowDown", "ArrowUp"].includes(event.key)) {
    event.preventDefault();
    openList();
  }
}

function onListKeydown(event) {
  switch (event.key) {
    case "ArrowDown":
      event.preventDefault();
      move(1);
      break;
    case "ArrowUp":
      event.preventDefault();
      move(-1);
      break;
    case "Home":
      event.preventDefault();
      activeIndex.value = 0;
      break;
    case "End":
      event.preventDefault();
      activeIndex.value = LOCALES.length - 1;
      break;
    case "Enter":
    case " ":
      event.preventDefault();
      choose(activeIndex.value);
      break;
    case "Escape":
      event.preventDefault();
      closeList();
      break;
    case "Tab":
      closeList({ focusTrigger: false });
      break;
    default:
      if (event.key.length === 1 && !event.metaKey && !event.ctrlKey && !event.altKey) {
        typeAhead(event.key);
      }
  }
}

function onFocusOut(event) {
  if (!rootEl.value?.contains(event.relatedTarget)) closeList({ focusTrigger: false });
}

// Close when the route changes (e.g. the user picked a language).
watch(() => route.fullPath, () => closeList({ focusTrigger: false }));
</script>

<template>
  <div ref="rootEl" class="lang-switcher" @focusout="onFocusOut">
    <button
      ref="triggerEl"
      type="button"
      class="lang-trigger"
      :aria-label="`${t('lang.switcherLabel')} — ${t('lang.current', { language: current.name })}`"
      aria-haspopup="listbox"
      :aria-expanded="open"
      aria-controls="ua-lang-list"
      @click="toggle"
      @keydown="onTriggerKeydown"
    >
      <UiIcon name="globe" :size="16" />
      <span class="lang-name">{{ current.name }}</span>
      <UiIcon name="chevron" :size="13" class="lang-chevron" :class="{ up: open }" />
    </button>

    <ul
      v-show="open"
      id="ua-lang-list"
      ref="listEl"
      class="lang-list"
      role="listbox"
      tabindex="-1"
      :aria-label="t('lang.select')"
      :aria-activedescendant="open ? optionId(activeIndex) : undefined"
      @keydown="onListKeydown"
    >
      <li
        v-for="(item, index) in LOCALES"
        :id="optionId(index)"
        :key="item.code"
        class="lang-option"
        :class="{ active: index === activeIndex, selected: item.code === current.code }"
        role="option"
        :aria-selected="item.code === current.code"
        :lang="item.htmlLang"
        :dir="item.dir"
        @click="choose(index)"
        @mousemove="activeIndex = index"
      >
        <span class="lang-option-name">{{ item.name }}</span>
        <UiIcon v-if="item.code === current.code" name="check" :size="14" />
      </li>
    </ul>
  </div>
</template>
