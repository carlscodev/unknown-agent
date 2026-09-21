"""The curated decision catalogue: what Unknown Agent knows about building software.

This file is the product's *taste*, expressed as data.  Each axis carries options
with real consequences (``implications``), real follow-on effects (``presets``,
``unlocks``, ``locks_out``) and honest effort/cost/risk numbers.  It exists so the
clarification flow can offer "choose one of these, here is what each means"
instead of "please describe your project in a text box".

Authoring rules for new axes:

1. **Options must be mutually comprehensible.**  A user who knows nothing about
   backends should still be able to pick sensibly from ``backend_style``, which
   means each option's ``description`` states the tradeoff in product terms.
2. **Every option needs a consequence.**  If you cannot say what choosing it
   commits the user to, it is not an option yet.
3. **Recommend exactly one default.**  ``recommended=True`` on one option only;
   the recommendation engine may override it with evidence, but there is always a
   safe answer for a user who just presses Enter.
4. **Declare dependencies.**  ``applies_when``/``skip_when`` keep the question
   count low: a static site is never asked about authentication strategy.
"""

from __future__ import annotations

from typing import Any

from unknown_agent.planning.models import DecisionAxis, DecisionOption

__all__ = ["AXES", "AXIS_ORDER", "CATEGORIES", "get_axis", "axes_for", "catalog_payload", "missing_implications"]


def _o(
    id: str,
    label: str,
    description: str = "",
    *,
    tags: tuple[str, ...] = (),
    implications: tuple[str, ...] = (),
    effort: int = 2,
    cost: int = 1,
    risk: int = 1,
    recommended: bool = False,
    presets: dict[str, str] | None = None,
    unlocks: tuple[str, ...] = (),
    locks_out: tuple[str, ...] = (),
    weights: dict[str, float] | None = None,
    requires: dict[str, tuple[str, ...]] | None = None,
) -> DecisionOption:
    return DecisionOption(
        id=id,
        label=label,
        description=description,
        tags=tags,
        implications=implications,
        effort=effort,
        cost=cost,
        risk=risk,
        recommended=recommended,
        presets=presets or {},
        unlocks=unlocks,
        locks_out=locks_out,
        weights=weights or {},
        requires=requires or {},
    )


# --------------------------------------------------------------------------- #
# 1. what is it
# --------------------------------------------------------------------------- #
APP_TYPE = DecisionAxis(
    id="app_type",
    question="What kind of thing are we building?",
    category="product",
    blurb="This one answer eliminates half the questions that follow.",
    impact=5,
    ambiguity=0.95,
    default_option="web-app",
    consequence="Determines the runtime, the validation strategy and the whole tool set.",
    infer_from={
        "web-app": ("web app", "saas", "dashboard", "portal", "admin", "platform", "website with login"),
        "static-site": ("landing page", "marketing site", "portfolio", "docs site", "blog", "brochure"),
        "api": ("api", "backend service", "rest api", "graphql", "webhook", "microservice"),
        "cli-tool": ("cli", "command line", "terminal tool", "devtool", "script"),
        "desktop-app": ("desktop app", "electron", "native app", "menubar"),
        "mobile-app": ("mobile app", "ios", "android", "react native", "flutter"),
        "library": ("library", "sdk", "package", "module", "python package", "npm package"),
        "bot": ("bot", "telegram", "discord", "slack app", "agent"),
        "data-pipeline": ("pipeline", "etl", "scraping", "data processing", "batch job"),
        "game": ("game", "playable", "arcade"),
    },
    options=(
        _o(
            "web-app",
            "Web application",
            "Interactive product in the browser: routes, state, data, auth.",
            tags=("app", "saas", "dashboard", "portal"),
            implications=("Browser validation becomes a quality gate", "Routing and state management need decisions", "Deployment needs a server or an edge target"),
            effort=4,
            recommended=True,
            presets={"platform": "web"},
            weights={"interactivity": 1.0, "reach": 0.8},
        ),
        _o(
            "static-site",
            "Static / content site",
            "Pages and content, no server state. Fast, cheap, easy to host anywhere.",
            tags=("landing", "marketing", "portfolio", "docs", "blog"),
            implications=("No auth, no database by default", "SEO and performance become first-class gates", "Content model still needs designing"),
            effort=2,
            cost=0,
            presets={"platform": "web", "backend_style": "static", "auth_strategy": "none", "performance_priority": "seo"},
            locks_out=("auth_strategy", "data_strategy"),
            weights={"speed-to-launch": 1.0, "seo": 1.0},
        ),
        _o(
            "api",
            "API / backend service",
            "HTTP(S) contract consumed by other software. No UI of its own.",
            tags=("api", "service", "backend", "rest", "graphql"),
            implications=("Contract and versioning matter more than pixels", "Tests and observability are the product", "Browser validation is replaced by request/response assertions"),
            effort=3,
            presets={"platform": "server", "design_direction": "dense-technical"},
            locks_out=("design_direction",),
            weights={"maintainability": 1.0, "scalability": 0.8},
        ),
        _o(
            "cli-tool",
            "CLI / terminal tool",
            "A command people run. Flags, structured output, exit codes, docs.",
            tags=("cli", "terminal", "command", "devtool", "automation"),
            implications=("UX is flags, output formatting and exit codes", "No browser gate; shell and integration tests instead", "Packaging and distribution become real work"),
            effort=2,
            presets={"platform": "cli", "design_direction": "dense-technical", "backend_style": "local"},
            locks_out=("design_direction", "deployment"),
            weights={"dx": 1.0, "speed-to-launch": 0.7},
        ),
        _o(
            "desktop-app",
            "Desktop application",
            "Native-feeling app that runs on the user's machine.",
            tags=("desktop", "electron", "tauri", "native", "menubar"),
            implications=("Packaging, signing and update channels are in scope", "OS integration (files, notifications) needs permissions", "Browser validation still works for web-based shells"),
            effort=4,
            cost=2,
            presets={"platform": "desktop"},
            weights={"offline": 1.0, "ux": 0.8},
        ),
        _o(
            "mobile-app",
            "Mobile application",
            "iOS/Android client, usually cross-platform.",
            tags=("mobile", "ios", "android", "react native", "flutter"),
            implications=("Store review and signing enter the timeline", "Offline-first data is usually mandatory", "Real browser validation is limited; emulators needed for full coverage"),
            effort=5,
            cost=2,
            risk=3,
            presets={"platform": "mobile", "data_strategy": "local-first"},
            weights={"reach": 1.0, "offline": 0.9},
        ),
        _o(
            "library",
            "Library / SDK",
            "Code other developers import. The API *is* the product.",
            tags=("library", "sdk", "package", "module", "plugin"),
            implications=("Public API design and semver discipline are the core work", "Docs and examples are mandatory deliverables", "Test matrix across supported runtimes"),
            effort=3,
            presets={"platform": "library", "design_direction": "dense-technical"},
            locks_out=("design_direction", "monetization"),
            weights={"dx": 1.0, "maintainability": 1.0},
        ),
        _o(
            "bot",
            "Bot / conversational agent",
            "Lives inside someone else's surface: Slack, Telegram, Discord, web widget.",
            tags=("bot", "telegram", "discord", "slack", "chat"),
            implications=("Platform API and webhook verification are in scope", "Conversation state and rate limits need design", "Deployment is usually a long-running worker"),
            effort=3,
            presets={"backend_style": "serverless", "integrations": "third-party"},
            weights={"interactivity": 0.9, "speed-to-launch": 0.8},
        ),
        _o(
            "data-pipeline",
            "Data pipeline / job",
            "Moves and transforms data on a schedule or a trigger.",
            tags=("etl", "pipeline", "scraping", "batch", "cron", "data"),
            implications=("Idempotency and retry semantics are the design", "Observability means run logs and row counts", "No UI; validation is data assertions"),
            effort=3,
            presets={"platform": "server", "backend_style": "api-only", "performance_priority": "scalability"},
            locks_out=("design_direction",),
            weights={"scalability": 1.0, "maintainability": 0.8},
        ),
        _o(
            "game",
            "Game / interactive experience",
            "A playable thing: loop, input, feedback, feel.",
            tags=("game", "playable", "arcade", "canvas", "webgl"),
            implications=("Frame budget and input latency are quality gates", "Browser validation covers rendering, not 'fun'", "Asset pipeline may be needed"),
            effort=4,
            risk=3,
            presets={"platform": "web", "performance_priority": "ux"},
            weights={"ux": 1.0, "interactivity": 1.0},
        ),
    ),
)

PLATFORM = DecisionAxis(
    id="platform",
    question="Where will people actually run it?",
    category="product",
    impact=4,
    ambiguity=0.5,
    default_option="web",
    consequence="Constrains the stack, the packaging and the validation strategy.",
    applies_when={"app_type": ("web-app", "static-site", "api", "cli-tool", "desktop-app", "mobile-app", "library", "bot", "data-pipeline", "game")},
    options=(
        _o("web", "Browser (any device)", "One URL, no install. The widest reach and the easiest validation.", recommended=True, weights={"reach": 1.0}),
        _o("mobile-web", "Mobile browser first", "Responsive, thumb-friendly, data-conscious.", weights={"reach": 0.9, "ux": 0.8}),
        _o("desktop-os", "Desktop OS install", "Native integration, offline by default.", effort=3, weights={"offline": 1.0}),
        _o("mobile-os", "iOS / Android", "Store distribution, push, device APIs.", effort=5, risk=3, weights={"reach": 1.0}),
        _o("server", "Server / headless", "No human UI: an API, a worker or a job.", weights={"scalability": 1.0}),
        _o("cli", "Terminal", "Runs where a shell runs.", weights={"dx": 1.0}),
        _o("embedded", "Embedded / edge", "Constrained runtime, tiny footprint.", effort=4, risk=4),
    ),
)

AUDIENCE = DecisionAxis(
    id="audience",
    question="Who is this for?",
    category="product",
    blurb="Audience sets the visual language, the copy register and the accessibility bar.",
    impact=4,
    ambiguity=0.85,
    default_option="consumers",
    consequence="Drives design direction, copy tone, and how forgiving the UX must be.",
    options=(
        _o("consumers", "General consumers", "Strangers with no patience. Clarity and speed beat power.", recommended=True, presets={"design_direction": "soft-consumer"}, weights={"ux": 1.0}),
        _o("developers", "Developers", "Dense information, keyboard-first, honest errors, code samples.", presets={"design_direction": "dense-technical"}, weights={"dx": 1.0}),
        _o("internal-team", "An internal team", "Efficiency over beauty; bulk actions, filters, audit trails.", presets={"design_direction": "dense-technical", "auth_strategy": "sso"}, weights={"maintainability": 0.9}),
        _o("enterprise", "Enterprise buyers", "Security review, SSO, compliance evidence, procurement paperwork.", effort=4, risk=2, presets={"auth_strategy": "sso"}, weights={"security": 1.0}),
        _o("creators", "Creators / prosumers", "Expressive tools, undo everywhere, fast feedback loops.", weights={"ux": 0.9, "interactivity": 0.8}),
        _o("kids", "Children / education", "Legibility, safety, no dark patterns, strict accessibility.", risk=3, presets={"accessibility": "strict"}, weights={"a11y": 1.0}),
        _o("regulated", "A regulated industry", "Health, finance or government: auditability is a feature.", risk=4, effort=4, presets={"auth_strategy": "sso", "accessibility": "strict"}, weights={"security": 1.0}),
    ),
)

PROBLEM = DecisionAxis(
    id="problem",
    question="What problem does it solve?",
    category="product",
    impact=5,
    ambiguity=0.9,
    default_option="custom",
    default_boost=1.6,
    allow_custom=True,
    prefers_text=True,
    consequence="The one answer that cannot be defaulted well — it is the reason the project exists.",
    options=(
        _o("save-time", "Save time on a repeated task", "Automation of something a human does manually today.", recommended=True, weights={"speed-to-launch": 0.8}),
        _o("organize", "Organise information", "Records, search, relationships, history.", weights={"maintainability": 0.8}),
        _o("sell", "Sell something", "Catalogue, conversion, payments, fulfilment.", effort=4, presets={"monetization": "commerce"}, weights={"conversion": 1.0}),
        _o("communicate", "Communicate / publish", "Reach an audience with content or a message.", presets={"performance_priority": "seo"}, weights={"seo": 1.0}),
        _o("monitor", "Monitor or control something", "Telemetry, alerts, dashboards, actuation.", weights={"scalability": 0.9}),
        _o("analyze", "Analyse data", "Ingest, transform, visualise, decide.", effort=4, weights={"scalability": 0.9}),
        _o("create", "Make or generate things", "Editors, generators, AI-assisted creation.", weights={"interactivity": 1.0}),
        _o("connect", "Connect systems", "Integrations, sync, webhooks, migrations.", weights={"maintainability": 0.9}),
        _o("custom", "Something else — I'll describe it", "Free text. The engine will re-read it for signals on every other axis.", risk=0),
    ),
)

# --------------------------------------------------------------------------- #
# 2. how it should look
# --------------------------------------------------------------------------- #
DESIGN_DIRECTION = DecisionAxis(
    id="design_direction",
    question="Which visual direction should it take?",
    category="design",
    blurb="Curated, opinionated starting points — each one sets spacing, type scale, colour temperature and motion.",
    impact=4,
    ambiguity=0.95,
    default_option="minimal-editorial",
    consequence="Sets the design tokens the whole UI is built from.",
    skip_when={"app_type": ("api", "library", "data-pipeline")},
    options=(
        _o(
            "minimal-editorial",
            "Minimal editorial",
            "Generous whitespace, strong type hierarchy, one accent colour, no decoration.",
            tags=("minimal", "clean", "simple", "elegant", "editorial"),
            implications=("Content model must be good — there is nowhere to hide", "Fast to build and easy to keep consistent"),
            recommended=True,
            weights={"speed-to-launch": 0.8, "ux": 0.8},
        ),
        _o(
            "bold-expressive",
            "Bold & expressive",
            "Oversized type, saturated colour, strong motion. Built to be screenshotted.",
            tags=("bold", "vibrant", "loud", "playful", "fun", "colorful"),
            implications=("Needs restraint somewhere or it becomes noise", "Motion and layout work is real effort"),
            effort=3,
            weights={"ux": 1.0},
        ),
        _o(
            "glass-neumorphic",
            "Glass / depth",
            "Translucency, layering, soft shadows, blurred backgrounds.",
            tags=("glass", "glassmorphism", "frosted", "depth", "modern"),
            implications=("Contrast must be verified — accessibility risk", "Heavier rendering; test on low-end devices"),
            effort=3,
            risk=2,
            weights={"ux": 0.9},
        ),
        _o(
            "brutalist",
            "Brutalist / raw",
            "System fonts, hard edges, visible structure, monochrome with one alarm colour.",
            tags=("brutalist", "raw", "indie", "retro", "terminal"),
            implications=("Very fast to build, polarising to look at", "Great for devtools and internal things"),
            effort=1,
            weights={"dx": 0.9, "speed-to-launch": 0.9},
        ),
        _o(
            "dense-technical",
            "Dense technical",
            "Information-dense tables, monospace accents, keyboard-first, minimal chrome.",
            tags=("dense", "technical", "data", "pro", "terminal", "admin"),
            implications=("Needs a real grid and spacing system to stay readable", "Best for power users, worst for first-timers"),
            effort=2,
            weights={"dx": 1.0, "maintainability": 0.8},
        ),
        _o(
            "soft-consumer",
            "Soft consumer",
            "Rounded corners, pastel palette, friendly copy, big tap targets.",
            tags=("friendly", "soft", "consumer", "warm", "rounded"),
            implications=("Mobile-first spacing rules", "Illustration/empty states matter more"),
            effort=2,
            weights={"ux": 0.9, "a11y": 0.7},
        ),
        _o(
            "playful-illustrated",
            "Playful & illustrated",
            "Illustrations, mascots, micro-animations, personality in the copy.",
            tags=("playful", "illustrated", "fun", "mascot", "kids"),
            implications=("Needs art direction or it looks accidental", "Asset production is on the critical path"),
            effort=4,
            cost=2,
            weights={"ux": 1.0},
        ),
        _o(
            "premium-dark",
            "Premium dark / luxe",
            "Dark surfaces, metallic or neon accents, tight motion, high production value.",
            tags=("dark", "premium", "luxury", "sleek", "neon", "crypto"),
            implications=("Contrast and focus states need real attention", "Light-mode fallback still has to exist"),
            effort=3,
            weights={"ux": 0.9},
        ),
        _o(
            "accessible-first",
            "Accessible-first",
            "WCAG AA/AAA contrast, visible focus, reduced-motion support, semantic HTML leads.",
            tags=("accessible", "a11y", "inclusive", "wcag", "government"),
            implications=("Constraints on colour and motion — decided up front, not retrofitted", "Often a legal requirement"),
            effort=2,
            risk=1,
            presets={"accessibility": "strict"},
            weights={"a11y": 1.0},
        ),
        _o(
            "custom-design",
            "I'll describe it",
            "Free text. Paste a reference URL, a vibe, a brand — it becomes a design brief.",
            tags=("custom",),
            implications=("The description is turned into concrete tokens before any UI is written",),
            risk=0,
        ),
    ),
)

DESIGN_REFERENCE = DecisionAxis(
    id="design_reference",
    question="Anything to anchor the design to?",
    category="design",
    impact=2,
    ambiguity=0.7,
    default_option="none",
    allow_custom=True,
    prefers_text=True,
    consequence="A reference removes most design guesswork.",
    applies_when={"design_direction": ("custom-design", "minimal-editorial", "bold-expressive", "premium-dark", "soft-consumer")},
    options=(
        _o("none", "No reference — you choose", "The agent picks tokens consistent with the direction and audience.", recommended=True),
        _o("brand", "I have a brand (colours, logo, fonts)", "Provide them; every surface will use the brand tokens."),
        _o("competitor", "Make it feel like an existing product", "Name it; the agent extracts the *patterns*, not the pixels."),
        _o("url", "Here is a URL to imitate", "Fetched and analysed for layout, spacing and type scale."),
        _o("mood", "I have a mood, not a reference", "Describe it in a sentence."),
    ),
)

BRAND_COLOR = DecisionAxis(
    id="brand_color",
    question="Preferred accent colour?",
    category="design",
    impact=1,
    ambiguity=0.8,
    default_option="agent-choice",
    allow_custom=True,
    consequence="Small decision, high visibility — worth 5 seconds.",
    skip_when={"app_type": ("api", "library", "data-pipeline", "cli-tool")},
    options=(
        _o("agent-choice", "You choose", "Picked for contrast against the chosen surfaces.", recommended=True),
        _o("blue", "Blue / indigo", "Trustworthy, default for B2B."),
        _o("teal", "Teal / cyan", "Technical, modern, calm."),
        _o("green", "Green", "Growth, money, health."),
        _o("violet", "Violet / purple", "Creative, AI, premium."),
        _o("orange", "Orange / amber", "Energetic, consumer, commerce."),
        _o("red", "Red / rose", "Urgent, bold, editorial."),
        _o("mono", "Monochrome", "No accent; structure does the work."),
    ),
)

# --------------------------------------------------------------------------- #
# 3. how much of it
# --------------------------------------------------------------------------- #
FEATURE_DEPTH = DecisionAxis(
    id="feature_depth",
    question="How deep should the first version go?",
    category="scope",
    impact=5,
    ambiguity=0.9,
    default_option="core",
    consequence="The single biggest driver of effort and of whether the thing ships.",
    options=(
        _o("prototype", "Prototype", "One happy path, fake data allowed, no error handling beyond the basics.", effort=1, weights={"speed-to-launch": 1.0}),
        _o("mvp", "MVP", "The smallest thing a real user can complete end to end, with honest errors.", effort=2, recommended=True, weights={"speed-to-launch": 0.9}),
        _o("core", "Core + polish", "MVP plus empty states, validation, loading, settings and tests.", effort=3, weights={"ux": 0.8, "maintainability": 0.7}),
        _o("full", "Full product", "Roles, billing, notifications, admin, analytics, i18n-ready.", effort=4, cost=2, weights={"reach": 0.9}),
        _o("platform", "Platform", "Multi-tenant, API for third parties, plugin surface, SLAs.", effort=5, cost=4, risk=3, weights={"scalability": 1.0}),
    ),
)

SCOPE_LEVEL = DecisionAxis(
    id="scope_level",
    question="What complexity should we assume?",
    category="scope",
    impact=4,
    ambiguity=0.85,
    default_option="mvp",
    consequence="Sets step budgets, how much planning happens, and what 'done' means.",
    options=(
        _o("hours", "A few hours", "One sitting. Tight scope, no ceremony.", effort=1, weights={"speed-to-launch": 1.0}),
        _o("days", "A few days", "Small project, a handful of files.", effort=2),
        _o("weeks-1-2", "1–2 weeks", "A real feature set with tests and docs.", effort=3, recommended=True),
        _o("months-1-3", "1–3 months", "Multiple subsystems, staged delivery.", effort=4, risk=2),
        _o("open-ended", "Open-ended / evolving", "Design for change: module boundaries and migration paths matter most.", effort=4, weights={"maintainability": 1.0}),
    ),
)

NON_GOALS = DecisionAxis(
    id="non_goals",
    question="What should we deliberately *not* build?",
    category="scope",
    impact=3,
    ambiguity=0.8,
    default_option="agent-decides",
    multi=True,
    allow_custom=True,
    consequence="Prevents the most common failure: a plan that quietly doubles in size.",
    options=(
        _o("agent-decides", "You decide", "The agent proposes non-goals in the blueprint for approval.", recommended=True),
        _o("no-auth", "No authentication yet"),
        _o("no-payments", "No payments yet"),
        _o("no-admin", "No admin panel yet"),
        _o("no-i18n", "No internationalisation yet"),
        _o("no-mobile", "No mobile optimisation yet"),
        _o("no-analytics", "No analytics yet"),
        _o("no-tests", "No test suite (fastest path, weakest safety)", risk=4),
    ),
)

# --------------------------------------------------------------------------- #
# 4. how it is built
# --------------------------------------------------------------------------- #
BACKEND_STYLE = DecisionAxis(
    id="backend_style",
    question="What should the backend look like?",
    category="architecture",
    blurb="Each option states the operational cost, not just the technology.",
    impact=5,
    ambiguity=0.9,
    default_option="server-fullstack",
    consequence="Decides where state lives, how it is deployed and what can break at 3am.",
    skip_when={"app_type": ("library",)},
    options=(
        _o("static", "None — static only", "Files and a CDN. Nothing to operate, nothing to scale.", effort=1, cost=0, presets={"data_strategy": "none"}, weights={"speed-to-launch": 1.0, "cost": 1.0}),
        _o("server-fullstack", "Full-stack server framework", "One app owns routes, data and rendering. Simplest coherent choice.", effort=3, recommended=True, weights={"maintainability": 0.9}),
        _o("api-only", "Separate API + client", "Clean contract, independently deployable, more moving parts.", effort=3, weights={"scalability": 0.9}),
        _o("serverless", "Serverless functions", "Scale to zero, per-request billing, cold starts and vendor lock-in.", effort=3, cost=2, weights={"cost": 0.8, "scalability": 0.8}),
        _o("baas", "Backend-as-a-service", "Auth, database and storage from a vendor (Supabase/Firebase). Fastest to real.", effort=2, cost=2, risk=2, presets={"auth_strategy": "managed-auth"}, weights={"speed-to-launch": 1.0}),
        _o("local", "Local-first / no server", "State on the device; sync optional. Offline by construction.", effort=3, presets={"data_strategy": "local-first"}, weights={"offline": 1.0}),
        _o("microservices", "Microservices", "Independent services per domain. Only worth it with a team and a reason.", effort=5, cost=4, risk=4, weights={"scalability": 1.0}),
        _o("bff", "BFF (backend for frontend)", "A thin aggregation layer tuned to one client.", effort=3, weights={"ux": 0.7}),
    ),
)

DATA_STRATEGY = DecisionAxis(
    id="data_strategy",
    question="Where does the data live?",
    category="architecture",
    impact=4,
    ambiguity=0.85,
    default_option="sql",
    consequence="Sets the migration story, the backup story and the query patterns.",
    skip_when={"app_type": ("static-site", "library"), "backend_style": ("static",)},
    options=(
        _o("none", "No persistence", "Nothing is stored between sessions.", effort=0, cost=0, recommended=False),
        _o("local-first", "On the device", "SQLite/IndexedDB/files. Offline works, sync is a later decision.", effort=2, weights={"offline": 1.0}),
        _o("sql", "Managed SQL", "Postgres/MySQL. Relationships, transactions, migrations. The default for a reason.", effort=2, recommended=True, weights={"maintainability": 1.0}),
        _o("nosql", "Document store", "Flexible schemas, fast iteration, weaker integrity guarantees.", effort=2, risk=2, weights={"speed-to-launch": 0.8}),
        _o("kv-cache", "Key-value + cache", "Sessions, counters, queues. Usually an addition, not the whole store.", effort=2),
        _o("search-index", "Search index", "Full-text or vector search as the primary access path.", effort=3, cost=2),
        _o("files", "Files / object storage", "Blobs, media, exports.", effort=2, cost=1),
        _o("hybrid", "Hybrid (SQL + object store)", "Structured rows plus blobs. The honest answer for most real products.", effort=3, cost=1, weights={"maintainability": 0.9}),
    ),
)

AUTH_STRATEGY = DecisionAxis(
    id="auth_strategy",
    question="How do people sign in?",
    category="architecture",
    blurb="Authentication is where side projects go to die — choosing deliberately is the point.",
    impact=4,
    ambiguity=0.9,
    default_option="managed-auth",
    consequence="Determines the user model, the security surface and a chunk of the timeline.",
    skip_when={"app_type": ("static-site", "library", "cli-tool", "data-pipeline"), "backend_style": ("static", "local")},
    options=(
        _o("none", "No accounts", "Anonymous use. Nothing to secure, nothing to recover.", effort=0, risk=0, recommended=False, weights={"speed-to-launch": 1.0}),
        _o("managed-auth", "Managed auth provider", "Clerk/Auth0/Supabase Auth. Days instead of weeks, monthly bill, vendor dependency.", effort=1, cost=2, recommended=True, weights={"speed-to-launch": 1.0, "security": 0.7}),
        _o("email-password", "Email + password (self-hosted)", "Full control, and you own hashing, resets, rate limiting and breach response.", effort=3, risk=3, weights={"security": 0.8, "cost": 0.8}),
        _o("magic-link", "Magic link / OTP", "No passwords to store. Needs an email provider that actually delivers.", effort=2, risk=1, weights={"ux": 0.9}),
        _o("social-oauth", "Social OAuth", "Google/GitHub/Apple. Low friction, third-party dependency, email collisions to handle.", effort=2, weights={"ux": 1.0}),
        _o("passkeys", "Passkeys / WebAuthn", "Modern, phishing-resistant, still needs a fallback path.", effort=3, risk=1, weights={"security": 1.0}),
        _o("sso", "SSO / SAML / OIDC", "Enterprise requirement. Long tail of per-customer configuration.", effort=4, cost=2, risk=2, weights={"security": 1.0}),
        _o("api-keys", "API keys / tokens", "Machine callers. Needs scoping, rotation and revocation.", effort=2, weights={"dx": 0.9}),
    ),
)

INTEGRATIONS = DecisionAxis(
    id="integrations",
    question="What does it need to talk to?",
    category="architecture",
    impact=3,
    ambiguity=0.75,
    default_option="none",
    multi=True,
    allow_custom=True,
    consequence="Every integration adds a failure mode, a secret and a test double.",
    options=(
        _o("none", "Nothing yet", "Self-contained. Fewest secrets, fastest to green.", recommended=True),
        _o("payments", "Payments (Stripe et al.)", "Webhooks, idempotency and a sandbox account become requirements.", effort=3, risk=3),
        _o("email", "Email (transactional)", "Provider, templates, deliverability, unsubscribe handling.", effort=2),
        _o("analytics", "Analytics", "Event schema and consent handling.", effort=1),
        _o("third-party", "Third-party APIs", "Rate limits, retries, caching, and what to do when they are down.", effort=3, risk=2),
        _o("webhooks", "Inbound/outbound webhooks", "Signature verification, replay protection, retry semantics.", effort=3, risk=2),
        _o("internal-apis", "Internal services", "Service discovery, auth between services, contract tests.", effort=2),
        _o("push", "Push notifications", "Device tokens, APNs/FCM credentials, quiet hours.", effort=3, cost=1),
        _o("ai", "LLM / AI features", "Model choice, cost control, prompt injection defence, latency budgets.", effort=3, cost=3, risk=2),
    ),
)

PERFORMANCE_PRIORITY = DecisionAxis(
    id="performance_priority",
    question="What matters most when it is running?",
    category="quality",
    impact=3,
    ambiguity=0.8,
    default_option="balanced",
    consequence="Becomes a measured gate instead of an aspiration.",
    options=(
        _o("balanced", "Balanced", "Good defaults, no exotic engineering.", recommended=True),
        _o("speed-to-launch", "Ship fast", "Accept technical debt deliberately; note it in the blueprint.", weights={"speed-to-launch": 1.0}),
        _o("ux", "Perceived speed / UX", "Optimistic UI, skeletons, sub-100ms interactions.", effort=3, weights={"ux": 1.0}),
        _o("throughput", "Raw throughput", "Load targets, pooling, batching, backpressure.", effort=4, weights={"scalability": 1.0}),
        _o("seo", "SEO & Core Web Vitals", "Server rendering, real metadata, Lighthouse budgets as gates.", effort=3, weights={"seo": 1.0}),
        _o("cost", "Low operating cost", "Scale to zero, cheap storage, minimal third parties.", weights={"cost": 1.0}),
        _o("offline", "Offline capability", "Local-first data, sync conflict resolution.", effort=4, weights={"offline": 1.0}),
        _o("scalability", "Scale ceiling", "Horizontal scaling, multi-region, queueing.", effort=5, cost=3, weights={"scalability": 1.0}),
        _o("maintainability", "Maintainability", "Boundaries, tests and docs over cleverness.", weights={"maintainability": 1.0}),
        _o("security", "Security & compliance", "Threat model, audit log, least privilege, evidence.", effort=4, risk=1, weights={"security": 1.0}),
        _o("dx", "Developer experience", "Fast local loop, great errors, easy contribution.", weights={"dx": 1.0}),
        _o("conversion", "Conversion", "Funnel instrumentation and A/B readiness.", effort=3, weights={"conversion": 1.0}),
        _o("a11y", "Accessibility", "WCAG conformance as a blocking gate.", effort=2, weights={"a11y": 1.0}),
    ),
)

ACCESSIBILITY = DecisionAxis(
    id="accessibility",
    question="What accessibility bar must it clear?",
    category="quality",
    impact=2,
    ambiguity=0.85,
    default_option="aa",
    consequence="Retrofitting accessibility costs far more than designing it in.",
    skip_when={"app_type": ("api", "library", "data-pipeline")},
    options=(
        _o("none", "Not a requirement", "Internal tooling with no external users.", risk=3),
        _o("basic", "Basic (semantic HTML, focus order)", "The floor for anything a human uses.", effort=1),
        _o("aa", "WCAG 2.2 AA", "Contrast, keyboard, screen-reader support. The sane default.", effort=2, recommended=True),
        _o("strict", "AAA / legal requirement", "Public sector or regulated: evidence and testing required.", effort=4, risk=1),
    ),
)

TESTING_BAR = DecisionAxis(
    id="testing_bar",
    question="How much verification do you want before 'done'?",
    category="quality",
    impact=4,
    ambiguity=0.8,
    default_option="balanced",
    consequence="Maps directly onto Unknown Agent's definition-of-done and which gates block a run.",
    options=(
        _o("none", "None — just make it run", "Fastest, and the agent will say so in the report.", effort=0, risk=4),
        _o("smoke", "Smoke tests", "A few end-to-end paths prove the thing works.", effort=1),
        _o("balanced", "Unit + integration on the risky parts", "Coverage where it buys safety, not everywhere.", effort=2, recommended=True),
        _o("thorough", "Thorough suite + browser validation", "Every acceptance criterion has an executable check.", effort=3),
        _o("tdd", "Test-first (TDD)", "Tests written before implementation; red→green is the workflow.", effort=4, weights={"maintainability": 1.0}),
    ),
)

# --------------------------------------------------------------------------- #
# 5. how it ships
# --------------------------------------------------------------------------- #
DEPLOYMENT = DecisionAxis(
    id="deployment",
    question="Where will it be deployed?",
    category="delivery",
    impact=3,
    ambiguity=0.85,
    default_option="managed-paas",
    consequence="Determines the build output, the env-var story and the release workflow.",
    skip_when={"app_type": ("cli-tool", "library")},
    options=(
        _o("managed-paas", "Managed platform (Vercel/Netlify/Fly/Railway)", "Git push to deploy. Least operations, some lock-in.", effort=1, cost=1, recommended=True, weights={"speed-to-launch": 1.0}),
        _o("container", "Container (Docker/K8s/ECS)", "Portable and explicit; you own the platform.", effort=3, cost=2, weights={"scalability": 0.9}),
        _o("vps", "Single server / VPS", "Cheap, simple, one machine to babysit.", effort=2, cost=0, risk=2),
        _o("serverless-cloud", "Cloud serverless (Lambda/Cloud Run)", "Per-request cost, cold starts, IAM to configure.", effort=3, cost=2),
        _o("self-host", "Self-hosted / on-premise", "Air-gapped or customer-controlled: packaging and updates matter.", effort=4, risk=2),
        _o("static-cdn", "Static hosting / CDN", "Files only. Free tier usually suffices.", effort=1, cost=0, weights={"cost": 1.0}),
        _o("local-only", "Runs locally only", "No deployment story; distribution is the problem instead.", effort=1, cost=0),
        _o("undecided", "Not decided yet", "The agent picks the lowest-friction target and records the assumption.", effort=1),
    ),
)

MONETIZATION = DecisionAxis(
    id="monetization",
    question="Is there a business model in scope?",
    category="delivery",
    impact=2,
    ambiguity=0.9,
    default_option="none",
    consequence="Billing is a subsystem, not a feature — knowing early avoids a rewrite.",
    skip_when={"app_type": ("library", "cli-tool", "api", "data-pipeline")},
    options=(
        _o("none", "No — free / internal", "No billing code at all.", recommended=True),
        _o("free", "Free public product", "Cost control matters, billing does not."),
        _o("subscription", "Subscription", "Plans, trials, dunning, proration, invoices.", effort=4, cost=2, risk=2),
        _o("usage", "Usage-based", "Metering, aggregation, rate cards.", effort=4, risk=3),
        _o("one-time", "One-time purchase / licence", "Keys, activation, refunds.", effort=2),
        _o("freemium", "Freemium", "Entitlement checks threaded through the whole product.", effort=4, risk=2),
        _o("commerce", "Physical/digital commerce", "Catalogue, tax, fulfilment, disputes.", effort=5, risk=3),
        _o("ads", "Advertising", "Consent, ad slots, performance tradeoffs.", effort=3, risk=2),
        _o("internal", "Internal cost centre", "Chargeback reporting at most.", effort=1),
    ),
)

MAINTENANCE = DecisionAxis(
    id="maintenance",
    question="Who maintains it after this?",
    category="delivery",
    impact=3,
    ambiguity=0.85,
    default_option="solo",
    consequence="Sets how much the agent invests in docs, structure and test coverage.",
    options=(
        _o("throwaway", "Nobody — it's throwaway", "Optimise for the demo. The agent will not gold-plate.", effort=0, recommended=False),
        _o("solo", "Me, solo", "Clear structure and a README that explains decisions.", effort=2, recommended=True),
        _o("team", "A small team", "Conventions, review guidance, CI, contribution docs.", effort=3, weights={"maintainability": 1.0}),
        _o("handover", "Someone else takes it over", "Onboarding docs and explicit architecture notes are deliverables.", effort=3, weights={"maintainability": 1.0}),
        _o("oss", "Open-source community", "Licence, contributing guide, issue templates, public API stability.", effort=4),
        _o("agency", "Agency delivery to a client", "Acceptance criteria, handover pack, support window.", effort=4, risk=2),
    ),
)

TECH_PREFERENCE = DecisionAxis(
    id="tech_preference",
    question="Any technology preference or constraint?",
    category="delivery",
    impact=3,
    ambiguity=0.8,
    default_option="agent-choice",
    multi=True,
    allow_custom=True,
    consequence="A stated preference always beats the agent's default; silence means 'pick well'.",
    options=(
        _o("agent-choice", "You choose", "Selected for the app type, the team size and the deployment target.", recommended=True),
        _o("python", "Python", "FastAPI/Django/Flask, Pydantic, pytest.", tags=("python", "django", "flask", "fastapi", "pydantic", "pytest")),
        _o("typescript", "TypeScript / Node", "Next.js/Remix/Express, Vitest, strict TS.", tags=("typescript", "javascript", "node", "nextjs", "next.js", "remix", "express", "vitest")),
        _o("react", "React", "Component model, hooks, ecosystem depth.", tags=("react", "nextjs", "next.js", "remix")),
        _o("vue", "Vue", "Approachable reactivity, single-file components.", tags=("vue", "nuxt")),
        _o("svelte", "Svelte / SvelteKit", "Compiler-first, small bundles.", tags=("svelte", "sveltekit")),
        _o("go", "Go", "Single binary, concurrency, fast builds.", tags=("go", "golang")),
        _o("rust", "Rust", "Performance and correctness; slower to write.", tags=("rust", "tauri")),
        _o("existing-stack", "Match the existing repo", "Detected from the repository and never contradicted."),
    ),
)


# --------------------------------------------------------------------------- #
# the catalogue
# --------------------------------------------------------------------------- #
AXES: dict[str, DecisionAxis] = {}
AXIS_ORDER: tuple[str, ...] = (
    "app_type",
    "problem",
    "audience",
    "platform",
    "scope_level",
    "feature_depth",
    "design_direction",
    "design_reference",
    "brand_color",
    "backend_style",
    "data_strategy",
    "auth_strategy",
    "integrations",
    "tech_preference",
    "performance_priority",
    "accessibility",
    "testing_bar",
    "deployment",
    "monetization",
    "maintenance",
    "non_goals",
)

CATEGORIES: dict[str, str] = {
    "product": "Product",
    "design": "Design",
    "scope": "Scope",
    "architecture": "Architecture",
    "quality": "Quality",
    "delivery": "Delivery",
}

for _axis in (
    APP_TYPE,
    PLATFORM,
    AUDIENCE,
    PROBLEM,
    DESIGN_DIRECTION,
    DESIGN_REFERENCE,
    BRAND_COLOR,
    FEATURE_DEPTH,
    SCOPE_LEVEL,
    NON_GOALS,
    BACKEND_STYLE,
    DATA_STRATEGY,
    AUTH_STRATEGY,
    INTEGRATIONS,
    PERFORMANCE_PRIORITY,
    ACCESSIBILITY,
    TESTING_BAR,
    DEPLOYMENT,
    MONETIZATION,
    MAINTENANCE,
    TECH_PREFERENCE,
):
    AXES[_axis.id] = _axis

_MISSING = [axis for axis in AXIS_ORDER if axis not in AXES]
if _MISSING:  # pragma: no cover - guards the catalogue at import time
    raise RuntimeError(f"AXIS_ORDER references unknown axes: {_MISSING}")

# The consequence table is applied here rather than inline in every option so the
# copy can be revised in one place.  It is additive: an option that already
# declares its own implications keeps them.  See planning/implications.py for why
# every option must state what choosing it commits the user to.
from unknown_agent.planning.implications import apply_implications, missing_implications  # noqa: E402
from unknown_agent.planning.inference import apply_inference_vocabulary  # noqa: E402

AXES.update(apply_inference_vocabulary(AXES))
AXES.update(apply_implications(AXES))

_INCOMPLETE = missing_implications(AXES)
if _INCOMPLETE:  # pragma: no cover - guards the authoring rule at import time
    raise RuntimeError(f"options with no stated consequences: {', '.join(_INCOMPLETE)}")


def get_axis(axis_id: str) -> DecisionAxis:
    try:
        return AXES[axis_id]
    except KeyError:
        raise KeyError(f"unknown decision axis {axis_id!r}") from None


def axes_for(answers: dict[str, Any] | None = None) -> list[DecisionAxis]:
    """Axes still worth asking, in impact order, given what is already known."""

    known = answers or {}
    applicable = [axis for axis in (AXES[key] for key in AXIS_ORDER) if axis.applies(known)]
    return sorted(applicable, key=lambda axis: (-axis.impact, AXIS_ORDER.index(axis.id)))


def catalog_payload() -> dict[str, Any]:
    """The whole catalogue as JSON — for ``ua plan axes --json``, docs and the TUI."""

    return {
        "axes": [AXES[key].to_dict() for key in AXIS_ORDER],
        "categories": CATEGORIES,
        "order": list(AXIS_ORDER),
    }
