# About this fork

`aliyun-fc/mini-swe-agent` exists for one reason: to ship the `e2b` environment class
ahead of upstream, so that Alibaba Cloud Function Compute's Agent Sandbox best-practice
documentation can depend on an importable class instead of asking readers to copy 130
lines of glue into their project.

Upstream [SWE-agent/mini-swe-agent#792](https://github.com/SWE-agent/mini-swe-agent/pull/792)
proposes the same feature. It has been open since 2026-03-24 and green since 2026-07-14,
waiting on maintainer review. Our implementation is deliberately shaped to match it.

## Upstream baseline

| | |
| --- | --- |
| Forked from | `SWE-agent/mini-swe-agent` |
| Base commit | `25941c8` (`chore: update pre-commit hooks (#917)`, upstream `main`) |
| Upstream version at base | `2.4.6` |
| Our version | `2.4.6+fc.1` (PEP 440 local version — deliberately not publishable to PyPI) |

## What we changed

The change set, spread over a few `feat(env):` commits:

- `src/minisweagent/environments/extra/e2b.py` — new environment class.
- `src/minisweagent/environments/__init__.py` — registers the `e2b` key.
- `src/minisweagent/run/benchmarks/swebench.py` — per-instance image injection for `e2b`,
  plus `_teardown_environment()` so the sandbox is released on every exit path.
- `pyproject.toml` — new `e2b` extra, also pulled into `full`.
- Docs, `mkdocs.yml` nav, `README.md`, and tests for the above.

Nothing else is touched, so merging upstream `main` should stay mechanical.

### Deltas against PR 792

The class follows PR 792 closely (same name, same `e2b` registry key, no
vendor-specific fields) and adds seven things we hit while running this on real
evaluation images. Each is generic — none of them mention any particular cloud:

1. **`mkdir -p <cwd>` at startup.** `docker run -w` creates the working directory; a
   sandbox does not. The failure surfaces at process spawn *without* an exit code, so
   `execute()` reports it as an infrastructure error on every single step.
2. **`git config --global --add safe.directory <cwd>` at startup.** When the repository
   was created by a different uid than the one commands run as, git refuses it as
   dubiously owned, which breaks `git diff` and therefore the whole submission. Template
   builds do not support build steps on every backend, so this has to happen at runtime.
3. **`run_as_user` is configurable** (defaulting to `root`) rather than hard-coded.
4. **`serialize()` records `sandbox_id` and `template`.** Without them a trajectory cannot
   be traced back to the sandbox it ran in — the only handle you have in a batch run.
5. **The template name hashes the build options, not just the image name.** Template
   lookup short-circuits on `Template.exists()`, so a name derived from the image alone
   silently returns a template built with a stale `memory_mb`/`cpu_count`. This is a real
   bug in PR 792 and worth reporting upstream on its own.
6. **`interpreter` is honoured** like `docker` and `contree` do. The SDK already spawns
   commands through `bash -l -c`, so this is one shell more than strictly necessary — but
   `config/benchmarks/swebench.yaml` sets `interpreter`, and a pydantic model that ignores
   extra keys would have dropped it without a word.
7. **`domain` and `api_url` are configurable**, not just `api_key`. Both are standard e2b
   SDK connection parameters. PR 792 exposes only `api_key`, which forces the control plane
   to be selected through process-global environment variables — unworkable when one
   process has to talk to two of them. A `_create_sandbox()` hook is also provided so
   subclasses can pass extra `Sandbox.create` options (metadata, network, volumes).

Deliberately *not* carried over from our earlier private implementation, to keep the class
minimal: a `request_timeout` passthrough (inert in current e2b SDKs — the command `timeout`
bounds the whole stream), host env forwarding, and evaluation-suite-specific
working-directory probing. Those belong in the orchestration layer, not here.

## Pinning this fork

```bash
pip install "mini-swe-agent[e2b] @ git+https://github.com/aliyun-fc/mini-swe-agent@v2.4.6-fc.1"
```

Tags only — **do not publish a GitHub Release**. `.github/workflows/release.yaml` triggers
on `release: published` and would attempt a PyPI upload.

## When upstream merges #792

1. Switch the documentation's pin to the upstream release that contains it.
2. Re-propose our seven deltas upstream as separate, individually reviewable PRs.
3. Retire this fork.
