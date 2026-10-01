#!/usr/bin/env node
/**
 * i18n quality gate.
 *
 * Validates every shipped locale against the source locale (`en`) and exits
 * non-zero on any violation, so CI fails before a broken translation reaches
 * production. Run locally with `npm run i18n:check`.
 *
 * Checks performed
 *  1. structure    — every locale has exactly the source key set (no missing,
 *                    no extra), and types match (string vs array vs object).
 *  2. empty        — no empty or whitespace-only values.
 *  3. placeholders — the `{named}` variables in each message match the source
 *                    exactly, so no translation drops or invents a variable.
 *  4. plurals      — messages the source defines with pipe-separated plural
 *                    forms keep a valid form count in every locale.
 *  5. untranslated — no value is byte-identical to the English source, unless
 *                    the key is explicitly allow-listed (brand names, URLs,
 *                    product nouns that legitimately do not translate).
 *  6. descriptions — every source key has translator context in
 *                    `src/i18n/descriptions.json`.
 *  7. registry     — the locale files on disk match `src/i18n/locales.js`.
 *
 * Partial-by-design namespaces (see PARTIAL_PREFIXES) are exempt from the
 * "no missing keys" rule: long-form changelog entries intentionally fall back
 * to English and the UI shows an explicit "not translated" notice.
 */

import { readFileSync, readdirSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const localesDir = join(here, "..", "src", "i18n", "locales");
const descriptionsPath = join(here, "..", "src", "i18n", "descriptions.json");

const SOURCE = "en";

/**
 * Key prefixes where a locale may legitimately ship a subset of the source
 * keys. Anything present must still be complete and valid; only absence is OK.
 */
const PARTIAL_PREFIXES = ["changelog.entries."];

/**
 * Keys whose value is allowed to be identical to English, with the reason.
 * Keeping this explicit means a genuine copy-paste mistake still fails.
 *
 * Two forms:
 *   "some.key"          — allowed in every locale (brand names, filenames, …)
 *   "pt-BR:some.key"    — allowed only in that locale (established loanword)
 *
 * A locale-scoped entry is the honest way to record that a word genuinely is
 * the native term in that language, instead of weakening the gate globally.
 */
const SAME_AS_ENGLISH_OK = {
  "meta.siteName": "brand name",
  "meta.titleTemplate": "brand name + variable only",
  "a11y.github": "brand name",
  "nav.github": "brand name",
  "footer.columns.project.github": "brand name",
  "footer.copyright": "brand name + variable only",
  "compare.table.unknownAgent": "brand name",
  "security.reporting.cardLabel": "filename",
  "common.terminal": "widely used as-is in technical UI",
  "home.terminal.live": "status token rendered as a badge",
  "home.surfaces.cliLabel": "acronyms",
  "developers.tabs.quick": "short label that is identical in some languages",
  "capabilities.groups.providerFabric.i2": "product names only",
  "capabilities.groups.providerFabric.i3": "product names only",
  "download.platforms.linux.name": "product name",
  "download.platforms.macos.name": "product name",
  "download.platforms.windows.name": "product name",
  "download.platforms.req2": "product name + version",
  "developers.reference.plugins.title": "product noun, used as-is in most languages",
  "product.stages.understand.step": "numeric label",
  "product.stages.plan.step": "numeric label",
  "product.stages.act.step": "numeric label",
  "product.stages.verify.step": "numeric label",

  // Brazilian Portuguese: these are the established terms in pt-BR technical
  // writing — translating them would read as stilted, not as more localised.
  "pt-BR:meta.download.title": "'Download' is the standard pt-BR term",
  "pt-BR:meta.roadmap.title": "'Roadmap' is the standard pt-BR term",
  "pt-BR:nav.download": "'Download' is the standard pt-BR term",
  "pt-BR:nav.roadmap": "'Roadmap' is the standard pt-BR term",
  "pt-BR:footer.columns.product.download": "'Download' is the standard pt-BR term",
  "pt-BR:footer.columns.resources.roadmap": "'Roadmap' is the standard pt-BR term",
  "pt-BR:footer.columns.project.issues": "GitHub feature name, used as-is in pt-BR",
  "pt-BR:home.terminal.branch": "Git terminology, used as-is in pt-BR",
  "pt-BR:download.hero.eyebrow": "'DOWNLOAD' is the standard pt-BR term",
  "pt-BR:download.client.eyebrow": "'DESKTOP' and 'MOBILE' are standard pt-BR terms",
  "pt-BR:roadmap.hero.eyebrow": "'ROADMAP' is the standard pt-BR term",

  // German: English loanwords that are the normal register in German tech copy.
  "de:meta.download.title": "'Download' is a standard German noun",
  "de:meta.roadmap.title": "'Roadmap' is standard in German tech writing",
  "de:nav.download": "'Download' is a standard German noun",
  "de:nav.roadmap": "'Roadmap' is standard in German tech writing",
  "de:footer.columns.product.download": "'Download' is a standard German noun",
  "de:footer.columns.resources.roadmap": "'Roadmap' is standard in German tech writing",
  "de:roadmap.hero.eyebrow": "'ROADMAP' is standard in German tech writing",
  "de:download.hero.eyebrow": "'DOWNLOAD' is a standard German noun",
  "de:developers.install.eyebrow": "'Installation' is spelled identically in German",
  "de:download.platforms.req3": "'Optional' plus three product names",

  // French: identical spellings and established loanwords.
  "fr:developers.install.eyebrow": "'Installation' is spelled identically in French",
  "fr:developers.reference.config.title": "'Configuration' is spelled identically in French",
  "fr:product.sovereign.openTitle": "'Open source' is the established French term",

  // Italian: established loanwords in Italian technical writing.
  "it:meta.roadmap.title": "'Roadmap' is standard in Italian tech writing",
  "it:nav.roadmap": "'Roadmap' is standard in Italian tech writing",
  "it:footer.columns.resources.roadmap": "'Roadmap' is standard in Italian tech writing",
  "it:roadmap.hero.eyebrow": "'ROADMAP' is standard in Italian tech writing",
  "it:home.terminal.branch": "Git terminology, used as-is in Italian",
  "it:download.client.eyebrow": "'DESKTOP' and 'MOBILE' are standard Italian terms",

  // Scripts that do not transliterate Latin-script paths or protocol acronyms.
  "ja:home.surfaces.codeLabel": "filesystem path, kept in Latin script",
  "zh-CN:home.surfaces.codeLabel": "filesystem path, kept in Latin script",
  "ar:home.surfaces.codeLabel": "filesystem path, kept in Latin script and LTR",
  "ja:product.surfaces.pythonApi.title": "'Python API' is used as-is in Japanese technical writing",
  "zh-CN:product.surfaces.pythonApi.title": "'Python API' is used as-is in Chinese technical writing",
  "de:product.surfaces.terminal.title": "'Terminal' is the standard German term",
  "es:product.surfaces.terminal.title": "'Terminal' is the standard Spanish term",
  "fr:product.surfaces.terminal.title": "'Terminal' is the standard French term",
  "pt-BR:product.surfaces.terminal.title": "'Terminal' is the standard Brazilian Portuguese term",
  "ja:developers.reference.webApi.title": "'Web API' is used as-is in Japanese",
};

/**
 * Values that may legitimately be identical in any locale because they are not
 * really prose: phase labels like "Phase 4.6", pure version strings, and
 * two-digit step numbers.
 */
const LANGUAGE_NEUTRAL_VALUE = /^(Phase\s[\d.]+|v?\d+(\.\d+)*|\d{2})$/;

/** True when `key` may legitimately match the English source in `locale`. */
function sameAsEnglishAllowed(locale, key, value) {
  const base = baseKey(key);
  if (SAME_AS_ENGLISH_OK[base] || SAME_AS_ENGLISH_OK[`${locale}:${base}`]) return true;
  return LANGUAGE_NEUTRAL_VALUE.test(value);
}

/* ── helpers ──────────────────────────────────────────────────────────── */

/** Flattens a nested object into `{ 'a.b.c': value }`, arrays become `a.b[0]`. */
function flatten(value, prefix = "", out = {}) {
  if (Array.isArray(value)) {
    value.forEach((item, index) => flatten(item, `${prefix}[${index}]`, out));
  } else if (value && typeof value === "object") {
    for (const [key, child] of Object.entries(value)) {
      flatten(child, prefix ? `${prefix}.${key}` : key, out);
    }
  } else {
    out[prefix] = value;
  }
  return out;
}

/** Named `{variable}` placeholders in a message, deduped and sorted. */
function placeholders(message) {
  if (typeof message !== "string") return [];
  const found = message.match(/\{\s*[a-zA-Z_][a-zA-Z0-9_]*\s*\}/g) ?? [];
  return [...new Set(found.map((token) => token.replace(/[{}\s]/g, "")))].sort();
}

/** Number of pipe-separated plural forms in a message. */
function pluralForms(message) {
  if (typeof message !== "string") return 1;
  return message.split("|").length;
}

function isPartialKey(key) {
  return PARTIAL_PREFIXES.some((prefix) => key.startsWith(prefix));
}

/** Strips the array index so `a.b[0]` matches an allow-list entry for `a.b`. */
function baseKey(key) {
  return key.replace(/\[\d+\]$/, "");
}

function readJson(path) {
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch (error) {
    console.error(`✖ cannot parse ${path}: ${error.message}`);
    process.exit(1);
  }
}

/* ── load ─────────────────────────────────────────────────────────────── */

const localeFiles = readdirSync(localesDir)
  .filter((name) => name.endsWith(".json"))
  .map((name) => name.replace(/\.json$/, ""));

if (!localeFiles.includes(SOURCE)) {
  console.error(`✖ source locale "${SOURCE}.json" is missing from ${localesDir}`);
  process.exit(1);
}

const source = readJson(join(localesDir, `${SOURCE}.json`));
const sourceFlat = flatten(source);
const sourceKeys = Object.keys(sourceFlat);

const errors = [];
const warnings = [];

/* ── 7. registry consistency ──────────────────────────────────────────── */

const registryPath = join(here, "..", "src", "i18n", "locales.js");
if (existsSync(registryPath)) {
  const registrySource = readFileSync(registryPath, "utf8");
  const declared = [...registrySource.matchAll(/^\s{4}code:\s*"([^"]+)"/gm)].map((m) => m[1]);
  for (const code of declared) {
    if (!localeFiles.includes(code)) {
      errors.push(`registry: locales.js declares "${code}" but ${code}.json does not exist`);
    }
  }
  for (const code of localeFiles) {
    if (!declared.includes(code)) {
      errors.push(
        `registry: ${code}.json exists but "${code}" is not declared in locales.js ` +
          `(an unlisted locale is unreachable from the language switcher)`,
      );
    }
  }
}

/* ── 6. translator descriptions ───────────────────────────────────────── */

if (!existsSync(descriptionsPath)) {
  errors.push(`descriptions: ${descriptionsPath} is missing`);
} else {
  const descriptions = readJson(descriptionsPath);
  const describedPrefixes = Object.keys(descriptions);
  const undescribed = sourceKeys.filter((key) => {
    const base = baseKey(key);
    // A direct description, or one on any parent namespace, covers the key.
    const covered = describedPrefixes.some(
      (prefix) => base === prefix || base.startsWith(`${prefix}.`),
    );
    return !covered;
  });
  if (undescribed.length) {
    errors.push(
      `descriptions: ${undescribed.length} source key(s) have no translator context ` +
        `in src/i18n/descriptions.json — e.g. ${undescribed.slice(0, 5).join(", ")}`,
    );
  }
}

/* ── 1-5. per-locale checks ───────────────────────────────────────────── */

const summary = [];

for (const code of localeFiles) {
  if (code === SOURCE) continue;

  const target = readJson(join(localesDir, `${code}.json`));
  const targetFlat = flatten(target);
  const targetKeys = Object.keys(targetFlat);

  const missing = sourceKeys.filter((key) => !(key in targetFlat) && !isPartialKey(key));
  const partialMissing = sourceKeys.filter((key) => !(key in targetFlat) && isPartialKey(key));
  const extra = targetKeys.filter((key) => !(key in sourceFlat));

  if (missing.length) {
    errors.push(
      `${code}: ${missing.length} missing key(s) — ${missing.slice(0, 8).join(", ")}` +
        (missing.length > 8 ? ` …and ${missing.length - 8} more` : ""),
    );
  }
  if (extra.length) {
    errors.push(
      `${code}: ${extra.length} key(s) not present in ${SOURCE} — ${extra.slice(0, 8).join(", ")}` +
        (extra.length > 8 ? ` …and ${extra.length - 8} more` : ""),
    );
  }

  let identical = 0;

  for (const key of targetKeys) {
    const value = targetFlat[key];
    const sourceValue = sourceFlat[key];
    if (sourceValue === undefined) continue;

    // 2. empty values
    if (typeof value !== "string" || value.trim() === "") {
      errors.push(`${code}: "${key}" is empty or not a string`);
      continue;
    }

    // 1b. type agreement
    if (typeof sourceValue !== "string") {
      errors.push(`${code}: "${key}" type mismatch against ${SOURCE}`);
      continue;
    }

    // 3. placeholder parity
    const want = placeholders(sourceValue);
    const got = placeholders(value);
    if (want.join(",") !== got.join(",")) {
      errors.push(
        `${code}: "${key}" placeholder mismatch — ${SOURCE} has [${want.join(", ")}], ` +
          `${code} has [${got.join(", ")}]`,
      );
    }

    // 4. plural form count
    const wantForms = pluralForms(sourceValue);
    if (wantForms > 1 && pluralForms(value) < 2) {
      errors.push(
        `${code}: "${key}" loses pluralization — ${SOURCE} defines ${wantForms} forms, ` +
          `${code} defines ${pluralForms(value)}`,
      );
    }

    // 5. untranslated copy
    if (value === sourceValue && !sameAsEnglishAllowed(code, key, value)) {
      identical += 1;
      errors.push(`${code}: "${key}" is identical to the English source (untranslated copy)`);
    }
  }

  summary.push({
    code,
    keys: targetKeys.length,
    missing: missing.length,
    extra: extra.length,
    fallback: partialMissing.length,
    identical,
  });

  if (partialMissing.length) {
    warnings.push(
      `${code}: ${partialMissing.length} key(s) intentionally fall back to ${SOURCE} ` +
        `(partial-by-design namespace); the UI shows a "not translated" notice`,
    );
  }
}

/* ── report ───────────────────────────────────────────────────────────── */

console.log(`\ni18n check — source locale "${SOURCE}", ${sourceKeys.length} keys\n`);
console.log(
  ["locale", "keys", "missing", "extra", "fallback", "identical"]
    .map((heading, index) => (index === 0 ? heading.padEnd(8) : heading.padStart(10)))
    .join(""),
);
for (const row of summary) {
  console.log(
    row.code.padEnd(8) +
      String(row.keys).padStart(10) +
      String(row.missing).padStart(10) +
      String(row.extra).padStart(10) +
      String(row.fallback).padStart(10) +
      String(row.identical).padStart(10),
  );
}

if (warnings.length) {
  console.log("\nNotes:");
  for (const warning of warnings) console.log(`  · ${warning}`);
}

if (errors.length) {
  console.error(`\n✖ ${errors.length} i18n error(s):\n`);
  for (const error of errors) console.error(`  - ${error}`);
  console.error("");
  process.exit(1);
}

console.log("\n✔ all locales valid\n");
