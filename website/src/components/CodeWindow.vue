<script setup>
import { ref } from "vue";
import { useI18n } from "vue-i18n";
import UiIcon from "./UiIcon.vue";

const props = defineProps({
  command: { type: String, required: true },
  /** Pre-translated label. Defaults to the localized word for "terminal". */
  label: { type: String, default: "" },
});

const { t } = useI18n();
const copied = ref(false);

async function copy() {
  await navigator.clipboard.writeText(props.command);
  copied.value = true;
  setTimeout(() => (copied.value = false), 1600);
}
</script>

<template>
  <div class="code-window">
    <div class="code-bar">
      <span><i></i><i></i><i></i></span>
      <b>{{ label || t("common.terminal") }}</b>
      <button type="button" :aria-label="copied ? t('common.copied') : t('common.copyCommand')" @click="copy">
        <UiIcon :name="copied ? 'check' : 'copy'" :size="15" />
        {{ copied ? t("common.copied") : t("common.copy") }}
      </button>
    </div>
    <!-- The command itself is a literal CLI invocation and is never translated. -->
    <pre><span>$</span> {{ command }}</pre>
  </div>
</template>
