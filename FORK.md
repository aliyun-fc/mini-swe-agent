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
  plus `_teardown_environment()` so the sandbox is released on every exit path, and a
  bounded shutdown on SIGTERM. `atexit` does not run on SIGTERM, so a scheduler or CI
  timeout would otherwise leave one running -- and billed -- cloud sandbox per in-flight
  instance. Pending instances are cancelled, the running ones get
  `MSWEA_SHUTDOWN_GRACE_SECONDS` (120s) to submit, and past that their environments are
  released and the process exits 130. The deadline is the point: simply taking the `^C`
  path waits for every running instance, so a scheduler that follows its own grace period
  with SIGKILL still leaks their sandboxes.
- `pyproject.toml` — new `e2b` extra, also pulled into `full`.
- Docs, `mkdocs.yml` nav, `README.md`, and tests for the above.

Nothing else is touched, so merging upstream `main` should stay mechanical.

### Deltas against PR 792

The class follows PR 792 closely (same name, same `e2b` registry key, no
vendor-specific fields) and adds twelve things we hit while running this on real
evaluation images. Each is generic — none of them mention any particular cloud:

1. **`mkdir -p <cwd>` at startup.** `docker run -w` creates the working directory; a
   sandbox does not. The failure surfaces at process spawn *without* an exit code, so
   `execute()` reports it as an infrastructure error on every single step. This one is not
   best-effort: if the directory cannot be created -- an unprivileged `run_as_user`, say --
   initialization stops, because otherwise every later command fails for a reason that has
   nothing to do with the task.
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
8. **Template builds are serialised per template name, and a failed template can be
   rebuilt.** `get_or_build()` checked `Template.exists()` outside any mutual exclusion, so
   concurrent first builds of one image all decided to build the same alias: measured on
   real infrastructure, 4 threads produced 3 losers and 8 threads produced 7, each failing
   with `409 ... resource conflict` and leaving an orphan template record behind. Worse, the
   failure was unrecoverable: the alias endpoint reports existence but carries no status, so
   `exists()` keeps saying yes, while `Sandbox.create` refuses the template and a second
   `Template.build` is rejected outright. Recovery now resolves the alias, deletes the
   template and builds again — deletion is asynchronous, so it waits for the alias to
   disappear before rebuilding. A build someone else has in flight is waited for rather
   than deleted. `docker pull` is concurrency-safe and leaves no broken state, so this is a
   gap against `DockerEnvironment` rather than a cloud quirk. Two details that only show up
   under load: the build runs on a *daemon* thread, so `build_timeout` bounds the process
   and not merely the wait -- a pooled worker cannot be cancelled once started and the
   interpreter joins it on exit (measured: logic done at 1.0s, process at 8.2s) -- and the
   once-per-process bookkeeping for `skip_cache` is recorded only after the rebuild lands,
   keyed by control plane. The template name hashes just the image and its build
   parameters, so a single shared key would rebuild on the first control plane only.
9. **A timed-out command keeps the output it already produced.** PR 792 hard-codes
   `"output": ""` on the generic exception path, and the SDK's timeout carries no
   stdout/stderr at all, so everything printed before the deadline was lost — worst exactly
   where it matters most, since a slow test suite on a large repository is what runs out of
   time. Output is now collected through the streaming callbacks, and the timeout is named
   in `exception_info` instead of being indistinguishable from an infrastructure failure.
   `DockerEnvironment` salvages `TimeoutExpired.output` for the same reason.
10. **A created working directory is reported.** Both `docker run -w` and step 1 above
    create a missing `cwd` silently, which hides a misconfigured working directory
    completely: commands succeed in the empty directory, and the agent runs to completion
    and submits an empty patch. The directory is now probed before it is created, and
    `require_existing_cwd` turns that into a hard failure for repository-based benchmarks.
11. **The registry of live sandboxes holds weak references.** PR 792 keeps them in a plain
    `set`, which holds every reference count above zero and therefore makes `__del__`
    unreachable: an environment nobody holds on to any more keeps its sandbox running --
    and billed -- until the process ends. Measured: three environments dropped, followed
    by `gc.collect()`, left all three sandboxes alive. A `WeakSet` restores the finaliser
    while `atexit` keeps covering the environments that are still alive. Restoring the
    finaliser needs two things alongside it. `cleanup()` treats only a *returned* kill as
    terminal -- an exception leaves the environment in the registry for `atexit` to retry,
    since swallowing it would let one transient API error keep the sandbox billed until its
    TTL. And `__del__` does nothing while `sys.is_finalizing()`: the SDK's native runtime is
    already torn down by then, so a request from there ends the process with SIGSEGV --
    correct output, exit code 139, which quietly breaks any `a.py && b.py` chain.
12. **A negative exit code is labelled as a signal.** The SDK reports `exit_code == -1` for
    every signal, and -1 is also what this class returns for an infrastructure error, so an
    empty `exception_info` would claim a killed interpreter was an ordinary command result.
    The exit code is left alone -- 128+N cannot be recovered -- but it is now explained.
    (The common case is unaffected: when a *child* is killed, the shell survives and
    reports 137/143 as usual, so this only concerns the interpreter itself being killed.)

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

The tag is cut after this branch merges, so until then pin the full commit SHA of the
branch head instead — anything mutable, a branch included, makes an evaluation run
unreproducible. Naming a SHA in this file would be self-referential, so read it off the
remote at the moment you pin:

```bash
git ls-remote https://github.com/aliyun-fc/mini-swe-agent fc/e2b-environment
pip install "mini-swe-agent[e2b] @ git+https://github.com/aliyun-fc/mini-swe-agent@<that sha>"
```

## When upstream merges #792

1. Switch the documentation's pin to the upstream release that contains it.
2. Re-propose our twelve deltas upstream as separate, individually reviewable PRs.
3. Retire this fork.
