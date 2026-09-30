export const github = "https://github.com/flormariap/unknown-agent";
export const releases = `${github}/releases/latest`;

export const capabilities = [
  { icon: "orbit", label: "Long-horizon runtime", text: "Durable missions survive restarts, freeze stalled work and resume without paying twice." },
  { icon: "branch", label: "Parallel agent branches", text: "Fork sessions at any event, preserve ancestry and merge results with semantic conflict previews." },
  { icon: "code", label: "Code intelligence", text: "Persistent LSP supervisors deliver definitions, references, symbols, diagnostics and precise edits." },
  { icon: "shield", label: "Policy before action", text: "Every side effect crosses typed contracts, path boundaries, risk levels and permission checks." },
  { icon: "check", label: "Verification gates", text: "Builds, tests, linters and browser flows decide when work is done—not model confidence." },
  { icon: "memory", label: "Memory with provenance", text: "Working, session and project memory stay replayable, attributable and under your control." },
];

export const phases = [
  { n: "01", status: "Complete", title: "Coding core", text: "Mission runtime, LSP intelligence, isolated worktrees, durable forks, tiered compaction and transactional mutations—all shipped." },
  { n: "02", status: "Complete", title: "Extensible runtime", text: "Signed capabilities, durable hooks, persistent automation, native sandbox profiles and stable ACP/JSON-RPC are shipped." },
  { n: "03", status: "Complete", title: "Omnichannel gateway", text: "Durable messaging, ACL pairing, five production channel adapters, content-addressed media, voice transcription and Ed25519 device delegation are live." },
  { n: "04", status: "Building", title: "Product ecosystem", text: "Migrations, SPDX/SLSA release security and durable performance/reliability regression benchmarks are live; clients and marketplace follow." },
];

export const terminalLines = [
  ["dim", "$ ua build \"ship the billing portal\""],
  ["info", "◆ mapped 214 files · Next.js + FastAPI"],
  ["plain", "  planned  7 steps across 3 isolated branches"],
  ["good", "✓ tests    184 passed · 0 failed"],
  ["good", "✓ browser  checkout flow verified"],
  ["good", "✓ policy   no boundary violations"],
  ["accent", "◆ shipped  definition of done satisfied"],
];

export const comparison = [
  ["Durable missions", true, false, false],
  ["Verification-gated completion", true, false, false],
  ["Historical session forks", true, false, false],
  ["Persistent LSP supervision", true, true, false],
  ["Provider independence", true, false, true],
  ["Local / offline models", true, false, true],
  ["Policy-checked side effects", true, false, false],
  ["Self-hosted web control plane", true, false, false],
];
