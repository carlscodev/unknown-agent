"""The consequence table: what each planning option commits you to.

The axes catalogue states its own authoring rule — *"Every option needs a
consequence. If you cannot say what choosing it commits the user to, it is not an
option yet."*  This module is that rule made real.

It is kept apart from :mod:`unknown_agent.planning.axes` on purpose.  ``axes.py``
describes the shape of the decision space (options, tags, effort, risk, preset
answers, dependencies); this file describes what the *user experiences* as a
result of each choice, in product terms.  Keeping them separate means the copy can
be revised — made sharper, made honest about a cost — without touching the
machinery that scores and filters, and means a missing consequence is visible as a
missing key rather than buried in a 2000-line data file.

:func:`apply_implications` is called once, at import time, from ``axes.py``.  It
is additive and never overwrites implications an axis already declared, so a
future option can carry its own copy inline.

What a consequence is *for*:

* it is shown next to the option before the user commits
  (:class:`unknown_agent.planning.panels.question_panel`, the Textual decision
  panel);
* it is what makes two options comparable for someone who does not know the
  domain — "managed SQL" versus "document store" means nothing, "joins and
  transactions, plus migrations to write" versus "flexible shape, but integrity
  enforced in code" means something;
* it seeds the definition of done: choosing WCAG AA adds an a11y gate, choosing
  payments adds webhook idempotency, choosing TDD changes the order of work.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

__all__ = ["IMPLICATIONS", "apply_implications", "missing_implications"]


#: ``axis id → {option id → consequences}``.  Every string is written for a person
#: choosing, not for a developer reading: no jargon without its consequence.
IMPLICATIONS: dict[str, dict[str, tuple[str, ...]]] = {
    # -- 1. what problem is this ------------------------------------------- #
    "problem": {
        "save-time": (
            "the build optimises for automation: a scheduler or trigger, idempotent steps, and an audit trail",
            "success is measured in minutes saved, so a timing/throughput baseline is worth capturing early",
        ),
        "organize": (
            "search, filtering and permissions become first-class features, not add-ons",
            "data quality work (dedupe, normalisation) is usually the long pole",
        ),
        "sell": (
            "adds a product surface, a checkout path and the legal pages that go with selling",
            "payments bring webhooks, idempotency, refunds and reconciliation into scope",
        ),
        "communicate": (
            "content model, SEO and accessibility carry the outcome",
            "publishing workflow (drafts, review, schedule) matters more than features",
        ),
        "monitor": (
            "time series, alerting and retention become core concerns",
            "false positives are the failure mode — thresholds need tuning, not guessing",
        ),
        "analyze": (
            "reproducibility and data lineage matter more than UI polish",
            "notebook or pipeline; either way the output needs to be re-runnable",
        ),
        "create": (
            "generation quality, safety filters and provenance become requirements",
            "expect iteration loops: preview, edit, regenerate, keep",
        ),
        "connect": (
            "contracts, retries, backoff and observability across the boundary",
            "partial failure is normal: every call needs a defined compensation path",
        ),
        "custom": (
            "the agent will restate the problem in its own words before planning",
            "expect one or two follow-up questions to pin down success criteria",
        ),
    },
    # -- 2. who is it for --------------------------------------------------- #
    "audience": {
        "consumers": (
            "mobile-first layout, fast first paint, forgiving empty states",
            "sign-up friction is measured in abandonment, so onboarding gets its own pass",
        ),
        "developers": (
            "API-first: an OpenAPI contract, runnable examples and honest error messages",
            "the README is the product; docs are a deliverable, not an afterthought",
        ),
        "internal-team": (
            "SSO and role-based access are usually mandatory",
            "efficiency beats delight: dense tables, keyboard shortcuts, bulk actions",
        ),
        "enterprise": (
            "procurement needs a security questionnaire, an audit log and data residency answers",
            "adds an admin/ops surface and support tooling to the build",
        ),
        "creators": (
            "import/export, templates and integrations with the tools they already use",
            "power-user affordances (batch edit, keyboard, saved views) matter early",
        ),
        "kids": (
            "safety by design: no open chat, no dark patterns, content review",
            "children's privacy law applies — minimal data collection and parental consent",
        ),
        "regulated": (
            "compliance evidence, retention policy and access audit trails are in scope",
            "expect a review gate before anything is deployed",
        ),
    },
    # -- 3. where does it run ---------------------------------------------- #
    "platform": {
        "web": (
            "responsive layout across breakpoints, and a browser validation gate at the end",
            "accessibility and SEO are cheap now, expensive later",
        ),
        "mobile-web": (
            "a performance budget: bundle size, first paint, tap targets",
            "tested on a small viewport and a throttled connection, not a desktop",
        ),
        "desktop-os": (
            "packaging, code signing and auto-update become part of the release",
            "OS permissions (filesystem, notifications) need explicit handling",
        ),
        "mobile-os": (
            "store submission, push notifications and offline behaviour",
            "release cadence is gated by review, so feature flags help",
        ),
        "server": (
            "no UI: API contract, observability and horizontal scaling are the product",
            "the definition of done is a passing integration suite, not a screenshot",
        ),
        "cli": (
            "exit codes, --help, machine-readable output and shell completion",
            "must work in a dumb terminal: no colour dependency, no interactive-only paths",
        ),
        "embedded": (
            "memory and power budgets, OTA updates, hardware abstraction",
            "debugging is remote — logging and diagnostics are a feature",
        ),
    },
    # -- 4. how big --------------------------------------------------------- #
    "scope_level": {
        "hours": (
            "one path, no abstractions, no options screen",
            "the agent will not ask clarifying questions — it infers and states its assumptions",
        ),
        "days": (
            "a milestone or two, a smoke test and a short README",
            "structure stays deliberately simple; refactoring is a later decision",
        ),
        "weeks-1-2": (
            "real architecture: module boundaries, a test suite, CI",
            "documentation and a handover note are part of the deliverable",
        ),
        "months-1-3": (
            "phased delivery with a definition of done per phase",
            "expect a design document and explicit non-goals before code",
        ),
        "open-ended": (
            "the plan is a roadmap, and the first deliverable is the walking skeleton",
            "maintainability and extension points are weighted above speed",
        ),
    },
    # -- 5. how finished ---------------------------------------------------- #
    "feature_depth": {
        "prototype": (
            "hardcoded where it saves time; no error handling beyond the happy path",
            "explicitly not for production — it exists to answer a question",
        ),
        "mvp": (
            "one journey done end to end, with the edges left rough",
            "every later feature is measured against whether it serves that journey",
        ),
        "core": (
            "the main journeys plus the states that make them trustworthy: empty, loading, error",
            "polish pass at the end, and a browser check on anything with a UI",
        ),
        "full": (
            "admin surface, settings, onboarding, edge cases and documentation",
            "the longest path: expect the plan to be phased",
        ),
        "platform": (
            "extension points, multi-tenancy and an API other people build on",
            "compatibility promises start here — versioning is a requirement",
        ),
    },
    # -- 6. what it looks like --------------------------------------------- #
    "design_reference": {
        "none": (
            "the agent picks a direction and states why",
            "you get a rationale you can argue with, which is faster than a blank page",
        ),
        "brand": (
            "colours, type and logo drive the palette; components follow the brand",
            "bring the assets — a hex list or a file; guessing a brand is how designs go wrong",
        ),
        "competitor": (
            "the agent will describe the qualities it is borrowing (density, tone, layout), not copy markup",
            "avoids trademark and clone problems while keeping the feel you recognised",
        ),
        "url": (
            "the URL is visited and described structurally: grid, hierarchy, motion, palette",
            "if the browser extra is missing, this degrades to your description of it",
        ),
        "mood": (
            "the mood is translated into concrete tokens (contrast, radius, density, motion) before styling",
            "you will see the translation and can correct it — moods are where taste disagreements live",
        ),
    },
    "brand_color": {
        "agent-choice": (
            "chosen from the palette for contrast against your surfaces, not from fashion",
            "every colour is contrast-checked for text and focus states",
        ),
        "blue": ("reads as dependable and technical; the hardest colour to differentiate with", "needs a strong accent to avoid looking default"),
        "teal": ("reads modern and calm; works well in both light and dark", "watch green-leaning teals against success states"),
        "green": ("reads as growth, health or money", "collides with success semantics — success states need another colour"),
        "violet": ("reads as creative or premium", "dark violets can fail contrast on dark surfaces without a lift"),
        "orange": ("reads energetic and consumer", "needs care against warning semantics"),
        "red": ("reads urgent or bold", "collides with error semantics — errors need another colour"),
        "mono": ("type, spacing and hierarchy carry the design", "the most unforgiving choice: weak spacing shows immediately"),
    },
    # -- 7. how it is built ------------------------------------------------- #
    "backend_style": {
        "static": (
            "no server and no secrets: build-time data, CDN hosting",
            "dynamic behaviour has to live in the browser or a third party",
        ),
        "server-fullstack": (
            "one codebase, server-rendered pages and an ORM",
            "the simplest thing that scales to a real product; deployment is one process",
        ),
        "api-only": (
            "API and client versioned separately, with a contract between them",
            "adds CORS, auth token handling and two deployment targets",
        ),
        "serverless": (
            "scales to zero and per-request billing",
            "cold starts, execution time limits and no long-lived connections",
        ),
        "baas": (
            "auth, storage and database come pre-built — fastest route to working",
            "vendor lock-in and per-user pricing that grows with success",
        ),
        "local": (
            "data stays on the device: private, fast, offline-capable",
            "sync and conflict resolution become your problem the moment there are two devices",
        ),
        "microservices": (
            "independent deploy and scale per service",
            "distributed tracing, service discovery and eventually-consistent data",
        ),
        "bff": (
            "a backend shaped exactly for the client, hiding internal services",
            "one more service to own, and a second place where auth is enforced",
        ),
    },
    "data_strategy": {
        "none": (
            "stateless: everything comes from the request or a third party",
            "nothing to migrate, nothing to back up — and nothing remembered",
        ),
        "local-first": (
            "instant reads and full offline support",
            "device-bound data: export/import and sync need designing up front",
        ),
        "sql": (
            "joins, transactions and referential integrity",
            "migrations from day one, and a schema to keep honest",
        ),
        "nosql": (
            "flexible document shape, easy horizontal scale",
            "integrity enforced in application code, and queries planned around access patterns",
        ),
        "kv-cache": (
            "sub-millisecond reads for hot data",
            "not a system of record: eviction and TTLs are normal behaviour",
        ),
        "search-index": (
            "relevance, facets and typo tolerance",
            "indexing pipeline and eventual consistency between store and index",
        ),
        "files": (
            "cheap, durable blob storage with signed URLs",
            "no queries over content — metadata has to live somewhere queryable",
        ),
        "hybrid": (
            "SQL for structure and integrity, object storage for blobs",
            "two systems to back up, and consistency between them to reason about",
        ),
    },
    "auth_strategy": {
        "none": (
            "no accounts, no personal data, no auth surface",
            "everything is public or single-tenant — retrofitting auth later touches every query",
        ),
        "managed-auth": (
            "hosted login, MFA and user management out of the box",
            "a vendor dependency and per-user pricing; customisation is limited to what they expose",
        ),
        "email-password": (
            "full control and no vendor",
            "you own password hashing, reset flows, lockout, breach checking and the support email",
        ),
        "magic-link": (
            "no passwords to store or reset",
            "depends on email deliverability and a link-expiry policy",
        ),
        "social-oauth": (
            "lowest friction for consumers; no password handling",
            "third-party dependency, account-linking rules and token refresh",
        ),
        "passkeys": (
            "phishing-resistant and the best UX on supported platforms",
            "newer library support, and you still need a fallback for old devices",
        ),
        "sso": (
            "required by enterprise buyers; centralises joiners and leavers",
            "SAML/OIDC metadata, certificate rotation and per-tenant configuration",
        ),
        "api-keys": (
            "machine callers, scoped tokens and rotation",
            "you own key storage, hashing and revocation — and leaked keys are your incident",
        ),
    },
    "integrations": {
        "none": (
            "no external runtime dependencies to fail",
            "the simplest possible deploy and test story",
        ),
        "payments": (
            "webhook handling, idempotency keys, refunds and reconciliation",
            "PCI scope decisions and a test mode before a live one",
        ),
        "email": (
            "transactional provider, templates, bounces and unsubscribe",
            "deliverability (SPF/DKIM) is a deployment task, not a code task",
        ),
        "analytics": (
            "event schema and a consent story where privacy law applies",
            "data you collect becomes data you must protect and delete on request",
        ),
        "third-party": (
            "rate limits, retries with backoff, and contract tests against their docs",
            "their outage becomes your error state — design the degraded path",
        ),
        "webhooks": (
            "signature verification, replay protection and at-least-once handling",
            "an endpoint that must answer fast and process slowly (queue)",
        ),
        "internal-apis": (
            "service discovery, auth between services and shared schemas",
            "changes need coordination: a contract test guards both sides",
        ),
        "push": (
            "device tokens, delivery providers and notification preferences",
            "users who cannot turn it off uninstall — a preference centre is part of it",
        ),
        "ai": (
            "prompt/version management, token budgets and output validation",
            "non-determinism: the tests assert shape and safety, not exact wording",
        ),
    },
    "tech_preference": {
        "agent-choice": (
            "chosen for the task and for what is already in the repo",
            "you get the reasoning, and can override it in one answer",
        ),
        "python": ("one language across CLI, API and scripts; the deepest tooling for data work", "packaging and distribution need a decision (venv, wheels, container)"),
        "typescript": ("one language across front and back end, with types end to end", "build tooling choices multiply; pick one and stop"),
        "react": ("the largest ecosystem and the easiest hiring story", "state management and bundle size need deliberate choices"),
        "vue": ("gentle learning curve and excellent single-file components", "a smaller ecosystem than React for niche needs"),
        "svelte": ("less runtime, smaller bundles, compiler-driven reactivity", "fewer ready-made component libraries"),
        "go": ("single static binary, fast startup, excellent concurrency", "more boilerplate; generics and error handling are verbose"),
        "rust": ("memory safety and performance with no runtime", "the slowest to build and the hardest to hire for"),
        "existing-stack": ("matches the repo: no new toolchain, no new CI, no new review burden", "inherits whatever the repo already does well or badly"),
    },
    # -- 8. what it optimises for ------------------------------------------ #
    "performance_priority": {
        "balanced": ("no single metric dominates the design", "trade-offs are decided per feature, and recorded"),
        "speed-to-launch": ("the shortest path that works; refactoring deferred by name, not by silence", "technical debt is written into the plan so it is a decision, not an accident"),
        "ux": ("perceived speed: first paint, skeleton states, optimistic updates", "measured in a real browser, not in a unit test"),
        "throughput": ("load testing, connection pooling, batching and backpressure", "the bottleneck is found by measurement before it is optimised"),
        "seo": ("server-rendered or prerendered HTML, structured data, sitemaps", "Core Web Vitals become a gate; content is indexable without JavaScript"),
        "cost": ("cheap defaults: static where possible, right-sized compute, caching", "idle cost matters more than peak cost — serverless or static wins"),
        "offline": ("service worker or local store, sync queue and conflict policy", "every network call needs an offline answer"),
        "scalability": ("stateless services, horizontal scale, queue-based work", "costs rise with success — a scaling plan is part of the design"),
        "maintainability": ("module boundaries, tests that describe behaviour, and docs", "the next person (possibly you, in six months) is the user being designed for"),
        "security": ("threat model, dependency auditing, secret handling, least privilege", "a security review gate before release"),
        "dx": ("fast local setup, one command to run everything, useful errors", "CI under a few minutes or it will be skipped"),
        "conversion": ("analytics, A/B infrastructure and a measurable funnel", "every change to the funnel needs a metric attached"),
        "a11y": ("semantic HTML, keyboard paths, screen-reader testing", "an accessibility gate in CI, not a one-off audit"),
    },
    "accessibility": {
        "none": (
            "no a11y gate in the plan",
            "still gets semantic HTML and focus order, because those cost nothing",
        ),
        "basic": (
            "semantic elements, visible focus, labels on inputs",
            "cheap insurance; covers the majority of real assistive-tech use",
        ),
        "aa": (
            "WCAG 2.2 AA: contrast ratios, keyboard-only flows, aria where semantics fall short",
            "an automated a11y gate plus a manual pass on the main journeys",
        ),
        "strict": (
            "AAA and legal evidence: audit notes, test results, remediation log",
            "slower and more expensive; usually required by public-sector or regulated buyers",
        ),
    },
    "testing_bar": {
        "none": (
            "no test gate; the definition of done is 'it runs'",
            "appropriate for throwaway work and nothing else — the plan will say so",
        ),
        "smoke": (
            "one test per critical path, fast to run",
            "catches 'it does not start' and little else",
        ),
        "balanced": (
            "unit tests for logic, integration tests for the boundaries that break",
            "the default: the best coverage per minute of build time",
        ),
        "thorough": (
            "the balanced suite plus browser validation for anything with a UI",
            "needs the browser extra installed; slower, but UI claims become evidence",
        ),
        "tdd": (
            "tests are written first and the implementation follows them",
            "changes the order of work and needs fixtures before features",
        ),
    },
    "deployment": {
        "managed-paas": (
            "git push to deploy, TLS and scaling handled for you",
            "least control, per-unit pricing, and a vendor-shaped config file",
        ),
        "container": (
            "one artefact that runs identically everywhere",
            "you own the registry, the orchestrator and the upgrade path",
        ),
        "vps": (
            "predictable cost and full control",
            "you own patching, backups, TLS renewal and 3am restarts",
        ),
        "serverless-cloud": (
            "scales to zero, bills per invocation",
            "cold starts, execution limits and vendor-specific event shapes",
        ),
        "self-host": (
            "data never leaves the customer's infrastructure",
            "air-gapped installs, offline upgrades and support without access",
        ),
        "static-cdn": (
            "cheapest and fastest global delivery, no server to break",
            "no server-side logic and no secrets — everything must be public",
        ),
        "local-only": (
            "nothing to deploy: it runs on the user's machine",
            "distribution and updates are the product problem instead",
        ),
        "undecided": (
            "the plan keeps deployment behind an interface so the choice stays open",
            "a container is the safest hedge — it deploys to almost every target",
        ),
    },
    "monetization": {
        "none": (
            "no billing surface at all",
            "removes an entire class of failure modes from the build",
        ),
        "free": (
            "no billing, but public-facing: abuse limits and rate limiting matter",
            "cost per user is real and unbilled — a budget belongs in the plan",
        ),
        "subscription": (
            "billing provider, plan tiers, trials, dunning and cancellation flows",
            "webhook-driven state changes and a reconciliation job",
        ),
        "usage": (
            "metering, aggregation, quotas and a bill the customer can audit",
            "the metering path must be correct before it is billed on",
        ),
        "one-time": (
            "licences, keys, upgrades and refund handling",
            "simpler than subscriptions, and offline validation is a question to answer",
        ),
        "freemium": (
            "a free tier with real limits and upgrade prompts at the right moments",
            "the limits are a product decision; abuse of the free tier is a cost decision",
        ),
        "commerce": (
            "catalogue, inventory, cart, checkout, tax, shipping and fulfilment",
            "the largest surface here — expect a phased plan",
        ),
        "ads": (
            "ad slots, consent management and revenue reporting",
            "ads change layout and Core Web Vitals; the two goals need arbitrating",
        ),
        "internal": (
            "cost is justified by time saved, so the metrics are internal",
            "no billing, but adoption and support are the real deliverables",
        ),
    },
    "maintenance": {
        "throwaway": (
            "optimised for speed to an answer, then deleted",
            "no tests, no docs, no abstractions — and the plan says so out loud",
        ),
        "solo": (
            "boring technology and few dependencies: future-you has to understand it",
            "documentation is minimal but the code is expected to be readable",
        ),
        "team": (
            "code review, CI, contribution conventions and shared ownership",
            "a linter and formatter are non-negotiable — style arguments waste a team",
        ),
        "handover": (
            "architecture doc, runbook, onboarding guide and a recorded walkthrough",
            "the docs are a deliverable with its own gate, not a side effect",
        ),
        "oss": (
            "licence, contribution guide, issue templates, semantic versioning",
            "public API stability: breaking changes need a deprecation path",
        ),
        "agency": (
            "acceptance criteria, a deployment runbook and a support window",
            "handover documentation and knowledge transfer are billable deliverables",
        ),
    },
    "non_goals": {
        "agent-decides": (
            "the agent proposes the exclusions and lists them in the plan",
            "you can veto any of them in one answer",
        ),
        "no-auth": (
            "no accounts in this pass",
            "retrofitting auth later touches every query that returns user data",
        ),
        "no-payments": (
            "no billing in this pass",
            "the data model should still leave room for a plan/tier field",
        ),
        "no-admin": (
            "no back office: configuration lives in code or config files",
            "operational changes need a deploy until an admin surface exists",
        ),
        "no-i18n": (
            "one language, hardcoded strings",
            "extracting strings later is mechanical but touches every component",
        ),
        "no-mobile": (
            "desktop-first layout",
            "it will still be reachable on a phone — just not pleasant",
        ),
        "no-analytics": (
            "no tracking, no consent banner",
            "product decisions are made without usage data until it is added",
        ),
        "no-tests": (
            "no test gate: the fastest path and the weakest safety net",
            "the plan will name the risk explicitly rather than hiding it",
        ),
    },
}


def apply_implications(axes: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``axes`` with every declared consequence attached.

    Additive by design: implications already present on an option are kept and the
    table's copy is appended only if it is not there yet.  That way an axis author
    can override this file inline without a conflict, and this file can be revised
    without invalidating an axis.
    """

    updated: dict[str, Any] = {}
    for axis_id, axis in axes.items():
        table = IMPLICATIONS.get(axis_id, {})
        if not table:
            updated[axis_id] = axis
            continue
        options = []
        for option in axis.options:
            extra = table.get(option.id)
            if not extra:
                options.append(option)
                continue
            merged = tuple(option.implications) + tuple(line for line in extra if line not in option.implications)
            options.append(replace(option, implications=merged))
        updated[axis_id] = replace(axis, options=tuple(options))
    return updated


def missing_implications(axes: Mapping[str, Any]) -> list[str]:
    """Diagnostic: options that still have no consequence to show.

    Used by the test suite so the authoring rule stays enforced as the catalogue
    grows — a new option without implications fails the build rather than shipping
    a choice the user cannot weigh.
    """

    gaps: list[str] = []
    for axis_id, axis in axes.items():
        for option in axis.options:
            if not option.implications:
                gaps.append(f"{axis_id}.{option.id}")
    return gaps
