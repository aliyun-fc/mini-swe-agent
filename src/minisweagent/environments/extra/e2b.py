"""E2B cloud sandbox environment implementation."""

from __future__ import annotations

import atexit
import hashlib
import json
import logging
import re
import shlex
import sys
import threading
import time
from typing import Any

from pydantic import BaseModel, Field

# Keep E2B SDK imports lazy: generic SWE-bench shutdown imports this module even
# when the optional E2B dependency is not installed.

#: Sandbox handles, rather than environments, are kept alive until a kill lands. This
#: leaves dropped environments collectable while preserving the only object that can retry
#: a failed kill. Every access is protected because worker cleanup and SIGTERM cleanup run
#: concurrently.
_active_sandboxes: set[Any] = set()
_killing_sandboxes: set[Any] = set()
_sandbox_registry = threading.Condition()
_creating_sandboxes = 0
_shutting_down = False
_CLEANUP_TIMEOUT = 20.0


def _begin_sandbox_creation() -> None:
    global _creating_sandboxes
    with _sandbox_registry:
        if _shutting_down:
            raise RuntimeError("Cannot create an E2B sandbox while shutdown is in progress")
        _creating_sandboxes += 1


def _finish_sandbox_creation(sandbox=None) -> None:
    global _creating_sandboxes
    with _sandbox_registry:
        if sandbox is not None:
            _active_sandboxes.add(sandbox)
        _creating_sandboxes -= 1
        _sandbox_registry.notify_all()


def _claim_sandbox(sandbox) -> bool:
    with _sandbox_registry:
        if sandbox in _killing_sandboxes:
            return False
        _active_sandboxes.discard(sandbox)
        _killing_sandboxes.add(sandbox)
        return True


def _finish_sandbox_kill(sandbox, success: bool) -> None:
    with _sandbox_registry:
        _killing_sandboxes.discard(sandbox)
        if not success:
            _active_sandboxes.add(sandbox)
        _sandbox_registry.notify_all()


def _kill_for_shutdown(sandbox, deadline: float) -> None:
    """Make one deadline-bound kill attempt, even if worker cleanup is already stuck."""
    owned = False
    with _sandbox_registry:
        if sandbox in _active_sandboxes:
            _active_sandboxes.remove(sandbox)
            _killing_sandboxes.add(sandbox)
            owned = True
        elif sandbox not in _killing_sandboxes:
            return
    if (remaining := deadline - time.monotonic()) <= 0:
        if owned:
            _finish_sandbox_kill(sandbox, False)
        return
    try:
        sandbox.kill(request_timeout=remaining)
    except Exception:
        if owned:
            _finish_sandbox_kill(sandbox, False)
    else:
        if owned:
            _finish_sandbox_kill(sandbox, True)


def shutdown_active_sandboxes(timeout: float = _CLEANUP_TIMEOUT) -> None:
    """Concurrently kill registered sandboxes within one process-wide deadline."""
    global _shutting_down
    deadline = time.monotonic() + timeout
    attempted: set[Any] = set()
    workers: list[threading.Thread] = []
    with _sandbox_registry:
        _shutting_down = True

    while True:
        with _sandbox_registry:
            sandboxes = list((_active_sandboxes | _killing_sandboxes) - attempted)
            attempted.update(sandboxes)
        for sandbox in sandboxes:
            worker = threading.Thread(
                target=_kill_for_shutdown,
                args=(sandbox, deadline),
                name=f"e2b-kill-{getattr(sandbox, 'sandbox_id', 'unknown')}",
                daemon=True,
            )
            workers.append(worker)
            worker.start()

        with _sandbox_registry:
            unclaimed = bool((_active_sandboxes | _killing_sandboxes) - attempted)
            if not unclaimed and _creating_sandboxes == 0:
                break
            if (remaining := deadline - time.monotonic()) <= 0:
                return
            _sandbox_registry.wait(remaining)

    for worker in workers:
        if (remaining := deadline - time.monotonic()) <= 0:
            return
        worker.join(remaining)


atexit.register(shutdown_active_sandboxes)


class E2BEnvironmentConfig(BaseModel):
    image: str
    """Docker image name to use as the E2B template base.
    Example: ``'swebench/sweb.eval.x86_64.django__django-11099:latest'``
    """
    cwd: str = "/"
    """Working directory in which to execute commands."""
    timeout: int = 30
    """Timeout for executing commands in the sandbox."""
    env: dict[str, str] = Field(default_factory=dict)
    """Environment variables to set when executing commands."""
    forward_env: list[str] = []
    """Environment variables to forward from the host into the sandbox.
    Variables are only forwarded if they are set in the host environment.
    In case of conflict with `env`, the `env` variables take precedence.
    """
    interpreter: list[str] = ["bash", "-c"]
    """Shell that actions are handed to, like :class:`~minisweagent.environments.docker.DockerEnvironment`.

    The e2b SDK already spawns commands via ``bash -l -c``, so this is one shell more
    than strictly needed -- but silently ignoring a configured interpreter would be
    worse, and ``config/benchmarks/swebench.yaml`` does configure one.
    """
    sandbox_timeout: int = 3600
    """How long (in seconds) the sandbox is allowed to stay alive."""
    run_as_user: str = "root"
    """User that commands are executed as.

    Defaults to ``root`` to match ``docker exec``: SWE-bench style images own the
    repository as root and usually ship without ``sudo``, so running as the
    template's default (unprivileged) user leaves the agent unable to edit files.
    Set to an empty string to use the template's default user.
    """
    require_existing_cwd: bool = False
    """Fail instead of warning when ``cwd`` has to be created.

    A working directory that is not already in the image means the repository is
    somewhere else, and nothing downstream notices: commands succeed in the empty
    directory, the agent runs to completion and submits an empty patch. Turn this on
    for repository-based benchmarks, where a created ``cwd`` is always a misconfiguration.
    """

    # Template build options (passed to Template.build())
    cpu_count: int = 2
    """Number of vCPUs allocated to the sandbox."""
    memory_mb: int = 2048
    """Memory allocated to the sandbox in MiB. Default is higher than E2B's 1024 MiB default
    to accommodate larger SWE-bench images."""
    skip_cache: bool = False
    """If True, force-rebuild the template even if it already exists."""
    tags: list[str] = Field(default_factory=list)
    """Optional tags to attach to the template."""
    build_timeout: int = 1800
    """Timeout for template builds in seconds (default 30 min to handle large images)."""

    # E2B connection (each falls back to the matching E2B_* environment variable)
    api_key: str | None = None
    """E2B API key. Falls back to the E2B_API_KEY environment variable."""
    domain: str | None = None
    """E2B domain. Falls back to E2B_DOMAIN, then to e2b.dev.

    Any E2B-compatible control plane works: set this (or E2B_DOMAIN) and the SDK
    derives the API URL as ``https://api.<domain>``. Set it here rather than through
    the environment when one process has to talk to more than one control plane.
    """
    api_url: str | None = None
    """E2B API URL. Falls back to E2B_API_URL, then to ``https://api.<domain>``."""

    # Private registry credentials (passed to Template().from_image())
    registry_username: str | None = None
    """Username for authenticating against a private Docker registry."""
    registry_password: str | None = None
    """Password for authenticating against a private Docker registry."""

    def api_params(self) -> dict[str, str]:
        """Connection kwargs for the e2b SDK. Unset fields are left to the SDK's own env defaults."""
        params = {"api_key": self.api_key, "domain": self.domain, "api_url": self.api_url}
        return {k: v for k, v in params.items() if v is not None}


#: Serialises builds per template name: without it, concurrent first builds of one image
#: all see ``Template.exists() is False`` and all build the same alias, so every loser
#: fails with ``409 ... resource conflict`` (measured: 4 threads → 3 losers, 8 → 7).
_build_locks: dict[str, threading.RLock] = {}
_build_locks_guard = threading.Lock()

#: Template names already force-rebuilt in this process, so that ``skip_cache`` rebuilds
#: once instead of once per instance sharing the image.
_force_rebuilt: set[tuple[str, str, str, str]] = set()


def _build_lock(template_name: str) -> threading.RLock:
    """Return the lock guarding builds of *template_name*.

    Reentrant because :meth:`E2BTemplateManager.get_or_build` delegates to
    :meth:`~E2BTemplateManager.rebuild`, which takes the same lock.
    """
    with _build_locks_guard:
        return _build_locks.setdefault(template_name, threading.RLock())


def _is_alias_conflict(e: Exception) -> bool:
    """Return True for the error a *losing* concurrent first build gets.

    Another builder claimed the alias first: ``409: template alias creation failed due
    to resource conflict``. The build is lost but the template itself is on its way, so
    the right response is to wait for it rather than to fail the run.
    """
    return re.match(r"\s*409\b", str(e)) is not None and "resource conflict" in str(e)


class E2BTemplateManager:
    """Converts Docker images to E2B templates and manages their lifecycle.

    Can be used independently of :class:`E2BEnvironment` for pre-building
    templates in batch scripts.
    """

    #: Config fields that change the built artifact and therefore the template identity.
    _BUILD_FIELDS = ("cpu_count", "memory_mb")

    #: How long to wait for an asynchronous template deletion to land. Sub-second in practice.
    _DELETE_TIMEOUT = 60

    def __init__(self, config: E2BEnvironmentConfig) -> None:
        self.config = config
        self.logger = logging.getLogger("minisweagent.environment.e2b")

    def template_name(self, docker_image: str) -> str:
        """Deterministically map a Docker image name to a valid E2B template name.

        A sha256 8-character suffix is appended to avoid collisions between images
        that produce the same sanitized prefix. The result is at most 63 characters
        and contains only lower-case alphanumerics and hyphens.

        The hash covers the build options as well as the image name. This matters
        because :meth:`get_or_build` short-circuits on ``Template.exists()``: if the
        name depended on the image alone, changing ``memory_mb`` or ``cpu_count``
        would silently reuse a template built with the *old* resource spec.

        Example::

            'swebench/sweb.eval.x86_64.django__django-11099:latest'
            → 'swebench-sweb-eval-x86-64-django--django-11099-l-a1b2c3d4'
        """
        identity = json.dumps(
            [docker_image, *(getattr(self.config, f) for f in self._BUILD_FIELDS)],
            sort_keys=True,
        )
        hash_suffix = hashlib.sha256(identity.encode()).hexdigest()[:8]
        name = re.sub(r"[^a-zA-Z0-9-]", "-", docker_image)
        name = re.sub(r"-{3,}", "--", name)
        name = name.lower()
        # Reserve 9 characters for "-" + 8-char hash suffix → prefix max 54 chars
        prefix = name[:54].strip("-")
        if not prefix:
            return hash_suffix
        return f"{prefix}-{hash_suffix}"

    def get_or_build(self, docker_image: str) -> str:
        """Return the E2B template name for *docker_image*, building it if needed.

        The existence check happens *inside* the per-name lock: checking outside it is
        the race itself, since every thread would then decide to build.
        """
        from e2b import Template

        template_name = self.template_name(docker_image)
        with _build_lock(template_name):
            if self.config.skip_cache and self._rebuild_key(template_name) not in _force_rebuilt:
                # Once per process: rebuilding on every construction would delete the
                # template out from under the other instances sharing this image. Record
                # it only after the rebuild lands, so a transient failure stays retryable.
                self.rebuild(docker_image)
                _force_rebuilt.add(self._rebuild_key(template_name))
            elif not Template.exists(template_name, **self.config.api_params()):
                self.logger.info(
                    "E2B template %s not found. Starting build (up to %d seconds)...",
                    template_name,
                    self.config.build_timeout,
                )
                self._build_template(docker_image, template_name)
                self.logger.info("E2B template %s built successfully.", template_name)
            else:
                self.logger.debug("E2B template %s already exists.", template_name)
        return template_name

    def _rebuild_key(self, template_name: str) -> tuple[str, str, str, str]:
        """Key for the once-per-process rebuild registry.

        The template name only hashes the image and its build parameters, so it repeats across
        accounts. Without the identity here, a process talking to two of them would rebuild the
        image on the first one only. The key is hashed rather than stored: this set lives at
        module scope and must not hold a credential.
        """
        from e2b import ConnectionConfig

        connection = ConnectionConfig(**self.config.api_params())
        account = hashlib.sha256(str(connection.api_key or "").encode()).hexdigest()[:12]
        return (connection.domain, connection.api_url, account, template_name)

    def rebuild(self, docker_image: str) -> str:
        """Force-rebuild the E2B template for *docker_image*.

        Deletes it first because the control plane allows a single build per template:
        building over an existing one fails with ``409: template build is not allowed in
        current status ...``.
        """
        template_name = self.template_name(docker_image)
        with _build_lock(template_name):
            self.logger.info("Rebuilding E2B template %s...", template_name)
            self._delete_template(template_name)
            self._build_template(docker_image, template_name)
            self.logger.info("E2B template %s rebuilt successfully.", template_name)
        return template_name

    def repair(self, docker_image: str) -> str:
        """Make the template for *docker_image* usable again, and return its name.

        Rebuilds only what is actually broken, all of it under the build lock and
        re-reading the status after acquiring it: without that, two callers that saw the
        same failed template would both rebuild, and the second would delete what the
        first had just built.

        A build owned by someone else is waited for rather than deleted -- deleting it
        would break that other runner.
        """
        from e2b.api.client.models import TemplateBuildStatus

        template_name = self.template_name(docker_image)
        with _build_lock(template_name):
            status = self.template_status(template_name)
            if status == TemplateBuildStatus.READY:
                return template_name
            if status in (TemplateBuildStatus.BUILDING, TemplateBuildStatus.WAITING):
                self.logger.info("E2B template %s is being built elsewhere. Waiting...", template_name)
                self.wait_until_ready(template_name)
                return template_name
            if status is None:
                self.logger.info("E2B template %s not found. Building...", template_name)
            else:
                self.logger.warning("Rebuilding unusable E2B template %s (build status: %s)...", template_name, status)
            self._delete_template(template_name)
            self._build_template(docker_image, template_name)
        return template_name

    def template_status(self, template_name: str):
        """Latest build status of *template_name*, or None if the template does not exist.

        Two round trips, because ``Template.exists()`` cannot answer this: it only
        resolves the alias, and the alias endpoint carries no status at all. That is why a
        template whose build failed still reports as existing.
        """
        if (template_id := self._template_id(template_name)) is None:
            return None
        return self._build_status(template_id)

    def wait_until_ready(self, template_name: str) -> None:
        """Block until *template_name* is usable, for when another builder owns it.

        The cross-process counterpart to the in-process build lock: a build started by a
        different process cannot be serialised, so a loser waits it out instead.
        """
        from e2b.api.client.models import TemplateBuildStatus

        if (template_id := self._template_id(template_name)) is None:
            msg = f"E2B template {template_name} disappeared while waiting for its build"
            raise RuntimeError(msg)
        deadline = time.monotonic() + self.config.build_timeout
        while (status := self._build_status(template_id)) in (
            TemplateBuildStatus.BUILDING,
            TemplateBuildStatus.WAITING,
        ):
            if time.monotonic() > deadline:
                msg = f"E2B template {template_name} still {status} after {self.config.build_timeout}s"
                raise TimeoutError(msg)
            time.sleep(1)
        if status != TemplateBuildStatus.READY:
            msg = f"E2B template {template_name} did not become usable (build status: {status})"
            raise RuntimeError(msg)

    def _api_client(self):
        """Low-level API client. The ``Template`` facade exposes no status or deletion."""
        from e2b.api.client_sync import get_api_client
        from e2b.connection_config import ConnectionConfig

        return get_api_client(ConnectionConfig(**self.config.api_params()))

    def _template_id(self, template_name: str) -> str | None:
        """Resolve an alias to a template id, or None if the alias does not exist.

        Only a 404 means "does not exist": treating every unreadable response that way
        (a 403 from someone else's template, a 5xx) would send :meth:`repair` off to
        delete and rebuild a template that is perfectly healthy.
        """
        from e2b.api import handle_api_exception
        from e2b.api.client.api.templates import get_templates_aliases_alias

        response = get_templates_aliases_alias.sync_detailed(alias=template_name, client=self._api_client())
        if response.status_code == 404:
            return None
        if response.status_code >= 300:
            raise handle_api_exception(response)
        return response.parsed.template_id

    def _build_status(self, template_id: str):
        """Status of the template's most recent build, or None if it has none."""
        from e2b.api import handle_api_exception
        from e2b.api.client.api.templates import get_templates_template_id

        response = get_templates_template_id.sync_detailed(template_id=template_id, client=self._api_client())
        if response.status_code >= 300:
            raise handle_api_exception(response)
        if not (builds := response.parsed.builds):
            return None
        return max(builds, key=lambda build: build.created_at).status

    def _delete_template(self, template_name: str) -> None:
        """Delete *template_name* if it exists, and wait for the deletion to land.

        Deletion is asynchronous: the API answers 204 while the template is still in
        ``DELETING``, and building inside that window fails with ``409: template build is
        not allowed in current status DELETING``. Convergence is sub-second in practice.
        """
        from e2b import Template
        from e2b.api import handle_api_exception
        from e2b.api.client.api.templates import delete_templates_template_id

        if (template_id := self._template_id(template_name)) is None:
            return
        response = delete_templates_template_id.sync_detailed(template_id=template_id, client=self._api_client())
        if response.status_code >= 300:
            raise handle_api_exception(response)
        deadline = time.monotonic() + self._DELETE_TIMEOUT
        while Template.exists(template_name, **self.config.api_params()):
            if time.monotonic() > deadline:
                msg = f"E2B template {template_name} still exists {self._DELETE_TIMEOUT}s after deletion"
                raise TimeoutError(msg)
            time.sleep(0.5)

    def _build_template(self, docker_image: str, template_name: str) -> None:
        """Build an E2B template from *docker_image*.

        Runs the build on a daemon thread rather than a pooled worker: ``build_timeout``
        has to bound the *process*, and a pooled worker cannot be cancelled once started
        -- the interpreter then joins it on exit, so a hung build delays shutdown by its
        full remaining duration. Abandoning a daemon thread is safe here because the
        template's real state lives on the control plane, where :meth:`repair` picks it up.
        """
        from e2b import Template

        template = Template().from_image(
            docker_image,
            username=self.config.registry_username,
            password=self.config.registry_password,
        )
        failure: list[BaseException] = []

        def _do_build() -> None:
            try:
                Template.build(
                    template,
                    template_name,
                    cpu_count=self.config.cpu_count,
                    memory_mb=self.config.memory_mb,
                    skip_cache=self.config.skip_cache,
                    tags=self.config.tags or None,
                    on_build_logs=self._on_build_log,
                    **self.config.api_params(),
                )
            except BaseException as e:  # noqa: BLE001 - re-raised on the calling thread
                failure.append(e)

        thread = threading.Thread(target=_do_build, name=f"e2b-build-{template_name}", daemon=True)
        thread.start()
        thread.join(timeout=self.config.build_timeout)
        if thread.is_alive():
            # No status query here: it is another network round trip past the deadline, which
            # would stop `build_timeout` from being a bound. `repair()` resolves the real state
            # when the caller retries.
            msg = f"E2B template build timed out after {self.config.build_timeout}s: {template_name}"
            raise TimeoutError(msg)
        if failure:
            if not _is_alias_conflict(failure[0]):
                raise failure[0]
            self.logger.info("E2B template %s is being built by someone else. Waiting...", template_name)
            self.wait_until_ready(template_name)

    def _on_build_log(self, entry) -> None:
        """Override to collect build logs: their tail is the only clue when an image cannot be pulled."""
        self.logger.debug("E2B build: %s", getattr(entry, "message", entry))


class E2BEnvironment:
    """Executes bash commands inside an E2B cloud sandbox.

    `E2B <https://e2b.dev>`_ provides isolated cloud sandboxes that can run
    arbitrary Docker images without requiring a local Docker daemon. This
    makes it suitable for large-scale, fully-remote SWE-bench evaluations.

    The first time a Docker image is used it is converted into a persistent
    E2B template; subsequent runs reuse the cached template.

    Any E2B-compatible control plane works: point the ``E2B_DOMAIN`` environment
    variable at it and the SDK derives the API URL as ``https://api.$E2B_DOMAIN``.

    See :class:`E2BEnvironmentConfig` for keyword arguments.
    """

    #: Config fields that must never leak into prompts or saved trajectories.
    _SECRET_FIELDS = {"api_key", "registry_password", "registry_username"}

    @staticmethod
    def _is_recoverable_template_error(e: Exception) -> bool:
        """Return True if *e* says the template is missing (404) or not usable yet (409).

        e2b surfaces API errors as ``"{status_code}: {message}"`` (see
        ``e2b.api.handle_api_exception``). Match the leading status code rather than a
        bare substring, which could appear inside a sandbox id or path and trigger a
        costly, unnecessary rebuild.

        409 covers both ``template is CREATE_FAILED`` and a build still in flight. The
        two need opposite responses, so :meth:`_recover_template` tells them apart -- but
        both are recoverable, whereas treating 409 as fatal (as matching only 404 does)
        leaves the image permanently unusable.
        """
        match = re.match(r"\s*(\d{3})\b", str(e))
        return match is not None and match.group(1) in ("404", "409")

    def __init__(
        self,
        *,
        config_class: type = E2BEnvironmentConfig,
        logger: logging.Logger | None = None,
        **kwargs: Any,
    ) -> None:
        from e2b.exceptions import SandboxException

        self.logger = logger or logging.getLogger("minisweagent.environment.e2b")
        self.config = config_class(**kwargs)
        self._cleanup_lock = threading.Lock()
        manager = E2BTemplateManager(self.config)
        self.template = self._resolve_template(manager)
        self.logger.info("Creating E2B sandbox (template: %s)...", self.template)
        try:
            self.sandbox = self._create_registered_sandbox()
        except SandboxException as e:
            if not self._is_recoverable_template_error(e):
                raise
            self._recover_template(manager)
            self.sandbox = self._create_registered_sandbox()
        self.logger.info("E2B sandbox ready (id: %s)", self.sandbox.sandbox_id)
        try:
            self._prepare_cwd()
        except Exception:
            self.cleanup()  # the sandbox is already running and billed by the second
            raise

    def _create_registered_sandbox(self):
        """Create a sandbox while making shutdown wait for and register the result."""
        _begin_sandbox_creation()
        sandbox = None
        try:
            sandbox = self._create_sandbox()
            return sandbox
        finally:
            _finish_sandbox_creation(sandbox)

    def _recover_template(self, manager: E2BTemplateManager) -> None:
        """Make :attr:`template` usable again after ``Sandbox.create`` refused it.

        A subclass may have resolved a pre-built template name instead of deriving one
        from ``image``; rebuilding would then build a *different* name while the sandbox
        keeps asking for this one, which looks like the repair did not work.
        """
        if not self.config.image or manager.template_name(self.config.image) != self.template:
            msg = f"Template {self.template} cannot be repaired, because it was not derived from `image`"
            raise RuntimeError(msg)
        manager.repair(self.config.image)

    def _resolve_template(self, manager: E2BTemplateManager) -> str:
        """Override to reuse an already-built template instead of resolving one from the image."""
        return manager.get_or_build(self.config.image)

    def _create_sandbox(self):
        """Override to pass extra Sandbox.create options (metadata, network, volumes, ...)."""
        from e2b import Sandbox

        return Sandbox.create(
            template=self.template,
            timeout=self.config.sandbox_timeout,
            **self.config.api_params(),
        )

    #: Echoed by the preparation command when ``cwd`` was not already in the image.
    _CWD_MISSING_MARKER = "__MSWEA_CWD_MISSING__"

    def _prepare_cwd(self) -> None:
        """Make ``cwd`` usable, mirroring what ``docker run -w`` gives us for free.

        Three things differ from a local container and all three are silent-failure traps:

        1. ``docker run -w`` creates the working directory; a sandbox does not. A
           missing ``cwd`` fails at process spawn with no exit code attached, so
           :meth:`execute` reports it as an infrastructure error on *every* step.
        2. When the repository was created by a different uid than the one we run
           as, git refuses to touch it (``detected dubious ownership``), which
           breaks ``git diff`` and therefore the whole submission.
        3. Creating the directory hides a misconfigured ``cwd`` completely: commands
           then succeed in an empty directory and the agent submits an empty patch. So
           report when the directory was not there to begin with, and let
           ``require_existing_cwd`` turn that into an error.
        """
        quoted = shlex.quote(self.config.cwd)
        # The check shares a command with mkdir but stays in front of it, so that mkdir
        # keeps supplying the exit code.
        try:
            result = self.sandbox.commands.run(
                f"test -d {quoted} || echo {self._CWD_MISSING_MARKER}; mkdir -p {quoted}",
                user=self.config.run_as_user or None,
                timeout=30,
            )
            stdout = result.stdout
        except Exception as e:
            # Not best-effort: without the working directory every later command fails
            # for a reason that has nothing to do with the task. An unprivileged
            # `run_as_user` that cannot create it must stop initialization here.
            msg = f"Could not create working directory {self.config.cwd}: {e}"
            raise RuntimeError(msg) from e

        # This one is best-effort: in a prepared evaluation image the repository is
        # usually already owned by the user we run as.
        try:
            self.sandbox.commands.run(
                f"git config --global --add safe.directory {quoted}",
                user=self.config.run_as_user or None,
                timeout=30,
            )
        except Exception as e:
            self.logger.warning("Marking %s as a safe git directory failed: %s", self.config.cwd, e)

        if self._CWD_MISSING_MARKER not in stdout:
            return
        message = (
            f"Working directory {self.config.cwd} was not in the image and has been created empty. "
            "For a repository-based task this means it is misconfigured: commands will succeed in "
            "the empty directory and the agent will submit an empty patch."
        )
        if self.config.require_existing_cwd:
            raise RuntimeError(message)
        self.logger.warning(message)

    def execute(self, action: dict, cwd: str = "", *, timeout: int | None = None) -> dict[str, Any]:
        """Execute a command in the sandbox and return the output."""
        import os

        from e2b.exceptions import TimeoutException

        command = action.get("command", "") if isinstance(action, dict) else action
        envs = {k: os.environ[k] for k in self.config.forward_env if k in os.environ}
        envs.update(self.config.env)  # `env` wins over `forward_env`, as in DockerEnvironment
        limit = timeout or self.config.timeout
        # Collected while the command streams, because the SDK's timeout carries no output.
        # Two buffers, stdout first: `_check_finished` only looks at the first line, and an
        # interleaved stderr chunk would land in front of the submission sentinel.
        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []
        try:
            result = self.sandbox.commands.run(
                shlex.join([*self.config.interpreter, command]) if self.config.interpreter else command,
                user=self.config.run_as_user or None,
                cwd=cwd or self.config.cwd,
                timeout=limit,
                envs=envs or None,
                on_stdout=stdout_chunks.append,
                on_stderr=stderr_chunks.append,
            )
            output: dict[str, Any] = {
                "output": result.stdout + result.stderr,
                "returncode": result.exit_code,
                "exception_info": "",
            }
        except Exception as e:
            # e2b raises ``CommandExitException`` (carrying stdout/stderr/exit_code)
            # for any non-zero exit. That is a normal command result, not an
            # infrastructure error, so surface the real output and exit code
            # instead of masking it as a generic failure.
            if (exit_code := getattr(e, "exit_code", None)) is not None:
                output = {
                    "output": getattr(e, "stdout", "") + getattr(e, "stderr", ""),
                    "returncode": exit_code,
                    # A negative exit code means the interpreter itself was killed by a
                    # signal, and the SDK reports -1 for every signal, so 128+N cannot be
                    # recovered. Leaving `exception_info` empty would claim this was an
                    # ordinary command result. (A killed *child* is unaffected: the shell
                    # survives and reports 137/143 as usual.)
                    "exception_info": (
                        ""
                        if exit_code >= 0
                        else "The process was terminated by a signal; the SDK does not report which one."
                    ),
                }
            else:
                output = {
                    "output": "".join(stdout_chunks) + "".join(stderr_chunks),
                    "returncode": -1,
                    "exception_info": (
                        f"The command timed out after {limit} seconds. Any output above is what it "
                        "had produced by then."
                        if isinstance(e, TimeoutException)
                        else f"An error occurred while executing the command: {e}"
                    ),
                    "extra": {"exception_type": type(e).__name__, "exception": str(e)},
                }
        self._check_finished(output)
        return output

    def _check_finished(self, output: dict) -> None:
        """Raise :class:`~minisweagent.exceptions.Submitted` when the task-submission marker is detected."""
        from minisweagent.exceptions import Submitted

        lines = output.get("output", "").lstrip().splitlines(keepends=True)
        if lines and lines[0].strip() == "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" and output["returncode"] == 0:
            submission = "".join(lines[1:])
            raise Submitted(
                {
                    "role": "exit",
                    "content": submission,
                    "extra": {"exit_status": "Submitted", "submission": submission},
                }
            )

    def get_template_vars(self, **kwargs: Any) -> dict[str, Any]:
        import platform

        from minisweagent.utils.serialize import recursive_merge

        config = self.config.model_dump(exclude=self._SECRET_FIELDS)
        return recursive_merge(config, platform.uname()._asdict(), kwargs)

    def serialize(self) -> dict:
        return {
            "info": {
                "config": {
                    "environment": self.config.model_dump(
                        mode="json",
                        exclude=self._SECRET_FIELDS,
                    ),
                    "environment_type": f"{self.__class__.__module__}.{self.__class__.__name__}",
                    # Without these, a trajectory cannot be traced back to the sandbox
                    # it ran in -- the only handle you have when debugging a batch run.
                    "sandbox_id": getattr(getattr(self, "sandbox", None), "sandbox_id", None),
                    "template": getattr(self, "template", None),
                }
            }
        }

    def cleanup(self) -> None:
        if not hasattr(self, "_cleanup_lock"):
            self._cleanup_lock = threading.Lock()
        with self._cleanup_lock:
            if getattr(self, "_cleaned_up", False):
                return
            sandbox = getattr(self, "sandbox", None)
            if sandbox is not None:
                if not _claim_sandbox(sandbox):
                    return
                try:
                    # True: killed. False: already gone. Both are terminal -- but an
                    # exception is not: the registered handle stays available for retry.
                    sandbox.kill()
                except Exception as e:
                    _finish_sandbox_kill(sandbox, False)
                    self.logger.warning("Killing E2B sandbox %s failed, will retry: %s", sandbox.sandbox_id, e)
                    return
                _finish_sandbox_kill(sandbox, True)
            self._cleaned_up = True
            # Keep `self.sandbox`: callers still read `sandbox_id` after cleanup to
            # reconcile a batch run against the control plane.

    def __del__(self) -> None:
        # Never issue a request while the interpreter is tearing down: the SDK's native
        # runtime is already gone by then and the process dies with SIGSEGV -- correct
        # output, exit code 139. `atexit` runs before that point and covers this case.
        if sys.is_finalizing():
            return
        self.cleanup()
