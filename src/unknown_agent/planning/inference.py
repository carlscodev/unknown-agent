"""Inference vocabulary: what the request already told us, so we do not ask.

The clarification flow's first job is to *not* ask.  Every question costs the
user's attention, and a question whose answer is already in the request costs
their trust in the whole system.  So before anything is asked, the request text is
scanned for evidence, and any axis with enough evidence is answered on the user's
behalf — recorded with ``source="inferred"`` and a rationale that quotes the
phrase it matched, so the review panel can say *"I read this from your request"*
instead of silently assuming.

This module owns that vocabulary.  It is separate from
:mod:`unknown_agent.planning.axes` because the two change for different reasons:
the axes describe the decision space (product design), this describes how people
talk about it (language), and the language list grows every time a real request is
mis-read.

Scoring, implemented in :func:`unknown_agent.planning.clarify.extract_signals`:

* a multi-word needle is worth **3** ("deploys to fly.io" is nearly unambiguous);
* a single word is worth **2**;
* an option *tag* found in the token set adds **2** (or 1 if it only appears as a
  substring);
* :func:`~unknown_agent.planning.clarify.infer_answers` only answers an axis at a
  score of **3** or more.

That threshold is the reason a bare single word never answers a question on its
own — "api" alone will not decide your audience — while a word *plus* a tag, or a
phrase, will.  Substring matching means needles must be chosen so they cannot
appear inside an unrelated word; where a single word is genuinely ambiguous
("game", "internal", "ads") the option's tags do the work instead, because tags
are matched against the token set, not the raw string.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

__all__ = ["INFER_FROM", "SPECIFIC_NEEDLES", "apply_inference_vocabulary", "is_specific_needle"]


#: ``axis id → {option id → phrases that answer it}``.
INFER_FROM: dict[str, dict[str, tuple[str, ...]]] = {
    # -- audience ----------------------------------------------------------- #
    "audience": {
        "consumers": ("for consumers", "general public", "b2c", "customer-facing app"),
        "developers": ("for developers", "developer tool", "devtool", "api consumers", "developer experience"),
        "internal-team": ("internal tool", "for our team", "back office", "internal use", "staff tool", "ops team"),
        "enterprise": ("enterprise customers", "b2b saas", "procurement", "enterprise buyers", "corporate clients"),
        "creators": ("for creators", "prosumer", "content creators", "influencers"),
        "kids": ("for kids", "for children", "students", "classroom", "for schools", "pupils"),
        "regulated": ("hipaa", "gdpr", "sox", "pci-dss", "fda", "regulated industry", "compliance requirement"),
    },
    # -- platform ----------------------------------------------------------- #
    "platform": {
        "web": ("in the browser", "web app", "responsive", "single page app", "spa"),
        "mobile-web": ("mobile web", "mobile browser", "phone first", "mobile-first"),
        "desktop-os": ("desktop app", "macos app", "windows app", "linux desktop", "menubar app", "electron"),
        "mobile-os": ("ios app", "android app", "app store", "react native", "flutter", "mobile app"),
        "server": ("headless service", "server only", "no ui", "backend only", "no frontend", "api only"),
        "cli": ("command line", "terminal tool", "cli tool", "shell script", "runs in the terminal"),
        "embedded": ("embedded device", "raspberry pi", "iot device", "microcontroller", "edge device"),
    },
    # -- scope -------------------------------------------------------------- #
    "scope_level": {
        "hours": ("in an hour", "few hours", "this afternoon", "quick hack", "by end of day", "right now"),
        "days": ("in a few days", "this week", "couple of days", "by friday", "over the weekend"),
        "weeks-1-2": ("next two weeks", "in a few weeks", "this sprint", "two week", "fortnight"),
        "months-1-3": ("this quarter", "next quarter", "over a few months", "three months", "six weeks"),
        "open-ended": ("long term", "ongoing project", "evolve over time", "multi-year", "continuously"),
    },
    # -- how finished ------------------------------------------------------- #
    "feature_depth": {
        "prototype": ("throwaway prototype", "proof of concept", "spike", "quick demo", "just to test the idea"),
        "mvp": ("mvp", "minimum viable", "v1", "first version", "smallest thing that works"),
        "core": ("production ready", "polished", "done properly", "ship it properly"),
        "full": ("full product", "everything included", "complete platform", "no shortcuts"),
        "platform": ("multi-tenant", "extension points", "third parties build on", "public api for others", "plugin system"),
    },
    # -- design ------------------------------------------------------------- #
    "design_reference": {
        "none": ("you choose the design", "no design reference", "surprise me"),
        "brand": ("our brand", "brand guidelines", "brand colours", "we have a logo", "style guide"),
        "competitor": ("like linear", "like notion", "feels like stripe", "similar to figma", "like airbnb"),
        "url": ("look at http", "see https://", "this url", "copy the layout of"),
        "mood": ("calm and quiet", "dark and technical", "playful and bright", "editorial feel", "premium feel"),
    },
    # -- architecture ------------------------------------------------------- #
    "backend_style": {
        "static": ("static site", "no backend", "no server", "jamstack", "purely static", "html and css only"),
        "server-fullstack": ("django", "rails", "laravel", "next.js", "full-stack", "fullstack", "server rendered"),
        "api-only": ("separate api", "api and client", "rest api with a spa", "graphql api", "decoupled frontend"),
        "serverless": ("serverless", "lambda", "cloud functions", "cloud run", "edge functions"),
        "baas": ("supabase", "firebase", "appwrite", "backend as a service", "pocketbase"),
        "local": ("local-first", "offline first", "on device", "sqlite locally", "local storage only"),
        "microservices": ("microservices", "service per domain", "event driven architecture", "message bus", "kafka"),
        "bff": ("backend for frontend", "bff layer", "api gateway for the client"),
    },
    "data_strategy": {
        "none": ("no persistence", "stateless", "nothing stored", "no database"),
        "local-first": ("on the device", "local storage", "indexeddb", "sqlite file", "offline data"),
        "sql": (
            "postgres", "postgresql", "mysql", "mariadb", "sqlite", "sql server", "mssql", "duckdb",
            "relational database", "sql database", "foreign keys", "joins",
        ),
        "nosql": ("mongodb", "dynamodb", "couchdb", "document store", "document database", "cosmos db", "firebase firestore"),
        "kv-cache": ("redis", "memcached", "key-value", "cache layer", "in-memory cache", "valkey"),
        "search-index": ("elasticsearch", "opensearch", "algolia", "typesense", "meilisearch", "full text search", "faceted search"),
        "files": ("s3", "object storage", "minio", "google cloud storage", "blob storage", "r2 bucket", "uploaded files"),
        "hybrid": ("postgres and s3", "sql and object storage", "database plus blob", "metadata in sql"),
    },
    "auth_strategy": {
        "none": ("no login", "no accounts", "no auth", "public access", "without signing in", "no sign-in"),
        "managed-auth": ("auth0", "clerk", "workos", "supabase auth", "firebase auth", "managed auth", "cognito"),
        "email-password": ("email and password", "username and password", "password login", "self hosted auth", "bcrypt"),
        "magic-link": ("magic link", "passwordless", "one time code", "otp login", "email code"),
        "social-oauth": ("google login", "sign in with google", "github oauth", "oauth", "social login", "sign in with apple"),
        "passkeys": ("passkey", "webauthn", "fido2", "biometric login", "touch id", "face id"),
        "sso": ("saml", "single sign-on", "sso", "okta", "azure ad", "entra id", "oidc provider", "enterprise login"),
        "api-keys": ("api key", "bearer token", "personal access token", "service account", "scoped token"),
    },
    "integrations": {
        "none": ("no integrations", "nothing external", "standalone", "no third party"),
        "payments": (
            "stripe", "payments", "paypal", "checkout", "billing", "invoices", "adyen", "braintree",
            "square", "paddle", "lemon squeezy", "card payments",
        ),
        "email": ("sendgrid", "postmark", "amazon ses", "aws ses", "transactional email", "mailgun", "resend", "email notifications", "newsletter"),
        "analytics": ("analytics", "plausible", "mixpanel", "amplitude", "posthog", "segment", "google analytics", "ga4"),
        "third-party": ("third-party api", "external api", "public api", "partner api", "rest client"),
        "webhooks": ("webhook", "webhooks", "callback url", "event notification endpoint"),
        "internal-apis": ("internal service", "our api", "existing microservice", "in-house system", "legacy system"),
        "push": ("push notification", "fcm", "apns", "web push", "notifications to phones"),
        "ai": ("openai", "anthropic", "claude", "gpt-", "llm", "embeddings", "vector search", "ai feature", "rag pipeline"),
    },
    # -- qualities ---------------------------------------------------------- #
    "performance_priority": {
        "balanced": ("balanced approach", "nothing fancy", "keep it simple"),
        "speed-to-launch": ("as fast as possible", "ship quickly", "get it out", "asap", "this weekend", "hackathon"),
        "ux": ("feels fast", "smooth", "delightful", "polished ux", "snappy"),
        "throughput": ("high throughput", "thousands of requests", "low latency", "under load", "rps", "p99"),
        "seo": ("seo", "search rankings", "core web vitals", "google index", "organic traffic", "meta tags"),
        "cost": ("cheap to run", "low cost", "free tier", "minimise hosting", "budget hosting"),
        "offline": ("offline", "works without internet", "spotty connection", "air-gapped"),
        "scalability": ("scale to millions", "horizontal scaling", "millions of users", "high traffic", "grow to"),
        "maintainability": ("easy to maintain", "maintainable", "clean code", "simple to extend"),
        "security": ("secure", "security first", "threat model", "pen test", "hardened", "least privilege"),
        "dx": ("developer experience", "easy to contribute", "good docs for developers", "fast local setup"),
        "conversion": ("conversion rate", "signups", "funnel", "drop-off", "activation rate"),
        "a11y": ("accessibility", "accessible", "screen reader", "wcag", "section 508"),
    },
    "accessibility": {
        "none": ("no accessibility requirement", "accessibility is not required", "internal only, no a11y"),
        "basic": ("semantic html", "keyboard navigable", "basic accessibility"),
        "aa": ("wcag aa", "wcag 2.2 aa", "aa compliance", "accessibility audit"),
        "strict": ("wcag aaa", "legal requirement", "section 508", "public sector", "court order"),
    },
    "testing_bar": {
        "none": ("no tests", "skip the tests", "without tests", "don't write tests"),
        "smoke": ("smoke test", "happy path test", "one test", "basic test"),
        "balanced": ("unit tests", "integration tests", "test coverage", "pytest suite", "a test suite"),
        "thorough": ("full test suite", "end-to-end tests", "e2e", "browser tests", "playwright tests", "coverage report"),
        "tdd": ("tdd", "test first", "test driven", "write the tests before"),
    },
    # -- deployment --------------------------------------------------------- #
    "deployment": {
        "managed-paas": (
            "vercel", "netlify", "fly.io", "railway", "render.com", "heroku", "managed platform", "paas",
            "deploy to fly", "cloudflare pages",
        ),
        "container": ("docker", "kubernetes", "k8s", "ecs", "containerised", "containerized", "helm", "docker compose"),
        "vps": ("vps", "single server", "digitalocean", "linode", "hetzner", "ec2 instance", "bare metal"),
        "serverless-cloud": ("aws lambda", "cloud run", "azure functions", "serverless deploy"),
        "self-host": ("self-hosted", "self hosted", "on-premise", "on prem", "customer infrastructure", "air-gapped install"),
        "static-cdn": ("static hosting", "cdn", "github pages", "s3 static", "netlify static"),
        "local-only": ("runs locally", "local only", "on my machine", "never deployed", "developer machine only"),
        "undecided": ("not sure where to deploy", "deployment undecided", "hosting tbd", "where to host"),
    },
    # -- business ----------------------------------------------------------- #
    "monetization": {
        "none": ("not for sale", "no monetisation", "no monetization", "purely internal", "free tool"),
        "free": ("free and open", "free public", "open to everyone", "no charge"),
        "subscription": ("subscription", "monthly plan", "recurring billing", "saas pricing", "per month", "annual plan"),
        "usage": ("usage based", "pay per use", "metered billing", "per token", "credit system", "pay as you go"),
        "one-time": ("one-time purchase", "one off licence", "lifetime deal", "single purchase", "perpetual licence"),
        "freemium": ("freemium", "free tier with limits", "free plan with limits", "upgrade to pro"),
        "commerce": ("online store", "ecommerce", "e-commerce", "shopping cart", "sell products", "physical goods", "shopify"),
        "ads": ("ad supported", "advertising revenue", "display ads", "sponsored placements"),
        "internal": ("internal tool", "cost centre", "cost center", "for our own use", "not customer facing"),
    },
    "maintenance": {
        "throwaway": ("throwaway", "one-off script", "just for me today", "disposable", "hack together"),
        "solo": ("just me", "solo project", "side project", "personal project", "i maintain it"),
        "team": ("our team", "small team", "the devs", "our engineers", "team of five"),
        "handover": ("hand over", "handover", "someone else will maintain", "take it over", "onboard a new engineer"),
        "oss": ("open source", "open-source", "public repo", "contributors", "mit licence", "apache licence"),
        "agency": ("for a client", "client project", "agency work", "deliverable to the customer", "statement of work"),
    },
    # -- explicit exclusions ------------------------------------------------ #
    "non_goals": {
        "no-auth": ("no authentication", "no login", "skip auth", "without accounts"),
        "no-payments": ("no payments", "skip billing", "no checkout", "without stripe"),
        "no-admin": ("no admin panel", "no back office", "skip the admin"),
        "no-i18n": ("english only", "no internationalisation", "no i18n", "single language", "no translations"),
        "no-mobile": ("desktop only", "no mobile", "not mobile friendly", "ignore mobile"),
        "no-analytics": ("no analytics", "no tracking", "without analytics"),
        "no-tests": ("no tests", "skip the tests", "without a test suite"),
    },
    # -- stated technology -------------------------------------------------- #
    "tech_preference": {
        "python": ("python", "django", "flask", "fastapi", "pydantic", "uvicorn", "starlette"),
        "typescript": ("typescript", "node.js", "nodejs", "express", "nestjs", "bun ", "deno"),
        "react": ("react", "next.js", "remix", "vite react"),
        "vue": ("vue", "nuxt", "vue 3"),
        "svelte": ("svelte", "sveltekit"),
        "go": ("golang", "go service", " go ", "gin framework", "echo framework"),
        "rust": ("rust", "cargo", "actix", "axum"),
        "existing-stack": (
            "match the existing", "same stack", "existing stack", "consistent with the repo",
            "what we already use", "follow the repo conventions",
        ),
    },
    # -- the underlying problem -------------------------------------------- #
    "problem": {
        "save-time": ("automate", "save time", "stop doing this manually", "manual process", "spreadsheet every week", "repetitive task"),
        "organize": ("keep track of", "organise", "organize", "catalogue", "inventory of", "knowledge base", "notes and docs"),
        "sell": ("sell", "store to buy", "checkout", "customers pay", "take payments", "bookings"),
        "communicate": ("publish", "blog", "announce", "newsletter", "marketing site", "portfolio", "share my work"),
        "monitor": ("monitor", "alert me", "dashboard of metrics", "track uptime", "watch for", "observability"),
        "analyze": ("analyse", "analyze", "report on", "insights from", "data analysis", "visualise data"),
        "create": ("generate", "create content", "draft", "compose", "summarise", "translate text"),
        "connect": ("integrate", "sync between", "connect our", "bridge", "import from", "export to"),
    },
}


#: Needles that name a *specific* thing: a product, a standard, a platform.
#:
#: Scoring gives a single-word needle 2 points and a phrase 3, and
#: :func:`~unknown_agent.planning.clarify.infer_answers` only answers an axis at 3
#: or more — so one mention of a generic word ("api", "auth", "internal") can never
#: decide a high-impact question on its own.  That is deliberate.  But "uses JWT
#: auth" or "deploys to Fly.io" is not a generic word: it is a decision the user
#: already made, and asking them to make it again is exactly the failure mode the
#: clarification flow exists to avoid.  A needle listed here scores like a phrase.
SPECIFIC_NEEDLES: frozenset[str] = frozenset(
    {
        # databases and stores
        "postgres", "postgresql", "mysql", "mariadb", "sqlite", "duckdb", "mongodb", "dynamodb",
        "redis", "elasticsearch", "opensearch", "algolia", "meilisearch", "typesense", "memcached",
        # auth
        "saml", "okta", "auth0", "clerk", "workos", "webauthn", "passkey", "oauth", "cognito",
        # payments and money
        "stripe", "paypal", "adyen", "braintree", "paddle",
        # hosting and delivery
        "vercel", "netlify", "fly.io", "railway", "heroku", "render.com", "kubernetes", "docker",
        "helm", "aws lambda", "cloud run", "digitalocean", "hetzner", "linode",
        # platforms and stacks
        "django", "flask", "fastapi", "rails", "laravel", "next.js", "nuxt", "sveltekit",
        "typescript", "golang", "react native", "flutter", "electron", "supabase", "firebase",
        # compliance and standards
        "hipaa", "gdpr", "pci-dss", "wcag", "section 508", "sox",
        # integrations
        "sendgrid", "postmark", "mailgun", "plausible", "mixpanel", "amplitude", "posthog",
        "webhook", "webhooks",
        # AI
        "openai", "anthropic", "claude", "llm",
    }
)


def is_specific_needle(needle: str) -> bool:
    """Whether a needle names something specific rather than describing a quality."""

    return needle.strip().lower() in SPECIFIC_NEEDLES


def apply_inference_vocabulary(axes: Mapping[str, Any]) -> dict[str, Any]:
    """Attach the vocabulary to the axes, preserving anything already declared.

    ``app_type`` ships its own inference table inline because it is the axis the
    whole flow pivots on; this merge extends rather than replaces it, so both
    sources stay authoritative for what they cover.
    """

    updated: dict[str, Any] = {}
    for axis_id, axis in axes.items():
        table = INFER_FROM.get(axis_id, {})
        if not table:
            updated[axis_id] = axis
            continue
        merged: dict[str, tuple[str, ...]] = {key: tuple(value) for key, value in dict(axis.infer_from).items()}
        for option_id, needles in table.items():
            existing = merged.get(option_id, ())
            merged[option_id] = tuple(existing) + tuple(needle for needle in needles if needle not in existing)
        updated[axis_id] = replace(axis, infer_from=merged)
    return updated
