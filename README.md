# Codex Baron

Codex Baron routes engineering subtasks across specialized Codex models while keeping a primary agent responsible for integration, safety, and verification.

It adds a routing skill and local lifecycle hooks to the normal Codex experience. You keep prompting Codex normally; Baron decides whether a bounded subtask would benefit from a specialized agent while the primary agent retains ownership of the final result.

## Install

### Codex desktop app

Codex Baron is distributed through its GitHub marketplace. Add the marketplace once from a terminal:

```bash
codex plugin marketplace add AliZahr/codex-baron
```

Then install it from the Codex application:

1. Open the **Plugins** tab in the ChatGPT desktop app.
2. Open **Personal**, search for **Codex Baron**, and open its details.
3. Select the **+** button to install it.
4. Start a new Codex task so the plugin's skill and hooks are loaded.
5. Review and trust the hooks when Codex prompts you. They run the bundled Python routing script locally.

If the listing does not appear immediately, reopen the Plugins tab or restart the desktop app.

### Codex CLI

Install it directly from a terminal:

```bash
codex plugin marketplace add AliZahr/codex-baron
codex plugin add codex-baron@codex-baron
```

Alternatively, start `codex`, enter `/plugins`, switch to the Codex Baron marketplace, open the plugin, and install it from the interactive browser.

Start a new CLI session after installation. Newly installed plugin skills and hooks are loaded when a new task or session begins.

### Local development

From a clone of this repository:

```bash
codex plugin marketplace add "$PWD"
codex plugin add codex-baron@codex-baron
```

The qualified production runtime is Python 3.10 or newer on macOS and Linux. Windows is not currently supported.

## Configure a repository

Codex Baron bundles the routing skill and hooks, but named custom-agent profiles are repository configuration. From a clone of this repository, install the reviewed profiles into a target repository:

```bash
python3 plugins/codex-baron/scripts/configure_repo.py /absolute/path/to/your/repository
```

Profile settings may be selected per role; options can be repeated. For example:

```bash
python3 plugins/codex-baron/scripts/configure_repo.py /absolute/path/to/your/repository \
  --model bulk_reader=gpt-5.6-terra --model code_writer=gpt-5.6-luna \
  --reasoning bulk_reader=medium --fast
```

Use `--no-fast` (or omit both speed flags) for standard service tier. Each run is declarative: repeat every
model and reasoning override you want to keep, because omitted roles return to the bundled defaults. Supported
reasoning values are `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, and `ultra`.
Model/reasoning compatibility is the user's responsibility.

Review the generated `.codex/agents/*.toml`, `.codex/codex-baron.json`, and `.codex/codex-baron-managed.json` files before committing them to the target repository. Running the same command later safely upgrades unchanged managed files and stops on local customizations that need manual review.

## How it works

1. You describe the engineering task normally; no Baron-specific command is required.
2. Before the turn begins, a local hook classifies the prompt by intent, risk, and likely delegation benefit.
3. Routine work stays with the primary Codex agent. Broad read-only discovery may use `bulk_reader`; explicitly delegated mechanical changes or tests may use `code_writer` or `test_writer`; debugging and sensitive decisions stay with the primary or `senior_reviewer`.
4. A specialist receives a small, self-contained assignment rather than the full conversation.
5. Lifecycle guards enforce one discovery owner, a bounded command budget, and a real worker handoff.
6. The primary reviews the evidence, integrates changes, runs appropriate checks, and produces the final response.

| Role | Purpose | Model |
|---|---|---|
| Primary | User intent, decisions, integration, verification, final response | Your selected Codex model |
| `bulk_reader` | Broad, read-only repository exploration | GPT-5.6 Terra |
| `code_writer` | Explicitly delegated mechanical implementation and documentation | GPT-5.6 Luna |
| `test_writer` | Explicitly delegated tests, fixtures, mocks, and snapshots | GPT-5.6 Luna |
| `senior_reviewer` | Debugging and high-risk reasoning or review | GPT-5.6 Sol |

Baron recommends and constrains routing; Codex still controls execution, sandbox permissions, approvals, integration, and the final answer. Sensitive work such as authentication, security, payments, migrations, and concurrency cannot be downgraded to a lower-tier writer by naming one in a prompt.

## Use

Prompt Codex as usual. For example:

```text
Implement rate limiting for password reset and add regression tests.
Find why checkout sometimes charges twice.
Map every caller of PaymentService without changing code.
Generate fixtures matching the adjacent test suite.
```

See [the detailed plugin documentation](plugins/codex-baron/README.md) for routing precedence, lifecycle enforcement, configuration, privacy, telemetry, production qualification, verification, and rollout guidance.
