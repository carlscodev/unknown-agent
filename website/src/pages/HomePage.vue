<script setup>
import { useI18n } from "vue-i18n";
import UiIcon from "../components/UiIcon.vue";
import CodeWindow from "../components/CodeWindow.vue";
import { useLocalePath } from "../composables/useLocalePath.js";
import { capabilityCards, terminalLines, phases, github } from "../content.js";

const { t } = useI18n();
const lp = useLocalePath();

/** Provider wordmarks are brand names and are never translated. */
const providers = ["OPENAI", "ANTHROPIC", "OPENROUTER", "OLLAMA"];

/** Verification loop nodes; labels come from `home.proof.loop.*`. */
const loopNodes = ["plan", "act", "observe", "verify"];

/** Numbers are rendered as-is; only their labels are translated. */
const metrics = [
  { value: "74", key: "metricTools" },
  { value: "21", key: "metricProviders" },
  { value: "804", key: "metricTests" },
];
</script>

<template>
  <div>
    <section class="hero">
      <div class="hero-grid" aria-hidden="true"></div>
      <div class="container hero-inner">
        <div class="hero-copy">
          <a class="release-pill" :href="`${github}/commits/main`" target="_blank" rel="noreferrer">
            <span>{{ t("home.releasePill.badge") }}</span> {{ t("home.releasePill.text") }}
            <UiIcon name="arrow" :size="14" class="icon-directional" />
          </a>
          <h1>{{ t("home.hero.titleLine1") }}<br /><em>{{ t("home.hero.titleLine2") }}</em></h1>
          <p class="hero-lead">{{ t("home.hero.lead") }}</p>
          <div class="hero-actions">
            <RouterLink class="button primary large" :to="lp('download')">
              {{ t("home.hero.ctaPrimary") }} <UiIcon name="arrow" :size="18" class="icon-directional" />
            </RouterLink>
            <RouterLink class="button ghost large" :to="lp('product')">{{ t("home.hero.ctaSecondary") }}</RouterLink>
          </div>
          <div class="hero-note">
            <span>{{ t("home.hero.note1") }}</span>
            <span>{{ t("home.hero.note2") }}</span>
            <span>{{ t("home.hero.note3") }}</span>
          </div>
        </div>

        <!-- Terminal transcript: literal CLI output, intentionally LTR and untranslated. -->
        <div class="terminal-card" dir="ltr">
          <div class="terminal-top">
            <span class="dots"><i></i><i></i><i></i></span>
            <span>{{ t("home.terminal.mission") }}</span>
            <span class="live-dot">{{ t("home.terminal.live") }}</span>
          </div>
          <div class="terminal-body">
            <div v-for="(line, i) in terminalLines" :key="i" :class="line[0]">{{ line[1] }}</div>
            <span class="cursor"></span>
          </div>
          <div class="terminal-foot">
            <span>{{ t("home.terminal.tokens") }}</span>
            <span>{{ t("home.terminal.elapsed") }}</span>
            <span>{{ t("home.terminal.branch") }}</span>
          </div>
        </div>
      </div>
    </section>

    <section class="trust-strip">
      <div class="container">
        <span>{{ t("home.trust.label") }}</span>
        <b v-for="name in providers" :key="name">{{ name }}</b>
        <b>{{ t("home.trust.more") }}</b>
      </div>
    </section>

    <section class="section">
      <div class="container">
        <div class="section-head">
          <p class="eyebrow">{{ t("home.difference.eyebrow") }}</p>
          <h2>{{ t("home.difference.titleLine1") }}<br />{{ t("home.difference.titleLine2") }}</h2>
          <p>{{ t("home.difference.lead") }}</p>
        </div>
        <div class="cap-grid">
          <article v-for="card in capabilityCards" :key="card.id" class="cap-card">
            <div class="cap-icon"><UiIcon :name="card.icon" /></div>
            <h3>{{ t(`capabilityCards.${card.id}.label`) }}</h3>
            <p>{{ t(`capabilityCards.${card.id}.text`) }}</p>
            <RouterLink :to="lp('capabilities')">
              {{ t("common.learnMore") }} <UiIcon name="arrow" :size="14" class="icon-directional" />
            </RouterLink>
          </article>
        </div>
      </div>
    </section>

    <section class="section proof-section">
      <div class="container split">
        <div>
          <p class="eyebrow">{{ t("home.proof.eyebrow") }}</p>
          <h2>{{ t("home.proof.titleLine1") }}<br />{{ t("home.proof.titleLine2") }}</h2>
          <p class="lede-sm">{{ t("home.proof.lead") }}</p>
          <ul class="check-list">
            <li>{{ t("home.proof.item1") }}</li>
            <li>{{ t("home.proof.item2") }}</li>
            <li>{{ t("home.proof.item3") }}</li>
            <li>{{ t("home.proof.item4") }}</li>
          </ul>
        </div>
        <div class="loop-diagram">
          <div class="loop-center">
            <b>{{ t("home.proof.loop.ship") }}</b>
            <span>{{ t("home.proof.loop.shipNote") }}</span>
          </div>
          <div v-for="(node, i) in loopNodes" :key="node" class="loop-node" :class="`n${i}`">
            <span>{{ String(i + 1).padStart(2, "0") }}</span>{{ t(`home.proof.loop.${node}`) }}
          </div>
          <svg viewBox="0 0 400 400" aria-hidden="true">
            <circle cx="200" cy="200" r="145" />
            <path d="M200 55a145 145 0 0 1 145 145" />
          </svg>
        </div>
      </div>
    </section>

    <section class="section">
      <div class="container">
        <div class="section-head row">
          <div>
            <p class="eyebrow">{{ t("home.surfaces.eyebrow") }}</p>
            <h2>{{ t("home.surfaces.titleLine1") }}<br />{{ t("home.surfaces.titleLine2") }}</h2>
          </div>
          <p>{{ t("home.surfaces.lead") }}</p>
        </div>
        <div class="surface-grid">
          <div class="surface-main">
            <div class="surface-label">{{ t("home.surfaces.cliLabel") }}</div>
            <CodeWindow
              command='ua build "refactor auth and prove every flow"'
              :label="t('home.surfaces.codeLabel')"
            />
            <div class="metric-row">
              <div v-for="metric in metrics" :key="metric.key">
                <b>{{ metric.value }}</b><span>{{ t(`home.surfaces.${metric.key}`) }}</span>
              </div>
            </div>
          </div>
          <div class="surface-side">
            <span class="status-orb"></span>
            <p class="eyebrow">{{ t("home.surfaces.sideEyebrow") }}</p>
            <h3>{{ t("home.surfaces.sideTitle") }}</h3>
            <p>{{ t("home.surfaces.sideText") }}</p>
            <RouterLink :to="lp('product')">
              {{ t("home.surfaces.sideLink") }} <UiIcon name="arrow" :size="15" class="icon-directional" />
            </RouterLink>
          </div>
        </div>
      </div>
    </section>

    <section class="section roadmap-preview">
      <div class="container">
        <div class="section-head row">
          <div>
            <p class="eyebrow">{{ t("home.roadmap.eyebrow") }}</p>
            <h2>{{ t("home.roadmap.title") }}</h2>
          </div>
          <RouterLink class="text-link" :to="lp('roadmap')">
            {{ t("home.roadmap.link") }} <UiIcon name="arrow" :size="15" class="icon-directional" />
          </RouterLink>
        </div>
        <div class="phase-row">
          <article v-for="phase in phases" :key="phase.id">
            <span class="phase-n">{{ phase.n }}</span>
            <span class="phase-status">{{ t(`phases.${phase.id}.status`) }}</span>
            <h3>{{ t(`phases.${phase.id}.title`) }}</h3>
            <p>{{ t(`phases.${phase.id}.text`) }}</p>
          </article>
        </div>
      </div>
    </section>

    <section class="section final-cta">
      <div class="container">
        <div class="cta-box">
          <p class="eyebrow">{{ t("home.cta.eyebrow") }}</p>
          <h2>{{ t("home.cta.titleLine1") }}<br />{{ t("home.cta.titleLine2") }}</h2>
          <CodeWindow
            command='pipx install "git+https://github.com/flormariap/unknown-agent"'
            :label="t('home.cta.installLabel')"
          />
          <div class="cta-links">
            <RouterLink class="button primary large" :to="lp('download')">{{ t("home.cta.primary") }}</RouterLink>
            <a :href="github" class="button ghost large" target="_blank" rel="noreferrer">{{ t("home.cta.secondary") }}</a>
          </div>
        </div>
      </div>
    </section>
  </div>
</template>
