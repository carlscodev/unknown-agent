<script setup>
import { ref } from "vue";
import { useI18n } from "vue-i18n";
import PageHero from "../components/PageHero.vue";
import CodeWindow from "../components/CodeWindow.vue";
import UiIcon from "../components/UiIcon.vue";
import { github } from "../content.js";

const { t } = useI18n();
const tab = ref("quick");

const tabs = ["quick", "verify", "web"];

/** Literal shell commands — never translated (see docs/i18n-glossary.md). */
const commands = {
  quick: [
    'pipx install "git+https://github.com/flormariap/unknown-agent"',
    "ua setup",
    'ua build "add the feature and verify it"',
  ],
  verify: ["ua inspect", 'ua plan "migrate auth to passkeys"', "ua verify"],
  web: ["ua web", "ua approve device 123456", "ua runs show --last --events"],
};

const refs = ["config", "toolApi", "missionEngine", "webApi", "plugins", "contributing"];
</script>

<template>
  <div>
    <PageHero
      :eyebrow="t('developers.hero.eyebrow')"
      :title="t('developers.hero.title')"
      :lead="t('developers.hero.lead')"
    />

    <section class="section no-top">
      <div class="container">
        <div class="docs-console">
          <div class="docs-tabs" role="tablist" :aria-label="t('developers.reference.title')">
            <button
              v-for="id in tabs"
              :key="id"
              type="button"
              role="tab"
              :aria-selected="tab === id"
              :class="{ active: tab === id }"
              @click="tab = id"
            >
              {{ t(`developers.tabs.${id}`) }}
            </button>
          </div>
          <!-- Commands are literal CLI input: forced LTR so they stay readable in RTL locales. -->
          <div class="docs-code" dir="ltr">
            <div v-for="(cmd, i) in commands[tab]" :key="cmd"><span>{{ i + 1 }}</span><code>{{ cmd }}</code></div>
          </div>
        </div>
      </div>
    </section>

    <section class="section">
      <div class="container split">
        <div>
          <p class="eyebrow">{{ t("developers.install.eyebrow") }}</p>
          <h2>{{ t("developers.install.titleLine1") }}<br />{{ t("developers.install.titleLine2") }}</h2>
          <p class="lede-sm">{{ t("developers.install.lead") }}</p>
        </div>
        <div>
          <CodeWindow
            command='pipx install "git+https://github.com/flormariap/unknown-agent"'
            :label="t('developers.install.recommended')"
          />
          <CodeWindow
            command='uv tool install "git+https://github.com/flormariap/unknown-agent"'
            :label="t('developers.install.fastPath')"
          />
        </div>
      </div>
    </section>

    <section class="section dark-panel">
      <div class="container">
        <div class="section-head">
          <p class="eyebrow">{{ t("developers.reference.eyebrow") }}</p>
          <h2>{{ t("developers.reference.title") }}</h2>
        </div>
        <div class="reference-grid">
          <a v-for="id in refs" :key="id" :href="`${github}/tree/main/docs`" target="_blank" rel="noreferrer">
            <div>
              <h3>{{ t(`developers.reference.${id}.title`) }}</h3>
              <p>{{ t(`developers.reference.${id}.text`) }}</p>
            </div>
            <UiIcon name="arrow" class="icon-directional" />
          </a>
        </div>
      </div>
    </section>

    <section class="section mini-cta">
      <div class="container">
        <h2>{{ t("developers.cta.title") }}</h2>
        <a class="button ghost" :href="github" target="_blank" rel="noreferrer">
          <UiIcon name="github" /> {{ t("developers.cta.button") }}
        </a>
      </div>
    </section>
  </div>
</template>
