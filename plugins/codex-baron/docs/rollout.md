# Rollout and evaluation

## Pilot

1. Select 3–5 engineers and two representative repositories.
2. Confirm every engineer uses an individual Codex login and approved company data settings.
3. Install the marketplace plugin and repository agent profiles.
4. Review and trust the hook definitions.
5. Run for two weeks with telemetry enabled and no centralized collection.
6. Review false routes weekly and change keywords or thresholds through pull requests.

## Evaluation set

Create at least 30 sanitized tasks across:

- repository discovery;
- boilerplate implementation;
- unit and regression tests;
- ordinary feature work;
- debugging and concurrency;
- auth, payments, migrations, and infrastructure.

For every task, compare a baseline primary-only run with a routed run. Record completion, human corrections, test outcome, elapsed time, route, retries, and available usage/credit metrics. Do not claim token or cost savings from the local event count alone.

## Acceptance gates

- No credential, prompt, command, raw path, or source-code capture in telemetry.
- At least 95% of high-risk scenarios retain primary/Sol ownership and receive senior review.
- No statistically meaningful regression in task success or escaped defects.
- At least 25% reduction in primary-model usage on the evaluation set.
- Median added latency below 20 seconds for tasks that benefit from routing.
- False hard blocks below 2% of tool calls sampled during the pilot.

## Production hardening

- Replace personal subscriptions with an approved managed workspace if company policy requires centralized retention, SSO, audit, or data controls.
- Sign releases and pin the marketplace source to reviewed commits.
- Make hook and routing changes code-owner protected.
- Export aggregate counters only after security and privacy review.
- Add language-specific quality gates per repository.
- Test each Codex release against the hook wire format before broad rollout.
