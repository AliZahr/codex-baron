# Rollout and evaluation

## GitHub-imported workspace updates

ChatGPT workspaces imported from GitHub and tracking `main` sync daily. An administrator must initially import
`https://github.com/AliZahr/codex-baron` at the repository root, track `main`, and set the installation policy to
Installed for the desired roles. **Sync now** requests an immediate sync.
Generated `.codex/agents` files remain repository configuration and require rerunning `configure_repo.py`.
Automatic stamping requires the workflow's GitHub Actions token to receive `contents: write`; branch rules must allow
that Actions workflow to push directly or explicitly bypass its required-PR/check rules. Individual installs should run `codex plugin marketplace upgrade codex-baron`, then
`codex plugin add codex-baron@codex-baron`, and start a new task.

## Pilot

1. Select 3–5 engineers and two representative repositories.
2. Confirm every engineer uses an individual Codex login and approved company data settings.
3. Install the marketplace plugin and agent profiles. Prefer repository profiles and commit the generated `.codex/codex-baron-managed.json` with them so later upgrades can distinguish unchanged managed files from local customization. Use reviewed user-scoped profiles for frozen repositories that cannot accept project configuration.
4. Review and trust the hook definitions.
5. Keep telemetry disabled unless the pilot's data owner explicitly approves local metadata collection; if enabled, do not centralize raw events.
6. Review false routes weekly and change keywords or thresholds through pull requests.

## Repository install, upgrade, and removal

The supported installer platforms are macOS and Linux with Python 3.10 or newer. Run `scripts/configure_repo.py REPO` for both first install and later upgrades. It refuses symlinked managed paths, preflights all conflicts, writes through no-follow directory descriptors, records only fixed relative filenames, file modes, and SHA-256 digests in its managed manifest, and writes that manifest last.

An unchanged file is upgraded when its current digest matches the prior managed digest. Any customization stops the complete operation before writes; review the conflict and merge it manually, or use `--force` only when replacement is intentional. Caught partial install/remove failures are rolled back to the pre-run file contents and modes, although empty directories can remain. Process termination and machine/filesystem failures are outside the in-process rollback boundary and require a rerun plus manual review.

Use `scripts/configure_repo.py REPO --dry-run` before a production rollout and `scripts/configure_repo.py REPO --remove` to remove files that still match the prior managed digests. Removal after a plugin upgrade remains keyed to the installed manifest rather than the new bundle. Customized files are preserved unless `--force` is explicit.

## Evaluation set

Create at least 30 sanitized tasks across:

- repository discovery;
- boilerplate implementation;
- unit and regression tests;
- ordinary feature work;
- debugging and concurrency;
- auth, payments, migrations, and infrastructure.

For every task, compare a baseline primary-only run with a routed run. Record completion, human corrections, test outcome, elapsed time, route, retries, and available usage/credit metrics. Do not claim token or cost savings from the local event count alone.

Capture each paired run with `codex exec --ephemeral --json`. `scripts/usage_meter.py BASELINE BARON` is a root-thread-only diagnostic and must not gate a run that delegates work. Use `scripts/paired_benchmark.py` for delegation: it preserves authoritative root and bound-descendant rollouts and aggregates their tokens and commands. Keep repository state, prompt, primary model, reasoning effort, permissions, and tools identical. Repeat pairs in alternating order to limit cache and ordering bias.

If custom agents cannot be spawned by the installed Codex version during an ephemeral run, omit `--ephemeral` from both sides of the pair. Review worker transcripts for duplicated repository scans: the primary should consume concise cited findings and limit itself to focused verification. Record total commands and uncached input tokens as diagnostic metrics, because increased cached input can still dominate total usage.

For individual clean repositories, prefer `scripts/paired_benchmark.py REPO --prompt-file TASK --reasoning-effort medium --sandbox read-only --accept-inherited-tools`. Use a neutral task prompt without Baron, worker names, or delegation instructions. Before spending tokens, the runner requires all Baron hooks to be trusted and enabled and verifies that the effective `bulk_reader` profile matches the reviewed bundle. It preserves the public streams, stderr, authoritative root rollouts, and bound descendant rollouts; rejects repository drift or runtime mismatch; and exits nonzero unless aggregate Baron usage saves tokens within the command and failure budgets. Broad-discovery pairs must prove one completed `bulk_reader` handoff and a matching session tree. The privacy-safe manifest records hashes, aggregate counters, execution settings, runtime identity, and routing evidence. Raw preserved artifacts can contain prompts, commands, source excerpts, and paths, so keep them local under the repository's data policy. Alternate run order across repetitions.

The meter bounds input at 64 MiB/2 MiB per line/one million records and fails closed for malformed or incomplete usage, duplicate turn identities, and out-of-order sequence fields. Keep raw JSONL and stderr local under the repository's data-handling policy; do not upload them as a substitute for the privacy-safe manifest.

The paired runner's efficiency result is not a release decision. Require an independent quality attestation for both baseline and Baron answers. The attestation must bind the comparison hash, prompt/repository hashes, both JSONL hashes, and rubric hash; identify the evaluator and version; and report passing answers with nonnegative correction counts. Run `scripts/release_verifier.py COMPARISON QUALITY RUBRIC` to reparse usage and delegation events, require the enabled-hook attestation and a real worker handoff, and reject duplicate-key, malformed, stale, changed, or failing evidence before emitting the privacy-safe `production_release.json` approval record.

## Acceptance gates

- No credential, prompt, command, raw path, or source-code capture in telemetry.
- At least 95% of high-risk scenarios retain primary/Sol ownership and receive senior review.
- No statistically meaningful regression in task success or escaped defects.
- At least 25% reduction in primary-model usage on the evaluation set.
- Routed tasks do not increase median total tokens; any exception must show a measured latency, quality, or parallelism benefit.
- Routine workers receive minimal briefs without inherited parent history, and duplicated search/read commands remain below 10% in sampled traces.
- Median added latency below 20 seconds for tasks that benefit from routing.
- False hard blocks below 2% of tool calls sampled during the pilot.

## Production hardening

- Replace personal subscriptions with an approved managed workspace if company policy requires centralized retention, SSO, audit, or data controls.
- Sign releases and pin the marketplace source to reviewed commits.
- Make hook and routing changes code-owner protected.
- Export aggregate counters only after security and privacy review.
- Add language-specific quality gates per repository.
- Test each Codex release against the hook wire format before broad rollout.
- Qualify Python 3.10+ on macOS and Linux in CI; do not claim Windows support until the hook launcher is portable and tested there.
- Pin the plugin version used by a benchmark and verify every artifact SHA-256 before analysis.
- Treat a missing, stale, or incompatible manifest as an invalid benchmark; never merge partial usage into a release decision.
- Set a retention period for raw event streams and store only aggregate manifests in any shared location.
