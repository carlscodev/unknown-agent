/**
 * Shared structural content.
 *
 * This module used to hold English copy inline. After the i18n migration it
 * holds only *structure* — ids, icons, ordering and booleans — while every
 * human-readable string lives in `src/i18n/locales/<locale>.json` under the
 * matching namespace. Keeping the shape here and the words there means adding
 * a language never requires editing JavaScript.
 *
 *   capabilityCards[].id  -> t(`capabilityCards.${id}.label` | `.text`)
 *   phases[].id           -> t(`phases.${id}.status` | `.title` | `.text`)
 *   comparison[].id       -> t(`comparison.rows.${id}`)
 *
 * `terminalLines` is intentionally NOT translated: it reproduces literal
 * output of the `ua` CLI, which is English in the product itself. Translating
 * it would misrepresent what the user will actually see on their machine.
 * See docs/i18n-glossary.md.
 */

export const github = "https://github.com/flormariap/unknown-agent";
export const releases = `${github}/releases/latest`;

/** Home page capability grid. `id` keys into the `capabilityCards` namespace. */
export const capabilityCards = [
  { id: "longHorizon", icon: "orbit" },
  { id: "parallelBranches", icon: "branch" },
  { id: "codeIntelligence", icon: "code" },
  { id: "policy", icon: "shield" },
  { id: "verification", icon: "check" },
  { id: "memory", icon: "memory" },
];

/** Roadmap phases. `n` is a language-neutral ordinal rendered as-is. */
export const phases = [
  { id: "p1", n: "01" },
  { id: "p2", n: "02" },
  { id: "p3", n: "03" },
  { id: "p4", n: "04" },
];

/** Literal CLI transcript — deliberately untranslated (see module header). */
export const terminalLines = [
  ["dim", '$ ua build "ship the billing portal"'],
  ["info", "◆ mapped 214 files · Next.js + FastAPI"],
  ["plain", "  planned  7 steps across 3 isolated branches"],
  ["good", "✓ tests    184 passed · 0 failed"],
  ["good", "✓ browser  checkout flow verified"],
  ["good", "✓ policy   no boundary violations"],
  ["accent", "◆ shipped  definition of done satisfied"],
];

/**
 * Compare table rows: [rowId, unknownAgent, cloudAgent, chatTools].
 * `id` keys into the `comparison.rows` namespace.
 */
export const comparison = [
  { id: "durableMissions", values: [true, false, false] },
  { id: "verificationGated", values: [true, false, false] },
  { id: "historicalForks", values: [true, false, false] },
  { id: "persistentLsp", values: [true, true, false] },
  { id: "providerIndependence", values: [true, false, true] },
  { id: "localModels", values: [true, false, true] },
  { id: "policyChecked", values: [true, false, false] },
  { id: "selfHosted", values: [true, false, false] },
];
