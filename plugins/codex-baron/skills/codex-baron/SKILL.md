---
name: codex-baron
description: Automatically route engineering work among bulk-reader, code-writer, test-writer, and senior-reviewer Codex agents. Use for repository exploration, implementation, debugging, tests, reviews, migrations, security-sensitive changes, and multi-file engineering tasks.
metadata:
  short-description: Route engineering work to efficient Codex agents
---

# Codex Baron

Act as the primary orchestrator. Keep responsibility for the user's intent, integration, verification, and final answer. Delegate only concrete, bounded subtasks whose results can be checked.

The plugin's prompt hook may add a `Codex Baron recommendation`. Treat it as a strong routing hint, then apply the risk rules below. If custom agent profiles are unavailable, create an equivalent subagent with the specified model and instructions. If subagents are unavailable, continue locally and state that routing was not possible.

## Routing table

| Task | Agent | Model | Default permissions |
|---|---|---|---|
| Broad repository discovery, large-file analysis, call-path mapping | `bulk_reader` | `gpt-5.6-terra` | read-only |
| Predictable implementation, scaffolding, config, mechanical refactors, docs | `code_writer` | `gpt-5.6-luna` | workspace-write |
| Unit tests, fixtures, mocks, snapshots, regression tests | `test_writer` | `gpt-5.6-luna` | workspace-write |
| Debugging, architecture, concurrency, migrations, security, auth, payments, final high-risk review | `senior_reviewer` | `gpt-5.6-sol` | read-only |

## Required behavior

1. Parse the request into exploration, reasoning, implementation, testing, and validation work.
2. Skip delegation for a trivial task that is cheaper to complete directly.
3. Delegate independent exploration or generation tasks in parallel when useful.
4. Give each subagent explicit scope, paths, constraints, output format, and acceptance criteria. Never send the whole vague user request.
5. Do not delegate architectural decisions, root-cause conclusions, security decisions, or destructive operations to Luna.
6. Require `senior_reviewer` for changes touching authentication, authorization, cryptography, payments, billing, secrets, infrastructure, database migrations, concurrency, or public APIs.
7. Inspect the returned diff or focused sections before accepting generated code. Do not trust a summary as proof.
8. Run the repository's relevant formatter, static checks, and tests after integration.
9. Report routing only when it helps the user understand latency, risk, or an unavailable agent. Avoid noisy narration.

## Efficient reading

- Prefer `rg`, symbol search, manifests, indexes, and targeted line ranges.
- For a file over the configured threshold, delegate a focused question to `bulk_reader` or read only the necessary range.
- The parent should receive a concise map of paths, symbols, line ranges, dependencies, and unresolved questions—not copied file contents.
- When editing, the parent may read the exact relevant section directly.

## Safe generation

- `code_writer` and `test_writer` must follow reference files from the repository.
- Give workers exclusive file ownership when running in parallel.
- Generated code is not complete until the primary agent verifies the diff and relevant tests pass.
- Escalate ambiguity or domain-sensitive choices back to the primary agent.

## Failure and fallback

- If a worker fails, retry once only when the failure is transient or scope can be reduced.
- If a worker reports uncertainty, inspect the relevant code with the primary agent or `senior_reviewer`.
- If the requested model is unavailable on the user's plan, use the nearest available efficient model and disclose the fallback in the final answer.
- Never bypass permissions, hook trust, repository policy, or user approval to make routing succeed.
