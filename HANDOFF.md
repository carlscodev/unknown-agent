# Unknown Agent — handoff notes

**Read this first.** It records what is finished, what is *not*, the exact APIs a
continuing agent must not guess at, and the traps already paid for.
Last updated 2026-09-20 (third session — config wiring + conductor-in-runner).

Working copy: `/home/user/work/unknown-agent`
Baseline: `cd /home/user/work/unknown-agent && python -m pytest -q` → **417 passed** (~10s)

There is **no git repository** in this copy (`git rev-parse` → not a work tree).
The workspace snapshot is the only backup. If you continue, consider
`git init && git add -A && git commit` first — note that `.git/config` is excluded
from snapshots, so a repo created here will come back without its config.

---

## 1. State right now

* **417 tests pass** (exact per-file counts, `--collect-only`): orchestration 76,
  skills 48, planning_deep 44, orchestration_executor 40, cli_orchestration 31,
  **runner_orchestration 25**, providers 24, **config_wiring 24**, agent 20,
  shell 17, cli 16, tools 13, onboarding 12, config 9, validation 8, memory 6,
  onboarding_vendors 4. The two bolded files are new this session.
  (An earlier revision of this document claimed "agent 26" — it is 20; the total
  was right, the split was not.)
* **Orchestration now has a real executor** (`orchestration/executor.py`,
  `AgentExecutor`) and `ua orchestrate run` uses it by default. The layer is no
  longer simulation-only.
* **`[skills]`, `[appearance]` and `[reasoning]` are now honoured at runtime**
  (§2.42–2.47). Skills reach the system prompt and enable their own validation
  gates; the configured theme/glyphs/colour-depth/motion reach the real Rich
  consoles (CLI flags beat config); the reasoning ladder is negotiated into
  per-vendor wire parameters at every `ModelRequest` site, with honest degradation.
* **`ua run` / `ua build` / `ua fix` now delegate to the Conductor** when the task
  warrants it (§2.49–2.53): the decision is explainable and overridable with
  `--orchestrate` / `--solo`, workers can never re-delegate, and the outcome is
  mapped back into `RunState` without flattering it. Verified end to end through
  the CLI: a six-worker graph, `orchestration.json` in the run journal, a ledger at
  `.unknown/orchestration/<run-id>.jsonl`, exit 3 (BLOCKED) because the offline
  provider cannot satisfy role output contracts.
* All 21 deliverables exist as modules. **One wiring gap remains (§3 P0.4)**: there
  is no `ua skills` command group.
* Playwright is **not installed** here; browser code is import-guarded and tested
  offline. Installed: `textual==8.2.8`, `typer`, `rich`, `pydantic`, `pytest`,
  `pytest-timeout`.
* Python 3.13 in the sandbox — remember f-strings cannot nest the same quote
  character (this bit twice).

## 2. Fixed this session (do not re-do)

### planning/ (earlier in this session)
1. Preset answers now apply in `plan_questions` **even when the blueprint already
   has answers** (was inside `if not answers:` → 6 questions instead of 3).
2. `synthesize()` reassigns `approaches = ranked` after the tie-break so rendered
   order always matches `chosen`.
3. New `_handles_money(blueprint)` + payments line in `_verification_for()`
   (webhooks / idempotency / refund / reconciliation gate).
4. `DeepPlanner.plan` is **async**.

### skills/
5. `resolve_skills` accepts `Settings` **or** `SkillsConfig` (duck-typed); before,
   a `SkillsConfig` silently fell back to defaults.
6. Unknown pack names reported in `SkillSet.unresolved` with the known-pack list.
7. **`expand_pack("none")` returned the default pack** (`or [defaults]` fallback).
   An empty member list is a statement, not an omission.
8. A skill file with **no `id`** used to take the filename stem silently → now
   rejected: `"<file>.toml: no id declared"`.
9. **Project-vs-builtin collision is explicit.** A project skill whose id matches a
   builtin is *ignored and reported* unless it declares `overrides = "<builtin-id>"`.
   Closes a prompt-injection surface (a clone could rewrite `test-smith`'s
   instructions with no visible diff).
10. `SkillLoader` accepts **any plausible directory spelling** (project root,
    `.unknown`, `.unknown/skills`; `~`, `~/.config`, `~/.config/unknown-agent`,
    leaf `skills`). Public helper `skills_dir(root, kind=…)`.
11. Trust model corrected: `trusted = origin != "project"`.
    `SkillSet.trusted()` / `.untrusted()` / `to_dict()["untrusted"]`.
12. A dropped-in skill is active **without** being in a pack (packs govern the
    builtin catalogue, not files the user chose to create).

### orchestration/
13. **The ledger was never written to disk** — `orchestrate()` passed
    `ledger_path=None`, so the documented "answer on disk" did not exist. Now
    defaults to `<root>/.unknown/orchestration/<run-id>.jsonl`, honours
    `[orchestration] ledger = false`, and a **dry run writes no file**.
14. **Unverified gates were indistinguishable from passed gates.** With no
    verifier, `verification` was `{}` and `ok=True`. Now each such worker records
    `{"ok": None, "checked": False, "gates": […], "note": …}`, appears in the new
    `OrchestrationOutcome.unverified`, and the summary says so.
15. `conflicting_pairs(items, *, respect_dependencies=True)` — the invariant is
    about *concurrency*; a dependency-ordered pair is not a conflict.
    `_serialise_conflicts` calls it with `respect_dependencies=False`.
16. Dry-run outcome validates the graph: `ok = not graph.validate()`.
17. `DEFAULT_MILESTONES` is a module constant (exported); the router's rationale
    now states the group count it actually builds (was hardcoded 1, built 2).
18. **`Worker.run()` could never be called standalone** — it went PENDING →
    RUNNING, which its own state machine rejects. It now queues first.
19. **`CallableExecutor` fabricated artefacts and ignored reported failures** (P0).
    It filled every name in `role.outputs` with the stringified return value and
    hardcoded `ok=True`. An adapter returning `{"ok": False}` was recorded as
    success, and `{"ok": True}` "produced" a patch/tests/notes it never wrote —
    defeating the required-outputs check and feeding fabricated content into the
    merge. Now: `WorkResult` passes through; a `Mapping` is interpreted
    (`ok`, `summary`, `artifacts`, `files_touched`, `evidence`, `error`,
    `follow_ups`, `steps_used`, `tokens_used`; unknown keys → `data`); any other
    value becomes the summary only. **Nothing is invented.**
20. **`can_retry` was true for SUCCEEDED items** (it only compared attempts).
    Terminal states are terminal now.
21. **A red gate crashed the whole run** (P0). `_verify` did
    `SUCCEEDED → QUEUED` / `SUCCEEDED → BLOCKED`, both illegal →
    `OrchestrationError` escaped `Conductor.run`. New `WorkItem.reopen(reason,
    *, to=QUEUED|BLOCKED)` is the only legal way back from SUCCEEDED and records
    the reason in `history`. Verified: 2 retries → blocked → audit trail intact.
22. New `WorkItem.attempts_left` (state-independent). `_verify` uses it, because
    the item it judges is SUCCEEDED — the very state that makes `can_retry` false.
    Without this, every red gate was terminal on the first attempt.
23. **`Conductor.run()` accepted an unexecutable graph** and returned a bare
    `failed` with no blockers. It now validates up front, dispatches nothing, and
    reports `"<id>: depends on unknown item 'ghost'"` + `"not executable"` summary.
24. **`_fit_budget` lied about the shape** (P0, found via the new CLI). When
    trimming could not fit `max_workers`, it relabelled the graph `solo` while
    keeping `cap` (e.g. 4) workers. Now: `cap == 1` → genuinely one worker that
    **inherits the absorbed workers' acceptance criteria** (and says
    `(collapsed: also covers …)` in its task); `cap > 1` → keep the best `cap`
    workers, keep the honest pattern label, and append
    `(trimmed to N workers…)` to the rationale.
25. **Trimming dropped the wrong workers.** Sorting by `priority` kept planner (10)
    and inspector (9) and dropped the coder (7) — a plan that produces nothing.
    New module-level `_KEEP_RANK` / `_keep_rank(item)`: required first, then
    coder → refactorer → ux-finisher → verifier → documenter → browser-validator →
    releaser → tooling → planner → inspector. Verified: cap 2 → coder+ux-finisher,
    cap 3 → +verifier, cap 4 → +planner.

### orchestration/executor.py (new — the production executor)
29. **`AgentExecutor`**: a worker *is* a bounded `run_task` call. The role drives
    `read_only` (`not role.mutates`), `max_steps` (the tighter of role and item),
    `gates` (`role.verify_gates` + `extra_gates`), and metadata (work id, role,
    reasoning mode, owned paths, denied tools, attempt). `Worker.build_prompt` is
    reused so the model gets the same mission/boundaries/acceptance text.
30. **Honest artefacts** — the core design rule. Each declared output resolves
    from *evidence first*: diff/changed files for `patch`, `changelog`, `tokens`,
    `version`; gate reports for `tests`, `behaviour_proof`, `gate_report`,
    `evidence`, `baseline`; the browser gate + its artifacts for `screenshots`,
    `console_report`, `network_report`, `journey_result`. Only `_PROSE_OUTPUTS`
    (`plan`, `blueprint`, `risks`, `repo_map`, `readme`, `notes`, …) may come from
    the model's own `<deliverables>` block, because that content exists nowhere
    else. **A claim never substitutes for evidence**: `patch: implemented the cart`
    over an empty diff leaves the output missing, and `Worker.run`'s
    required-outputs check then fails the worker. Verified against a live
    `--provider offline` run: blocker = "Code generator: worker did not produce
    required outputs: patch, tests, notes", exit 3.
31. `deliverables_instruction(outputs)` / `parse_deliverables(text)` are exported —
    the block protocol is `<deliverables>\nname: value\n</deliverables>`;
    `none`/`n/a`/empty are dropped.
32. A runner that raises is contained (`ok=False`, `"<ExcType>: <msg>"`, summary
    "…could not run"); a broken `on_run` observer is swallowed; `executor.runs`
    records `{work_id, role, run_id}` so a worker maps back to a run dir.

### orchestration/ budget semantics (fixed)
33. **`Budget` charged retries as new workers and cancelled good runs** (P0, found
    through the CLI). `consume()` incremented `workers_used` per *result*, so a
    retried worker was charged twice, and `exhausted()` used `>=`, so a plan
    trimmed to exactly `max_workers` reported exhaustion as soon as it finished
    dispatching. Now: `work_ids: set[str]` counts **distinct** workers, exhaustion
    is `workers_used > max_workers`, and steps/tokens still accumulate per attempt
    (a retry genuinely spends more).
34. **`pressure()` excluded the worker dimension.** The router trims to
    `max_workers`, so a fully dispatched plan sits at 100% by construction and
    warned on *every* run. Pressure now means "about to run out of room to work":
    steps, tokens, wall clock only.

### observability/
35. **`RichConsoleHandler.handleError` printed a traceback into CLI output** —
    including into `--json`, corrupting anything piped to `jq`. The stdlib
    implementation writes "--- Logging error ---" + traceback to stderr; it is now
    overridden to drop the record silently. A log line that cannot be rendered is
    worth nothing next to the run's own error reporting.

### core/runner.py — the role boundary is now enforced
39. **`RunRequest.tools: list[str] | None`** — an explicit tool allow-list.
    `None` = whatever the session grants; `[]` = **no tools at all** (an empty list
    must not silently become the loosest boundary); non-empty = exactly those names.
40. **`Runner._apply_tool_allow_list()`** runs in `setup()` *after* the registry is
    built and plugins are loaded, and **withholds** (not merely hides) every other
    tool with the reason `"not granted to the <role> role"` — so
    `tools/executor.py` surfaces the reason when a withheld tool is called and the
    worker learns the boundary exists. Names that are not registered (browser tools
    without Playwright, plugin tools that failed to load) are ignored: an
    allow-list narrows what exists, it never conjures tools.
41. **`AgentExecutor.build_request` passes `tools=list(role.tools)`**, which closes
    the gap noted in §3 P0.1: a `documenter` worker can no longer call
    `git_commit`, and a plugin cannot widen a role's grant by registering a tool.

    Verified boundary invariants (all under test):
    * `read_only=True` withholds **every** non-read tool including `shell_run`
      (`tools/factory.py`), so `verifier` / `browser-validator` / `tooling` — which
      all *grant* `shell_run` while being non-mutating — cannot mutate. Their grant
      list is aspirational and that costs nothing.
    * The allow-list can never widen the session grant: with `read_only=True` and
      `tools=["write_file","shell_run","read_file"]` the registry ends up as
      `["read_file"]`.
    * Registry sizes in the test env: 30 tools normally, 14 read-only. Browser
      tools **are** registered under `load_settings(cwd=project)` (the earlier note
      "Playwright not installed" only holds for a bare `Settings()`).

### config wiring — skills, appearance, reasoning (third session)
42. **Skills now reach the prompt and the gates.** Three seams, in order:
    `prompts/system.py::build_system_prompt(..., skills="")` renders an
    "ACTIVE SKILLS" block when non-empty → `context/builder.py::ContextBuilder`
    takes `skills=` (a resolved `SkillSet`) and gained `skill_section(task)`, which
    calls `skills.prompt_section(task=task)` inside a fail-soft `try/except` (a
    broken skill file must never stop a run) → `core/runner.py::Runner.setup()`
    calls `_resolve_skills(root)`, which resolves once per run, hands the set to the
    builder and **enables each skill's gate**
    (`settings.validation.gates[name].enabled = True`) *before* `ValidationEngine`
    is constructed. That ordering is the invariant: a skill may only make a run
    **stricter**, never laxer, and it cannot retroactively disable a gate that was
    already built.
43. **Bug: `resolve_skills` was parsing `pyproject.toml` as a skill file.**
    `skills/loader.py::_skills_dir` walked up looking for a skills directory and
    treated any directory it found as one, so a project root produced a bogus
    "could not parse skill" warning on every run. Fixed with `_ROOT_MARKERS`
    (`pyproject.toml`, `setup.py`, `package.json`, `Cargo.toml`, `go.mod`,
    `pom.xml`, `.git`, `.unknown`): **a directory containing a project marker is
    never a skills directory**; instead the loader points at the
    `.unknown/skills` convention path, which may simply not exist — no skills, no
    warning.
44. **`ui/theme.py::apply_appearance` was reading a field that does not exist**
    (`appearance.color`), so `color_depth = "none"` was silently ignored and a user
    who asked for no colour still got colour. Rewritten to honour
    `theme` / `mode` / `color_depth` / `glyphs`, plus
    `force_glyphs` / `force_depth` parameters so CLI flags (`--ascii`,
    `--no-color`) beat the config file. New `resolve_theme_key(choice, mode=)`:
    `"auto"` resolves through `detect_preferred_mode()` unless `mode` pins
    dark/light; named palettes pass through; an unknown key raises `KeyError` so a
    typo says so. `depth == "none"` also forces ascii glyphs — no colour means the
    ascii set is the one that still reads correctly in a log file.
45. **`cli/runtime.py::Runtime._apply_appearance(settings)`** is the call site, and
    it lives in `load()` (not in the Typer callback) because the callback runs
    before any config is read. It applies the palette, then re-configures
    `output` with motion/timings/width derived from config **and** flags. Three
    bugs found while verifying it with a probe (all now fixed and under test):
    * `--ascii` never reached `output.unicode` — it only changed the theme's glyph
      set, so box drawing stayed on.
    * `max_width` was passed straight to Rich as `width=`, which **forces** the
      console to 120 columns; on an 80-column terminal that emits lines the
      terminal then re-wraps. It is a cap: `width=min(target_width(), cap)`.
    * `unicode_ok=output.unicode and not ascii_requested` read global state, so one
      `--ascii` invocation stuck for the rest of the process and a second `Runtime`
      could never get its box drawing back. Derived from flag+config only now.
    `Runtime` gained `force_color` / `ascii_only` / `animations` fields (`None` =
    "the config decides") and a `palette` attribute for `ua config show`.
    A bad theme name raises `ConfigError` listing `PALETTE_ORDER` — though for a
    config *file* pydantic's `ThemeChoice` literal rejects it first, so that branch
    only fires for duck-typed settings from an embedder.
46. **`[reasoning]` now becomes wire parameters at every request.** The single seam
    is `reasoning.params_for_settings(settings, surface, *, escalate_by=0,
    max_output_tokens=, temperature=) -> (params, resolved)`, duck-typed on
    `settings` to avoid a config import cycle. It reads
    `ReasoningConfig.mode_for_surface(surface)` (new; backed by the
    `SURFACE_MODES` class-var map, unknown surface → `default_mode` rather than an
    error, because a new call site must not break a run), escalates when
    `auto_escalate` and `escalate_by > 0`, then calls `resolve()` and returns
    `wire_params()`. Verified translation, one product rung → three dialects:
    `anthropic` → `{'thinking': {'type': 'enabled', 'budget_tokens': N}}`,
    `openai-compatible` → `{'reasoning_effort': 'high'}`, `ollama` →
    `{'reasoning_tokens': N}`; a model with no native control →
    `{'temperature': 0.2}` with `resolved.degraded == True` (the honest path).
    Call sites wired: `core/loop.py::_model_turn` (surface from
    `AgentLoop._surface(state)` = the run's mode/kind, falling back to `"run"`),
    `core/loop.py` self-heal analysis (`"heal"`), `core/planner.py::_reasoning`
    for both `plan()` and `revise()` (`"plan"` — planning must not inherit the
    cheap coding rung), `core/runner.py::_reasoning_params` for memory extraction
    (`"summary"`). `AgentLoop.last_reasoning` keeps the resolution so the UI can
    report which rung was actually used and whether it degraded.
    **`planning/deep.py` was deliberately left alone** — `DeepPlanner` already
    takes `reasoning=` and resolves its own.
47. **Config errors now quote the value the user wrote.**
    `config/loader.py::_offending_value()` appends `(got 'not-a-palette')` to each
    pydantic problem. Pydantic's own message lists what is *allowed* but never what
    was supplied, and the supplied half is what locates the typo. Scalars only —
    echoing a whole rejected section inside a one-line error is noise.
48. **`tests/test_config_wiring.py` (24 tests)** pins all of the above: per-surface
    rungs, per-vendor translation, degradation, "resolution never raises",
    escalation on/off, the loop/planner/runner seams, theme/glyphs/motion/timings/
    width, flags-beat-config, and non-stickiness between invocations. Its
    `restore_ui` autouse fixture snapshots and restores the palette, glyph set and
    `output` flags — **appearance is process-global state, so any test that touches
    it needs that fixture or it will leak into the next test.**
    `tests/test_agent.py` went 22 → 26 (skill prompt appears, skill gates enabled,
    disabled skills produce a byte-identical prompt, `pyproject.toml` is not
    scanned). `tests/test_cli_orchestration.py` now extracts JSON from the first
    `{` because `CliRunner` merges stdout and stderr, so log warnings corrupted
    `--json` parsing (the real shell is clean: JSON on stdout, warnings on stderr).

### conductor-in-runner — `ua run` delegates (third session)
49. **`RunRequest.orchestrate: bool | None`** is the switch: `None` = decide from
    config + the task, `True` = delegate even a trivial task, `False` = stay solo.
    Explicit beats inferred, because a user who typed a flag has more context than
    a heuristic. Exposed as `--orchestrate/--solo` on `ua run`, `ua build` and
    `ua fix` (threaded through `cli/commands/agent.py::_execute`, so all three stay
    in step). `ua plan --run` and `ua ask` inherit the default decision; `ask` /
    `plan` kinds are never delegable.
50. **`Runner._orchestration_decision() -> (bool, str)`** — the rule, in the order
    it is applied (order is the whole design): already inside a delegation →
    `--solo` → `[orchestration] enabled` → `mode = "solo"` → kind is delegable
    (`run`/`build`/`fix` only) → not a resumed plan → `--orchestrate` forces →
    `mode = "orchestrated"` forces → complexity floor → **router shape**.
    The reason string is emitted in the `orchestration.started` event and lands in
    the journal, so "why did this use six workers?" is answerable without reading
    code. Both `analyze_task` and `Router.pattern_for` are wrapped fail-soft: a
    scoring or routing bug keeps the run solo rather than killing it.
51. **Complexity alone is not sufficient — the shape decides.** The last gate asks
    `Router(config=config).pattern_for(analysis)`: if the router would hand the
    whole task to one worker (`"solo"`), delegating buys nothing and costs a
    conductor, a graph, a merge and a second audit trail, so the loop stays solo.
    `pattern_for` is deterministic and does no I/O, which is what makes it cheap
    enough to ask up front. Measured scores: `"summarise the repository"` → 1/solo,
    `"run the test suite and report"` → 4/pipeline, `"fix the bug in the login
    form"` → 4/escalate, `"refactor the auth module and add tests and update the
    docs"` → 4/pipeline, `"build a web app with auth, a dashboard, tests and docs"`
    → 9/graph.
52. **BUG (serious): workers re-delegated, recursively.** Each worker is a real
    `Runner`, so its own decision logic saw the same complex task and started a
    *second* conductor — conductor → worker → conductor → … The symptom was
    `OSError: [Errno 24] Too many open files` after ~36 nested worker runs (4
    workers × 3 attempts × 3 plans, the configured worst case), a 10-minute test
    suite, and blocker strings that nested themselves
    (`"Repo inspector: Code generator: Code generator: Code generator: …"`).
    Fixed in two layers, because one layer alone is a coincidence:
    * `AgentExecutor.build_request` now sets **`orchestrate=False`** — a worker is a
      *leaf* of the decomposition; the conductor already chose the shape.
    * `core/runner.py` holds a module-level `ContextVar` **`_DELEGATION_DEPTH`**
      for the duration of `_run_orchestrated()` (set/reset in a `finally`), and
      `_orchestration_decision()` refuses at depth > 0. That covers embedders who
      build a `Runner` by hand inside a worker.
    After the fix: fds stay flat at 7 across repeated runs and the same delegated
    run drops from ~13s to ~0.8s. **If a future change makes runs slow and
    fd-hungry, suspect nesting first.**
53. **`Runner._absorb_orchestration(outcome)`** maps an `OrchestrationOutcome` onto
    `RunState`, with the honesty rules that are easy to get wrong:
    * **`ok` decides the status, not the label.** `partial` → BLOCKED, because
      mapping it to COMPLETED would make `RunResult.ok` true for a run with missing
      deliverables (`ok` is `status is COMPLETED and verification in (None, passed)`,
      and a delegated run has `verification = None`).
    * `cancelled` → FAILED: the budget *stopped* work, which is a failed run even
      though nothing raised. (`RunStatus` has no CANCELLED member.)
    * `outcome.unverified` → a blocker naming the workers. A run that checked
      nothing must not look like a run that passed.
    * `merge.conflicts` → blockers; **`files_changed` comes from `merge.files`**,
      deduped in merge order — never from what workers claimed.
    * An empty `summary` is synthesised as `"Orchestration <status>: <n>/<m> workers
      succeeded."` so a report always says something true.
    `state.mode = "orchestrated"`, `state.step` = worker count, and the outcome dict
    is written to `<run_dir>/orchestration.json` next to `run.json`. Conductor
    events are re-emitted as `orchestration.<kind>` into the run journal.
54. **`forward()` honours async hooks.** `LoopHooks.on_event` is `ASYNC_HOOK`
    (`Callable[[Event], Awaitable[None] | None]`), and the conductor calls
    `on_event` from synchronous points, so a coroutine result must be scheduled with
    `asyncio.get_running_loop().create_task(...)` — exactly what `AgentLoop.emit`
    does. Calling it plainly would create an un-awaited coroutine and silently
    disable live progress for every async consumer (the TUI, `--ndjson`).
55. **`tests/test_runner_orchestration.py` (25 tests)** pins the decision matrix
    (every rung, both force directions, the floor raised, the depth guard), the leaf
    rule (`build_request(...).orchestrate is False` *and* the role grant still
    applies), the mapping (partial/cancelled/failed/unverified/conflicts/dedupe/
    synthesised summary/`finished_at`) and three end-to-end runs through `run_task`
    with the offline provider. Its `fast_orchestration()` helper pins
    `max_workers=2`, `max_retries_per_worker=0`, `max_replans=0` — **without it an
    end-to-end delegated test costs seconds instead of milliseconds.**
    One pre-existing test changed: `test_agent.py::
    test_failing_gates_block_success_and_produce_a_blocker` now passes
    `orchestrate=False`, because its subject is the *solo loop's* gate handling and
    its task (`"run the test suite and report"`, complexity 4/pipeline) would
    otherwise be delegated — where gate results live on the workers, not on
    `RunResult.verification`.

### cli/ (new this session)
36. **`src/unknown_agent/cli/commands/orchestration.py`** — the `ua orchestrate`
    group, registered in `cli/app.py` (Agent panel) and `cli/commands/__init__.py`.
    Commands: `plan`, `run`, `roles [id]`, `list`, `show [run]`,
    `ledger [run] [--kind] [--path]`, `worker <role> <task>`. All honour
    `--json` / machine mode; exit codes 0 / 2 (usage) / 3 (blocked) / 8 (budget).
    `run`/`worker` take `--executor agent|offline` (**default `agent`** — the CLI
    does real work unless told otherwise) plus `--model`/`--provider` passthrough.
    `plan` always uses the offline executor because it delegates nothing.
37. **`run` exited 8 (BUDGET) on a successful run.** The exit logic treated
    `budget.exhausted()` as failure, but spending the whole *worker* budget on a
    run that completed is the budget being used, not exceeded. Now only
    `status == "cancelled"` (work actually stopped) exits BUDGET.
38. **`tests/test_cli_orchestration.py` (31 tests)** covers the whole command
    group, including the three bugs that were only findable by running it: the
    "solo with four workers" label, the trim that dropped the coder, and the
    unbalanced Rich markup that raised at print time. Note Typer's convention:
    `ua orchestrate` with no subcommand prints usage and exits **2**.

## 3. What is NOT done — remaining work, in priority order

### P0 — wiring
1. ~~A real executor for orchestration.~~ **DONE and enforced.**
   `orchestration/executor.py` (`AgentExecutor`) is the default for
   `ua orchestrate run|worker`, and the role's grant is now a hard boundary via
   `RunRequest.tools` + `Runner._apply_tool_allow_list()` (see §2.39–41).
   What is still *advisory only*: `role.owned_paths`. The router serialises
   workers whose ownership overlaps, and `merge_results` fails on two roles
   claiming one file, but nothing stops a coder from writing outside
   `src/**` — that would need a path-scoped policy check (a `policy.owned_paths`
   filter in `tools/executor.py`). Worth doing, not urgent: ownership is a
   scheduling invariant first.
2. ~~`core/runner.py` does not use the Conductor.~~ **DONE** — see §2.49–2.53.
   `RunRequest.orchestrate: bool | None` is the switch (`None` = decide),
   `Runner._orchestration_decision()` explains itself, `Runner._run_orchestrated()`
   delegates and `Runner._absorb_orchestration()` maps the outcome back.
   What is still *not* done on this path:
   * **Worker runs each write their own run journal.** One six-worker graph
     produced 12 directories under `.unknown/runs/` (1 parent + 11 worker
     attempts). That is honest auditing — every worker really was a full
     `Runner` — but `ua runs list` will be noisy. A fix would be to give worker
     runs a nested path (`.unknown/runs/<parent>/workers/<id>/`) or a
     `metadata.orchestration.work_id`-aware listing.
   * `RunResult.verification` stays `None` for a delegated run: gate results live
     on the workers (`outcome.verification` dict + `outcome.unverified`), and
     `unverified` workers are surfaced as blockers instead. Building a real
     `VerificationReport` from the conductor's dict is the missing translation.
   * `state.plan` is not synthesised from the `WorkGraph`, so a delegated run has
     no `plan.json`. The graph *is* persisted in `orchestration.json`.
3. ~~Config wiring gaps.~~ **DONE for skills, appearance and reasoning** — see
   §2.42–2.47 for exactly where each one lands. What is still *not* wired:
   * `SkillSet.tools()` is not yet intersected into the session registry (a skill
     can add prompt text and gates, but not tools). Deliberate: granting tools from
     a file on disk needs the same review path as plugins.
   * `SkillSet.untrusted()` is not surfaced in `ua doctor`.
   * `AppearanceConfig.density`, `.banner`, `.compact_banner`, `.accent_emoji`,
     `.progress_rail`, `.show_token_usage`, `.show_cost` are read by the renderers
     that predate this wiring but are not re-applied by `Runtime._apply_appearance`
     (theme/glyphs/colour/motion/timings/max_width are).
   * `[streaming]` (`StreamingConfig`) still has no consumer on the non-TUI path:
     `output.animations` is honoured, but `token_pace`, `min_chunk_chars`,
     `flush_interval_ms`, `live_thinking` and `live_tools` are not. Providers
     already accept `stream=` — the gap is a streaming renderer for the CLI.
4. **A `ua skills` command group.** `skills.py` does not exist under
   `cli/commands/` (skills are only reachable through config). Needs:
   `list`, `packs`, `show <id>`, `new [path]` (writes `SKILL_FILE_TEMPLATE`),
   `check` (reports `SkillSet.unresolved` + `untrusted()`).

### P1 — tests
5. `tests/test_tui.py` — headless Textual tests. The old probes lived in
   `/tmp/ttest/` (`full.py` 4-scenario wizard, `shell.py` AgentShell with a fake
   runner — both passed) and **are not persisted**; `/tmp` is outside the
   workspace. Re-create with `App.run_test()` + `pilot`; mind §7.
6. `tests/test_cli_wiring.py` — first-run routing (`detect_setup_state()` →
   onboarding / main / repair), `ua config show|get|set`, `ua providers`,
   `ua models`, `ua plan`, `--help` for every group.
7. Check whether `tests/test_reasoning.py` / `test_workflows.py` coverage exists
   before writing them (`reasoning.resolve` vendor map; 9 workflow specs,
   `detect()`, `generate()`, `run_workflow()`).

### P2 — packaging and docs (deliverables 19–21)
8. `install.sh` (curl-able): OS detection, prefer `pipx`, fall back to
   `python -m venv`, opt-in Playwright chromium, print the first-run command,
   idempotent.
9. `.github/workflows/release.yml` (matrix ubuntu/macos/windows, lint+type+tests,
    sdist/wheel, publish on tag) and `ci.yml` for PRs.
10. `README.md` — premium, not generic: what it is, 60-second start, onboarding
    visuals, provider table, skills, orchestration shapes, reasoning modes,
    config reference.
11. `docs/`: `architecture.md` (module map + build order = deliverables 20/21),
    `onboarding.md`, `providers.md`, `skills.md`, `orchestration.md`,
    `reasoning.md`, `configuration.md`, `extending.md`.
12. `pyproject.toml` audit: entry point (`ua = …`, verify exact target), optional
    groups (`browser`, `dev`, `all`), Python floor, license, classifiers.
13. Repair flow polish: `detect_setup_state()` → READY / NEEDS_SETUP /
    NEEDS_REPAIR; the repair path must re-enter the TUI at the *broken step*, not
    step 1.
14. Lint/type gate: `ruff` + `mypy` config exist? Verify and run — unused imports
    were left behind once already this session.

## 4. CLI smoke results (hand-verified, `PYTHONPATH=src`, in an empty dir)

```
ua orchestrate --help            → 7 commands listed
ua orchestrate plan "<checkout>" → summary panel (shape/why/complexity/underspec/
                                   workers/surfaces), dependency Tree with glyphs,
                                   work-graph table, trim warnings, analysis reasons
  --max-workers 1 → shape solo, 1 worker, "(collapsed: also covers …)"
  --max-workers 2 → graph, 2 workers (Code generator, UI/UX finisher)
  --max-workers 4 → graph, 4 workers (+ Planner, Verifier)
ua orchestrate run "<checkout>" --max-workers 6
  → per-worker start/pass lines, summary panel (run id, shape, 6/6 succeeded,
    retries, escalations, duration, ledger path), merge panel, the
    "N workers' gates were not verified" warning, exit 0
  → ledger file written under .unknown/orchestration/<run>.jsonl
ua orchestrate run "<task>" --json → full outcome.to_dict()
ua orchestrate list               → table of runs (run id, bytes, file)
ua orchestrate show               → timeline() replay
ua orchestrate ledger --kind run_finished → one JSON line
ua orchestrate roles              → specialists table (stage/writes/tools/denied/
                                    verified by/kind)
ua orchestrate roles coder        → full panel + outputs/tools/denied/owns/gates
ua orchestrate worker inspector "map this repository"
  → mission, granted tools, denied tools, prompt chars, result, ok
```

## 5. Orchestration API (probed and verified — write tests against this)

```python
# roles.py
ROLES = ['planner','inspector','coder','refactorer','ux-finisher',
         'browser-validator','verifier','documenter','releaser','tooling']
resolve_role_id('plan')->'planner'; ('codegen')->'coder'; ('repo')->'inspector';
  ('PLANNER')->'planner'; ('The Planner')->None; ('nope')->None
roles_for_pattern('solo')     -> ['coder']
roles_for_pattern('pipeline') -> ['inspector','planner','coder','verifier']
roles_for_pattern('fanout')   -> ['inspector','planner','coder','ux-finisher','documenter','verifier']
roles_for_pattern('graph')    -> ['planner','inspector','coder','ux-finisher','browser-validator','documenter','verifier']
roles_for_pattern('escalate') -> ['planner','inspector','coder','verifier','refactorer']
roles_payload()  # stage-ordered; keys: id,label,mission,outputs,tools,deny_tools,
                 # mutates,reasoning_mode,stage,owned_paths,verify_gates,required,
                 # can_delegate,guidance,max_steps
mutates: coder, refactorer, ux-finisher, documenter, releaser (True); rest False
stages: planner 0; inspector 1; tooling 1; coder 2; refactorer 3; ux-finisher 4;
        browser-validator 5; verifier 5; documenter 6; releaser 7
verify_gates: planner/inspector/tooling are EMPTY (legitimate — they produce
  understanding, not diffs, and none of them mutates). coder ('syntax','tests');
  refactorer ('tests','lint','diff'); ux-finisher ('build','browser');
  browser-validator ('browser',); verifier ('syntax','lint','typecheck','tests',
  'build','diff'); documenter ('syntax',); releaser ('tests','build')
Role also has .glyph and .style (used by the CLI/TUI)

# worker.py
WorkState: PENDING QUEUED RUNNING SUCCEEDED FAILED BLOCKED CANCELLED SKIPPED
LEGAL_TRANSITIONS: PENDING->{CANCELLED,QUEUED,BLOCKED,SKIPPED};
  QUEUED->{CANCELLED,PENDING,RUNNING,BLOCKED,SKIPPED};
  RUNNING->{CANCELLED,BLOCKED,FAILED,SUCCEEDED}; FAILED->{CANCELLED,QUEUED,BLOCKED};
  BLOCKED->{CANCELLED}; SUCCEEDED/CANCELLED/SKIPPED terminal
WorkItem.transition(state, *, note='')  # raises OrchestrationError if illegal;
                                        # appends {'at','from','to','note','attempt'}
WorkItem.reopen(reason, *, to=WorkState.QUEUED|BLOCKED)  # only from SUCCEEDED
WorkItem.can_retry      # False in terminal states
WorkItem.attempts_left  # max_attempts - attempts, state-independent
Worker(role_or_id, *, executor=None, context=None, on_event=None)
Worker.tool_subset(available: Iterable[str]) -> list[str]   # intersection, ordered
Worker.build_prompt(item) -> str
await Worker.run(item) -> WorkResult    # queues itself if the item is PENDING
WorkerContext: project_root, task, blueprint_text, repo_map, conventions,
  skill_prompt, policy_summary, previous_results, settings
OfflineExecutor(*, fail_roles=(), fail_once=())
CallableExecutor(func(item, role, context))   # WorkResult | Mapping | any
WorkResult: item_id, role, ok, summary, artifacts, files_touched, evidence,
  steps_used, tokens_used, duration_s, error, data, follow_ups
# no executor -> ok=False, error='no executor wired (dry run)'
# required role missing declared outputs -> ok=False,
#   error='worker did not produce required outputs: patch, tests, notes'

# conductor.py
analyze_task(task, *, blueprint=None, repo_known=False) -> TaskAnalysis
TaskAnalysis.to_dict(): task, complexity(0-10), surfaces, has_ui/api/data/docs/
  release/security, is_bug/greenfield/refactor/question, has_action,
  underspecification, preset, milestones, reasons
Router(*, config=None).pattern_for(analysis, *, blueprint=None) -> (pattern, why)
Router(*, config=None).build(task, *, analysis=None, blueprint=None,
                             milestones=(), repo_known=False) -> WorkGraph
WorkGraph: items, pattern, rationale, analysis, warnings, created_at
  .by_id .find(role=,state=) .ready() .blocked_by_failure() .validate()
  .summary() .to_dict()      # NOTE: to_dict()['items'] is a COUNT, not a list
DEFAULT_MILESTONES -> (m0 Foundation, m1 Core behaviour)
conflicting_pairs(items, *, respect_dependencies=True) -> [(a, b, 'glob ∩ glob')]
merge_results(results, *, strategy='owner_partition'|'sequential_apply'|'union'|'best_of')
  -> MergeOutcome(strategy, artifacts, files, conflicts, notes, ok)
  # conflicts key on ROLE: two workers of the same role claiming a path is not a
  # conflict; different roles claiming one is, and owner_partition sets ok=False.
  # Duplicate artifact keys are kept under namespaced '<role>:<key>' with a note.
Budget(max_workers=8, max_steps=400, max_tokens=900_000, max_wall_clock_s=2700.0,
       workers_used, steps_used, tokens_used, started_at, warnings)
  .wall_clock_s .exhausted() -> str|None .pressure() .consume(result) .to_dict()
_KEEP_RANK / _keep_rank(item)  # module level, trim preference
Conductor(*, config=None, executor=None, context=None, ledger=None, verifier=None,
          on_event=None, router=None, run_id='')  # auto id orc-YYYYmmdd-HHMMSS-xxxx
await Conductor.run(task, *, graph=None, blueprint=None, milestones=(), repo_known=False)
OrchestrationOutcome: run_id, pattern, graph, results, merge, verification,
  unverified, budget, ok, status, summary, blockers, escalations, retries,
  duration_s, started_at, finished_at | .succeeded() .failed() .to_dict()
  status: pending|completed|partial|blocked|failed|cancelled|dry-run
await orchestrate(task, *, config=None, executor=None, blueprint=None, milestones=(),
                  project_root='.', ledger_path=None, verifier=None, on_event=None,
                  run_id='', dry_run=False) -> OrchestrationOutcome   # ASYNC

# ledger.py
ledger_path_for(root, run_id='') -> <root>/.unknown/orchestration/<run>.jsonl
Ledger(path=None, *, run_id='', enabled=True)  # None/disabled = memory only;
  # an unwritable path degrades to memory-only and logs (never fails a run)
  .record(kind, *, work_id='', role='', **data)  # drops None values from data
  .events .by_kind() .counts() .for_work() .summary() .read() .close()
LedgerEvent(kind, at, work_id, role, run_id, data) .to_dict() .line() .from_dict()
read_ledger(path) -> [LedgerEvent]
list_ledgers(root, *, limit=20) -> [{'path','run_id','size','modified'}]
timeline(events) -> ['HH:MM:SS glyph kind role note', …]
EVENT_KINDS (24): run_started plan_selected work_created work_queued worker_started
  worker_finished work_failed work_retried work_blocked work_skipped work_cancelled
  dependency_failed conflict_detected merge_started merge_finished merge_conflict
  verify_started verify_finished escalation replan budget_warning budget_exhausted
  run_finished note     # unregistered kinds accepted, flagged in data
```

### Behaviour verified by hand (safe to assert)

```
analyze_task('what does this repo do?')   complexity 1, is_question, underspec 0.93
  -> ('solo', 'this is a question: one read-only worker answers it')
  -> the built graph has ONE worker and it does NOT mutate (inspector, not coder)
analyze_task('fix the failing test in the cart module')  is_bug -> 'escalate'
analyze_task(REFLOW)  is_refactor -> ('pipeline', 'refactoring is strictly sequential…')
analyze_task(CHECKOUT) is_greenfield -> ('graph', 'greenfield work with 2 deliverable
  groups needs parallel, dependency-ordered workers')
CHECKOUT = "build a checkout flow for a store: product page, cart, stripe payments,
  responsive mobile design, deploy to fly.io, write docs and release notes"
REFLOW   = "refactor the auth module to use dependency injection, add tests, then
  update the docs"
config: pattern='solo' forces solo; enabled=False -> solo 'disabled in config';
  min_complexity=10 on a complexity-9 task -> solo 'below the orchestration
  threshold' (rule is `complexity < minimum`)
dry run: status 'dry-run', ok True, blockers [], merge None, NO ledger file
live run (OfflineExecutor, CHECKOUT, default config): status 'completed', ok True,
  8 items planner->inspector->coder(m0)->coder(m1)->ux-finisher->browser-validator
  ->verifier->releaser; graph.warnings includes
  "dropped documenter '<id>' to fit max_workers=8: optional role";
  merge owner_partition ok, files ['src/**','app/**','lib/**','**/*.css','**/*.scss'];
  verification has 6 entries {'ok': None,'checked': False,…}; unverified == 6 ids;
  ledger 51 events (run_started 1, plan_selected 1, work_created 8, work_queued 8,
  worker_started 8, worker_finished 8, note 8, verify_finished 6, budget_warning 1,
  merge_finished 1, run_finished 1)
red gate (verifier returns {'ok': False, 'detail': 'tests are red'}):
  status 'blocked', ok False, retries 2, blockers ['Code generator left a gate red:
  tests are red'], ledger has work_retried x2 + work_blocked, item.history contains
  'reopened: gate red: tests are red'
fail_roles=['coder'] -> not ok, blockers, BLOCKED items, dependency_failed events
fail_once=['coder']  -> retries >= 1 and the run still succeeds
broken graph (dangling dep) -> status 'failed', ok False, results == {} (nothing
  dispatched), blockers name the missing id, summary says 'not executable'
milestones=[m0,m1,m2] -> parallel_group {'milestone:m0','milestone:m1','milestone:m2'}
```

### Orchestration behaviour still untested (write these next, expect bugs)
* budget exhaustion **mid-run** → remaining items CANCELLED, `status=='cancelled'`,
  `budget_exhausted` event (only the routing-time trim is covered today).
* `_escalate` / replan path: `max_replans`, the `escalation` and `replan` events,
  and the revision worker's transitions.
* `concurrency` actually bounding simultaneous dispatch (the observer test only
  asserts events were seen).
* `merge_strategy` config plumbing (`sequential_apply`).
* `Ledger.summary()` / `.counts()` / `.by_kind()` / `.for_work()`.
* `WorkGraph.summary()`.

## 6. Headless TUI probes (were in /tmp — recreate if needed)

`/tmp/ttest/full.py` drove the whole 12-step onboarding wizard through 4
scenarios with `App.run_test()`; `/tmp/ttest/shell.py` drove `AgentShell` with a
fake runner. Both passed. Entry points:

```python
AgentShell(settings=…, runner=…, first_task=…)     # cli/shell.py
ShellBridge, StatusSnapshot, ComposerMessage, run_shell(...)
# verified onboarding scenarios:
#   ollama        -> no secret prompt
#   groq + key    -> api_key == 'store:groq', secret file mode 0600
#   custom        -> [providers.internal-llm] type = 'openai-compatible'
#   groq + env    -> api_key == 'env:GROQ_API_KEY'
# config writes are delta-only (prune_defaults)
```

## 7. Traps already paid for (do not rediscover)

**Python / tooling**
* f-strings cannot nest the same quote char on 3.13 — build the string outside.
* String surgery: always `assert s.count(old) == 1` before `replace`. A
  twice-occurring marker once duplicated ~500 lines; and inserting module-level
  code before an indented `def` silently **ends the enclosing class** (this broke
  `Router._serialise_conflicts` — the CLI caught it, the tests did not).
* Same trap, second variant (paid for twice now): inserting a module-level `def`
  **between a decorator and the class it decorates** re-attaches the decorator to
  the new function. `@dataclass(slots=True)` above `class PlannerResult` ended up
  decorating `_reasoning`, and the import died with
  `AttributeError: 'function' object has no attribute '__mro__'` — an error that
  names dataclasses internals, not your edit. When anchoring an insert on a class,
  anchor **above its decorators**, and grep the decorator back afterwards.
* **Rich markup is parsed in Typer `help=` strings.** `help="…decide from
  [orchestration] and task complexity"` renders as "decide from  and task
  complexity" — the bracket is eaten as a style tag. Escape it: `\[orchestration]`.
  Same family as the stray-closing-tag crash in §7's Rich note, but silent instead
  of fatal, so it survives review.
* **A test suite that goes from 10s to 620s is telling you something structural**,
  not something flaky. Here it was nested delegation (§2.52): the same task
  re-decided at every level. `OSError: [Errno 24] Too many open files` with the soft
  limit at 1024, and blocker strings that nest themselves
  (`"A: B: B: B: …"`), are the same fingerprint.
* **Measure fds when a path spawns nested resources**:
  `len(os.listdir("/proc/self/fd"))` before/after, with `logging.disable(...)` so the
  noise does not hide the counts. Solo runs were flat at 7; the leak was only in the
  delegated path, which is what localised it in one step.
* Pydantic models validate on assignment (`validate_assignment`), so
  `settings.appearance.theme = "typo"` raises `ValidationError` at the setattr —
  you cannot build an invalid Settings in-process to test a downstream guard. Test
  that guard with a `SimpleNamespace` stub (which is also the duck-typed path
  embedders take) and test the typed path through a config **file**.

**Textual 8.2.8**
* Widget `DEFAULT_CSS` is parsed *before* `register_theme()` — `$ua-*` only in
  app-level CSS.
* `@on(Message, "#selector")` needs a `control` property; use `self._sender`.
* `set_class(add, *names)` — toggle FIRST. Rich markup in widgets: plain styles.
* `Static()` takes a str/renderable, never a Widget.
* Children passed to `Vertical.compose()` in `__init__` become `_pending_children`.
* Unique widget ids. Mount is async → `call_after_refresh`; tolerant accessors.
* `TabbedContent(initial=…)` / `.active` take the **TabPane id** (`"tab-plan"`).
* `Input` ignores `pilot.press("enter")` unless focused.

**Rich**
* `Table.add_column(style=…)` raises `MissingStyle` during measurement — style the
  `Text` cells.
* Markup must balance: a stray `[/ua.warn]` raised at print time inside the CLI.
* `ua.*` styles exist: `ua.ok ua.err ua.warn ua.dim ua.accent ua.obs ua.panel.border`.

**This codebase**
* `Settings` root: `provider: str = "default"` + `providers` dict — **no**
  `default_provider`. `extra="forbid"`.
* `AppearanceConfig`: theme/mode/glyphs/color_depth/density/motion/banner/
  compact_banner/accent_emoji. `ReasoningMode`: default|medium|high|super|max|ultra.
* `ProviderType`: openai-compatible | anthropic | ollama | offline.
* `BrowserConfig.channel` field is named **`browser`**.
* `[skills].pack` is `list[str]` — wrap `[value]` on write, `[0]` on read.
* Custom endpoint profile: `type = "openai-compatible"` + sanitized profile name as
  the dict key, **not** `type = "custom:<name>"`.
* `Palette` has `tagline`/`inspired_by`, **not** `description`;
  `get_palette('auto')` raises KeyError → normalize to `get_palette(None)`.
* `resolve_entry()` returns `None` for unknown keys.
* `prune_defaults`: the list branch must return `data` unchanged when equal.
* `OrchestrationConfig`: enabled, mode('auto'), min_complexity=3, **max_workers=4**,
  concurrency=2, pattern=None, merge_strategy='owner_partition',
  verify_each_worker=True, max_replans=2, max_retries_per_worker, accept_partial,
  worker_max_steps=24, worker_token_budget=180_000, max_wall_clock_s, ledger=True.
  Note `max_workers` defaults to **4** while `Budget.max_workers` defaults to 8.
* `analyze_task(task, *, blueprint=None, repo_known=False)` — **no** settings kwarg.
* `infer_answers(task, *, signals=, threshold=3) -> dict[str, Answer]`.
* `SignalSet`: text/tokens/phrases/hits/qualities/tech/magnitudes/preset;
  `.strength(axis, option_id)`, `.top(axis)`, `.to_dict()`. **Not**
  `.technologies/.surfaces/.explicit`.
* `QuestionPlan`: ask/auto (list[PlannedQuestion]), skipped/inferred,
  declined: list[tuple[str,str]], preset, max_questions, scope, change_kind,
  underspecification. `PlannedQuestion`: axis/recommendation/options/priority/
  already_answered/existing + `.question`.
* `Approach`: id/label/summary/stack/steps/tradeoffs/weights/**total**/risks/
  effort_days/rationale/recommended/source — **no** `.name`/`.score`.
* `ProjectPlan`: goal/blueprint/approaches/**chosen**/architecture/decisions/
  milestones/risks/**verification**/non_goals/estimate/warnings/reasoning_mode/
  created_at/source — **no** `.definition_of_done`/`.rationale`.
* `Answer(axis=, value=, label=, source=, …)` — **no** `option_id`.
  `HeadlessAskUI(overrides=…)` marks overrides `source="flag"`.
* `Skill`: id/label/summary/instructions/category/tools/gates/workflows/fact_kinds/
  triggers/default_enabled/in_default_pack/source/path/version/author/priority/
  settings/**overrides** — **no** `name`/`description`.
  `SkillSet.relevant(task, limit=6) -> list[tuple[Skill, int]]`.
  `parse_skill_file(path, *, origin='project') -> LoadedSkill(skill, origin, path,
  error, trusted)`; `error` is **`None`** when clean, not `""`.
* `run_task(loaded, request, hooks=LoopHooks, event_sink=) -> RunResult`
  (state/outcome/run_dir/verification/diff/exit_status; props run_id/status/ok/
  summary/blockers/files_changed). `RunRequest`: task/mode/kind/read_only/dry_run/
  yes/model/provider/max_steps/gates/browser_url/invoke_browser/resume_run_id.
* `reasoning.resolve(mode, surface=, model=, provider=, protocol=) ->
  ResolvedReasoning`. Vendor map: anthropic→budget, ollama→tokens, offline→none,
  deepseek/moonshot/zhipu→always_on, perplexity/cohere/cerebras/azure→none,
  else→effort. `MODE_ORDER` = default/medium/high/super/max/ultra.
* CLI conventions: `runtime: Runtime = ctx.obj`; `runtime.load()` →
  `loaded.settings`, `loaded.project_root`, `loaded.paths.cwd`;
  `runtime.is_machine`, `runtime.quiet`, `runtime.emit_json(payload)`,
  `runtime.start_logging()`; `output.print/blank/raw/rule/status/error_panel/
  success_panel`; renderers `banner header kv_table data_table status_label
  gate_table tool_card unified_diff file_diff plan_tree step_header thought
  observation markdown bullets summary_panel elapsed token_usage columns timestamp`;
  `run_async(coro)`; exit via `raise typer.Exit(ExitCode.X)`.
* `ExitCode`: OK 0, FAILURE 1, USAGE 2, BLOCKED 3, CONFIG 4, PROVIDER 5, POLICY 6,
  GATE 7, BUDGET 8, TOOL 9, INTERRUPTED 130.
* Some `DecisionOptions` legitimately have `effort=0`.
* The tie-break rationale "scored marginally higher (77.0 vs 75.0)" is intended.
* `from unknown_agent.reasoning import *` needs `__all__` (was missing).

## 8. Module map (deliverable 20)

```
src/unknown_agent/
  cli/        app.py (`ua` Typer app; _register_commands()) + commands/
              (agent, browser, config_cmd, context_cmd, doctor, inspect,
               memory_cmd, misc, orchestration ← NEW, plugins, providers_cmd,
               runs, sessions, tools, verify), runtime.py, shell.py
  config/     models.py (Settings + 17 sub-configs), loader.py
              (load_settings/dump_settings/default_config_path/prune_defaults/
               save_settings → 0600)
  core/       runner.py (run_task), loop.py (LoopHooks), models.py (GateStatus…)
  providers/  catalog (21 entries) + real adapters
  onboarding/ 12-step wizard logic
  tools/      35 builtin tools
  validation/ memory/ browser/ context/ extensions/ observability/ prompts/ host/
  ui/         console.py (output, OutputManager), renderers.py, theme_defs.py,
              errors.py
  reasoning/  MODE_ORDER + resolve()
  planning/   models/axes/clarify/deep/panels/implications/inference (21 axes)
  skills/     pack.py (14 builtins, 7 packs, SkillSet), loader.py (discovery,
              precedence, trust, SKILL_FILE_TEMPLATE, skills_dir)
  workflows/  9 specs, detect(), generate(), run_workflow()
  orchestration/ roles.py, worker.py, conductor.py, ledger.py
  tui/        themes.py, widgets.py, onboarding.py, app.py  (Textual 8)
```

Planning calibration (verified): "build me a website" → underspec 0.934/high,
6 questions; a FastAPI/SQLite/Fly.io request → 0.422/medium, infers
app_type/platform/data_strategy/tech/deployment, 3 questions; "add a healthcheck
endpoint" → 0.307/low/change, 0 questions; refactor/fix/remove → only scope_level
+ testing_bar.

## 9. Suggested order for the next agent

1. Run `python -m pytest -q` — confirm **417 passed** (~10s) before changing
   anything. If it suddenly takes minutes, read §2.52 first.
2. ~~P0.3 config wiring.~~ **DONE** (skills → prompt/gates, appearance → consoles,
   reasoning → every `ModelRequest`). See §2.42–2.48 for the seams and
   §3 P0.3 for the four sub-items intentionally left unwired (`SkillSet.tools()`,
   `ua doctor` untrusted report, the remaining `AppearanceConfig` fields,
   `[streaming]`).
3. ~~P0.2 conductor-in-runner.~~ **DONE** — `ua run`/`ua build`/`ua fix` delegate
   when the task warrants it, with `--orchestrate/--solo` as the override. See
   §2.49–2.55 for the decision order, the nesting bug and the mapping rules; see
   §3 P0.2 for the three sub-items left unwired (nested worker run journals,
   `RunResult.verification` for delegated runs, `plan.json` from the graph).
4. **P0.4 `ua skills` command group** (`list`, `packs`, `show <id>`, `new [path]`,
   `check`). Everything it needs already exists: `resolve_skills`,
   `SkillSet.unresolved` / `.untrusted()`, `SKILL_FILE_TEMPLATE`, `expand_pack`.
   This is now the **only remaining P0**.
5. Small, high-value follow-ups from this session, in rough order of payoff:
   * **Nested worker run journals** (§3 P0.2): a six-worker graph writes 12
     directories under `.unknown/runs/`. Give worker runs a nested path or filter
     them out of `ua runs list`.
   * **`RunResult.verification` for delegated runs**: translate
     `outcome.verification` + `outcome.unverified` into a real
     `VerificationReport` so one shape serves both paths.
   * **`SkillSet.tools()` into the session registry** (§3 P0.3): a skill can add
     prompt text and gates but not tools. Needs the same review path as plugins —
     do not just union the lists.
   * **`[streaming]` on the CLI path** (§3 P0.3): providers accept `stream=`;
     `token_pace`, `min_chunk_chars`, `flush_interval_ms`, `live_thinking` and
     `live_tools` have no consumer outside the TUI.
   * **`ua doctor` should report `SkillSet.untrusted()`** — unreviewed skills from
     a repo are a prompt-injection surface and the user has no way to see them.
6. Optional: make `role.owned_paths` a policy boundary, not just a scheduling one
   (§3 P0.1 note).
7. **P1 tests**: tui + cli wiring (orchestration CLI, executor, config wiring and
   the delegation seam are covered).
8. **P2**: install.sh, workflows, README, docs/, pyproject audit, lint+type gate.
