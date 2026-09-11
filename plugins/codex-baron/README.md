# Codex Baron

Codex Baron keeps the standard Codex chat, CLI, or IDE experience while routing bounded engineering work to specialized models.

It provides:

- deterministic prompt recommendations before each turn;
- `bulk_reader` on GPT-5.6 Terra for broad read-only exploration;
- `code_writer` and `test_writer` on GPT-5.6 Luna for predictable work;
- `senior_reviewer` on GPT-5.6 Sol for high-risk reasoning;
- a hard guard against simple full reads of oversized files;
- conservative senior-review recommendations for sensitive paths;
- local metadata-only telemetry and a reporting command.

## Install

### Codex desktop app

Codex Baron is distributed through its GitHub marketplace. Add that marketplace once from a terminal:

```bash
codex plugin marketplace add AliZahr/codex-baron
```

Then install it in the Codex application:

1. Open the **Plugins** tab in the ChatGPT desktop app.
2. Open **Personal**, search for **Codex Baron**, and open its details.
3. Select the **+** button to install it.
4. Start a new Codex task so the bundled skill and hooks are loaded.
5. Review and trust the plugin hooks when Codex prompts you. They run the bundled Python routing script locally.

If Codex Baron does not appear immediately after adding the marketplace, reopen the Plugins tab or restart the desktop app.

### Codex CLI

Install directly from the terminal with:

```bash
codex plugin marketplace add AliZahr/codex-baron
codex plugin add codex-baron@codex-baron
```

You can also start `codex`, enter `/plugins`, switch to the Codex Baron marketplace, open the plugin, and install it from the interactive browser.

For local development from the repository root:

```bash
codex plugin marketplace add "$PWD"
codex plugin add codex-baron@codex-baron
```

Whichever installation method you use, start a new Codex task or CLI session afterward. Codex loads newly installed plugin skills and hooks when a new task begins.

The qualified production runtime is Python 3.10 or newer on macOS and Linux. Windows is not yet supported: the hook command uses a POSIX environment launcher, and the repository installer uses directory-descriptor and no-follow filesystem operations available on the qualified platforms.

Each engineer signs into Codex using their own account:

```bash
codex login
codex login status
```

Do not share `auth.json`, session tokens, passwords, or API keys.

## Configure a repository's named agents

Plugin skills and hooks load from the plugin. Current custom-agent profiles are repository configuration, so install the included profiles once per repository:

```bash
python3 plugins/codex-baron/scripts/configure_repo.py /absolute/path/to/repository
```

Review the generated `.codex/agents/*.toml`, `.codex/codex-baron.json`, and `.codex/codex-baron-managed.json`, then commit them if they should apply to the team. The managed manifest contains only its schema, the plugin name/version, fixed repository-relative filenames, file modes, and SHA-256 digests; it contains no repository path, prompt, source content, or account data.

For a personal machine that must route in frozen or third-party repositories without adding project files, install the reviewed profiles at user scope instead:

```bash
install -d "$HOME/.codex/agents"
install -m 0644 plugins/codex-baron/agents/*.toml "$HOME/.codex/agents/"
```

Project profiles take precedence over user profiles. The paired benchmark verifies that the effective `bulk_reader` profile exactly matches the reviewed plugin bundle and fails before spending tokens when it is missing, symlinked, or modified.

Configuration is idempotent and is also the upgrade command. On a later plugin version, files that still match the digest recorded by the previous install are upgraded automatically. A locally customized file causes the whole operation to stop before writing anything, so it can be reviewed and merged manually. A legacy install without a managed manifest can be adopted automatically only when its files already match the current bundle; otherwise review it and use `--force` if replacement is intended.

Within one installer process, install and upgrade writes are applied as one transaction: the manifest is written last, and caught write failures roll earlier file changes back to their pre-run content and mode. Do not run concurrent installer processes against the same repository. Empty directories may remain after rollback. An external process kill or machine/filesystem failure cannot be rolled back automatically, so rerun the command and review its conflict report before using `--force`.

Remove files that are unchanged from their recorded managed version with:

```bash
python3 plugins/codex-baron/scripts/configure_repo.py /absolute/path/to/repository --remove
```

Removal uses the prior manifest digests, so it remains safe after the installed plugin has been upgraded. Modified managed files are preserved as conflicts unless `--force` is explicitly supplied. The remove transaction restores earlier removals after a caught failure, and empty `.codex` directories remain in place. The installer refuses symlinked managed path components and never intentionally writes or unlinks outside the resolved repository root.

## Use

Prompt normally. Examples:

```text
Implement rate limiting for password reset and add regression tests.
Find why checkout sometimes charges twice.
Map every caller of PaymentService without changing code.
Generate fixtures matching the adjacent test suite.
```

The prompt classifier automatically delegates only broad read-only discovery. Small lookups and all writing/testing stay with the primary unless the user explicitly requests a named worker or parallel delegation. Read-only maps of auth and other sensitive code start with `bulk_reader`; sensitive changes and security conclusions stay with the primary or `senior_reviewer`. Baron does not automatically delegate when the primary already uses the recommended worker model.

Discovery workers receive a minimal self-contained brief without the parent transcript. A session-scoped hook guard gives one worker exclusive ownership of the scan, caps it at four commands, and permits the primary one focused verification command after handoff. The worker profile instructs searches to cap output at 160 lines; this output limit is reviewed from benchmark evidence rather than enforced by the pre-tool hook. If workers are unavailable, retrying the first denied primary command enters local fallback instead of blocking the task.

## How it works

Baron is an orchestration policy around the normal Codex primary agent. It does not hand the full conversation to a cheaper model or replace the primary. The primary keeps ownership of user intent, decomposition, decisions, integration, diff review, tests, and the final response. Specialists receive only concrete, bounded subtasks whose results the primary can verify.

In practice, the workflow is:

1. You describe the engineering outcome normally; no special command is required.
2. Before the turn starts, Baron's local hook classifies the prompt by intent, risk, and likely delegation benefit.
3. Routine work stays with the primary Codex agent. Broad read-only exploration may be assigned to `bulk_reader`; explicitly delegated mechanical edits or tests can use `code_writer` or `test_writer`; debugging and sensitive decisions stay with the primary or `senior_reviewer`.
4. A delegated specialist receives a small, self-contained assignment instead of the whole conversation.
5. Lifecycle guards enforce one discovery owner, a bounded command budget, and a real worker handoff.
6. The primary verifies the evidence, integrates any changes, runs appropriate checks, and gives you the final result.

Baron recommends and constrains routing; Codex still controls execution, sandbox permissions, approval prompts, and the final answer. If a useful delegation cannot start, Baron permits a bounded local fallback so the task can continue without claiming that delegation succeeded.

```text
User prompt
    │
    ▼
UserPromptSubmit hook classifies the work
    │
    ├─ primary ───────────────────────────────────────────────┐
    │                                                        │
    └─ specialist recommendation                            │
         │                                                   │
         ▼                                                   │
      risk + benefit gates                                   │
         │                                                   │
         ├─ keep with primary                                │
         │                                                   │
         └─ spawn one bounded worker                         │
              │                                              │
              ▼                                              │
        lifecycle guard verifies identity, role,             │
        ownership, command budget, and completion             │
              │                                              │
              ▼                                              │
        cited worker result → one focused primary check ──────┘
                                │
                                ▼
                     integrate, test, and answer
```

### Routing precedence

The router applies the following order. Earlier decisions take precedence over later keyword matches:

1. A sensitive decision involving security, authentication, cryptography, payments, billing, secrets, migrations, concurrency, or similar high-risk work stays with the primary or routes to `senior_reviewer`. Explicitly naming a lower-tier writer cannot downgrade that decision.
2. An explicitly named Baron role is honored when it is compatible with the risk rules.
3. Otherwise, prompt intent is classified:
   - debugging terms such as `debug`, `bug`, `crash`, `failure`, `regression`, `incorrect`, and `why` recommend `senior_reviewer`;
   - discovery terms such as `explore`, `analyze`, `trace`, `find`, and `map` recommend `bulk_reader`;
   - test, fixture, mock, snapshot, or coverage work recommends `test_writer`;
   - scaffolding, configuration, boilerplate, or documentation generation recommends `code_writer`;
   - everything else remains with the primary.
4. If the primary already uses the recommended role's model and the role was not explicitly requested, Baron keeps the work with the primary.
5. The benefit gate suppresses automatic delegation for small discovery tasks and for unrequested writing/testing. Broad read-only discovery can delegate automatically; write and test workers require an explicit request or measured parallel benefit.

Keyword precedence is deliberately conservative. For example, `Find why checkout sometimes charges twice` routes to `senior_reviewer` because `why` is a debugging signal even though `find` is also a discovery signal. Prompt normally, but use an explicit role name when you need a specific compatible route.

### Bounded discovery lifecycle

For broad repository discovery, the skill and hook enforce one owner and a small evidence budget:

- the primary must read the installed Baron skill before repository discovery;
- exactly one `bulk_reader` is spawned before the primary runs repository tools;
- the worker receives a minimal `fork_turns="none"` brief with scope, paths, constraints, output format, and acceptance criteria;
- a nonempty worker ID and a real `SubagentStart` lifecycle event are required;
- the hook counts at most four worker commands; worker instructions require searches to cap output at 160 lines per command;
- a wait must correspond to the active worker; an absent or empty final response is not a handoff;
- after a nonempty worker result, the primary may run one focused verification command;
- the primary then owns conclusions, edits, tests, and the final answer.

The hook treats lifecycle events as authoritative. A successful-looking spawn API response alone does not prove that a worker actually started. Session state records a hash of the root session, stable worker identity, route, skill-read count, spawn/start/stop state, worker command count, mailbox waits, primary verification count, and completion. State files are private, size-limited, atomically replaced under a kernel-owned lock, and pruned after their retention window.

Current `codex exec --json` versions can omit the spawn item and serialize a mailbox wait without a target worker ID. In that case, the paired benchmark can use the separately captured hook lifecycle receipt, cryptographically bound to the root JSONL session, to prove the handoff. The receipt contains bounded counters and hashes rather than prompts, commands, source, repository paths, account data, or worker IDs. Missing, cross-session, contradictory, incomplete, or over-budget evidence fails closed.

If a worker cannot start, the first denied primary discovery command may be retried once in bounded local-fallback mode. Fallback is not reported as successful delegation, and it does not permit parallel or duplicate discovery scans.

### Responsibility boundaries

| Responsibility | Owner |
|---|---|
| User intent, task decomposition, final decisions | Primary |
| Broad read-only scan and cited repository map | `bulk_reader` |
| Mechanical implementation explicitly delegated by the user | `code_writer` |
| Tests, fixtures, mocks, and snapshots explicitly delegated by the user | `test_writer` |
| Debugging conclusions and high-risk review | Primary or `senior_reviewer` |
| Diff inspection, integration, formatter/static checks/tests, final response | Primary |

Generated code or tests are never complete merely because a worker returned successfully. The primary must inspect the changed sections and run checks appropriate to the repository and risk.

## Configuration

Repository overrides live in `.codex/codex-baron.json`:

```json
{
  "version": 1,
  "large_file_lines": 500,
  "telemetry_enabled": false,
  "prompt_routing_enabled": true,
  "delegation_benefit_gate_enabled": true,
  "exclusive_discovery_guard_enabled": true,
  "bulk_reader_model": "gpt-5.6-terra",
  "discovery_worker_command_limit": 4,
  "primary_verification_command_limit": 1,
  "state_lock_timeout_seconds": 0.5,
  "state_lock_stale_seconds": 30,
  "telemetry_max_bytes": 1048576,
  "telemetry_max_line_bytes": 4096,
  "large_read_guard_enabled": true,
  "sensitive_review_enabled": true,
  "sensitive_patterns": ["auth", "security", "payment", "migration"]
}
```

Keep `bulk_reader_model` identical to the model in `.codex/agents/bulk_reader.toml`; it lets lifecycle hooks distinguish the intended reader from unrelated default subagents on Codex versions that report custom agents as `default`.

Environment overrides:

- `ENGINEERING_ROUTER_LARGE_FILE_LINES=750`
- `ENGINEERING_ROUTER_TELEMETRY=on` or `off`

The large-read guard intentionally blocks only simple, unpiped `cat`/`bat` commands. Targeted tools such as `rg`, `sed -n`, and piped searches remain available. Hooks are a useful guardrail, not a complete security boundary.

## Telemetry and privacy

Telemetry is opt-in and disabled by default. When explicitly enabled, it is written to the plugin data directory as `events.jsonl`. It contains timestamps, route names, reason categories, model names, tool names, agent types, hashed session identifiers, extension names, and coarse line-count buckets.

It never intentionally records prompts, commands, source code, raw paths, account identifiers, or credentials. Enable or disable it with the repository setting or environment variable above.

Generate a local summary:

```bash
python3 plugins/codex-baron/scripts/router_report.py /path/to/events.jsonl
```

## Token usage meter

Baron cannot know the tokens a counterfactual non-Baron run would have used from routing events alone. For an exact comparison, run the same task from the same repository state once without Baron and once with Baron, capturing Codex's machine-readable event stream:

```bash
# Baseline: same primary model, Baron disabled.
codex exec --ephemeral --json \
  -C /absolute/path/to/repository \
  --sandbox workspace-write \
  -m gpt-5.6-sol \
  -c 'plugins.codex-baron@codex-baron.enabled=false' \
  "<task>" > baseline.jsonl

# Routed run: same primary model, Baron enabled.
codex exec --ephemeral --json \
  -C /absolute/path/to/repository \
  --sandbox workspace-write \
  -m gpt-5.6-sol \
  -c 'plugins.codex-baron@codex-baron.enabled=true' \
  "<task>" > baron.jsonl
```

Start both runs from identical clean checkouts or disposable worktrees. Then display the root-thread-only diagnostic meter:

```bash
python3 plugins/codex-baron/scripts/usage_meter.py baseline.jsonl baron.jsonl
```

Use `--json` for machine-readable output. This two-file command reports only the supplied root streams. Do not use it as an efficiency or release gate when either run delegated work, because descendant usage is absent. For non-delegating runs it reports input, cached input, uncached input, output, reasoning output, total tokens, command counts, and command failures. Reasoning output is shown separately and is not added twice.

For a clean read-only repository, the paired runner performs both executions, prints the meter automatically, preserves artifacts, and applies an efficiency gate:

```bash
python3 plugins/codex-baron/scripts/paired_benchmark.py /absolute/path/to/repository \
  --prompt-file /absolute/path/to/task.txt \
  --model gpt-5.6-sol \
  --reasoning-effort medium \
  --accept-inherited-tools \
  --sandbox read-only
```

The paired runner accepts only neutral prompts without Baron or worker-routing directives. It refuses a dirty repository, fails preflight unless every Baron hook is explicitly trusted and enabled, and verifies the effective `bulk_reader` profile before a broad-discovery run. It requires an explicit reasoning effort and read-only sandbox for an efficiency pass, compares the canonical runtime digest with the actual installed cache, verifies that `HEAD` remains unchanged, and waits for detached subagent output to settle before measuring. For prompts classified as broad discovery, it must verify exactly one completed `bulk_reader` handoff. It prefers the raw Baron JSONL when the CLI serializes worker IDs; on CLI versions that omit the spawn but emit only normalized wait records, it uses a captured hook lifecycle receipt cryptographically bound to the root JSONL session. Missing, malformed, cross-session, incomplete, contradictory, or over-budget handoffs make the run ineligible even if it used fewer tokens. Tool availability is inherited identically by both runs, but the effective set cannot currently be queried from Codex; `--accept-inherited-tools` is an explicit operator acknowledgement of that limitation and is recorded in the manifest. `comparability_verified` therefore remains false while the narrower `efficiency_gate_eligible` field records whether enforceable efficiency controls and that acknowledgement are present. The meter structurally parses shell and collaboration command lifecycle objects and refuses malformed, ambiguous, or incomplete streams rather than undercounting. It also rejects any increase in failed commands. An efficiency pass is not a production release approval: quality evidence is required separately.

If the installed Codex version cannot create custom agents from an ephemeral run, omit `--ephemeral` from both commands. Keep execution mode identical within every pair; never mix an ephemeral baseline with a persisted Baron run.

The routing `events.jsonl` file remains metadata-only and intentionally contains no prompts, source code, or token usage. The standalone meter reads only the supplied root JSONL files. The paired runner additionally freezes authoritative root and bound-worker rollouts, verifies their session tree, and charges preserved descendants' tokens and commands to the corresponding run.

The efficiency manifest is not sufficient for production approval. After independently evaluating both answers, create a strict quality attestation containing `comparison_sha256`, `prompt_sha256`, `repository_head`, `repository_status_sha256`, the baseline and Baron JSONL SHA-256 values, `rubric_sha256`, an evaluator `id`/`version`, and passing `baseline`/`baron` answer records with nonnegative `corrections`. Verify it with:

```bash
python3 plugins/codex-baron/scripts/release_verifier.py \
  artifacts/comparison.json artifacts/quality.json /absolute/path/to/rubric.txt
```

Only this verifier can write `production_release.json`. It independently reparses usage and delegation events and rejects disabled-hook attestations, missing or invalid worker handoffs, empty waits, duplicate-key JSON, missing or stale evidence, changed artifacts/rubric, failed answers, additional Baron command failures, missing evaluator identity, and malformed correction counts. The output contains hashes and evaluator metadata only; token savings and command counts alone never imply production approval.

Comparison manifests are privacy-safe attestations. They contain a prompt hash, a Git revision/status hash, model/reasoning/sandbox settings, inherited-tool policy, Codex CLI version, installed runtime digest, plugin version, run order, root/descendant/aggregate usage, and SHA-256/size metadata for every preserved artifact. Raw public streams, stderr, authoritative root rollouts, and bound-worker rollouts are sensitive local evidence and can contain prompts, commands, source excerpts, and paths. Event streams are bounded while parsing; malformed, incomplete, ambiguous, duplicated, cross-session, or out-of-order evidence fails closed.

## Production operation

Treat benchmark output as sensitive local evidence: retain the JSONL/stderr artifacts only where the task data is approved, and share `comparison.json` only after checking its schema. Verify the listed artifact hashes before moving artifacts or importing results. A pair is comparable only when its prompt hash, clean-checkout revision/status hash, model, reasoning effort, sandbox, tool set, plugin version, ephemeral mode, and run order are recorded and held constant. Repeat with the opposite order to reduce cache/order bias.

The hook stores only hashed session state unless telemetry is explicitly enabled. State updates use kernel-owned file locks capped at 0.5 seconds; optional telemetry uses a separate 0.05-second acquisition and is capped at 1 MiB and 4 KiB per line. Environment lock-timeout overrides are clamped so the combined wait remains below the host hook deadline. If discovery state cannot be updated safely, Baron keeps the work with the primary or denies additional discovery instead of allowing duplicate scans.

## Production-qualified build

Build `1.0.0+codex.20260911065504` is production-qualified on Python 3.10+ for macOS and Linux. The approval used comparison schema v3 against Hamsa commit `e6926c327d262ab70aae5c068fcfbab99e024291`, Codex CLI `0.153.4`, `gpt-5.6-sol` at medium reasoning, and a read-only sandbox.

The paired broad-discovery audit charged root and bound-descendant usage, verified one completed `bulk_reader` handoff through its session-bound hook receipt, and passed the efficiency gate:

- total tokens: `309,798` baseline → `254,129` Baron, saving `55,669` (`18.0%`);
- commands started: `7` → `7`, within the recorded maximum increase of two;
- command failures: `0` → `0`;
- independent quality review: both answers passed with zero corrections.

`release_verifier.py` emitted `production_approved` after independently reparsing the measured artifacts and validating all hashes. The approval is bound to comparison SHA-256 `8faea165e92ccd3fffb34617fcd40585440f8165533bc8f6e0a10ebfdfdd9610`, quality-attestation SHA-256 `1facb96482abfa6366d164e3cf9c191fcaa9a6370250306b58c9bfe74233e7ab`, rubric SHA-256 `eaaeeaece638487f2745a4e6831121a9e126229026dc1fc3526a81d0c4efe0e8`, and evaluator `codex-independent-senior-release-review` version `1.9.0`.

## Verify

The MVP uses only the Python standard library:

```bash
python3 -m unittest discover -s plugins/codex-baron/tests -p 'test_*.py'
python3 plugins/codex-baron/scripts/benchmark.py
```

See [docs/rollout.md](docs/rollout.md) for a production rollout and benchmark plan.
