"""E2B cloud sandbox environment implementation."""

from __future__ import annotations

import atexit
import concurrent.futures
import hashlib
import json
import logging
import re
import shlex
from typing import Any

from pydantic import BaseModel, Field

# Module-level registry of live sandboxes for best-effort cleanup on exit
# (covers Ctrl+C and unhandled exceptions where __del__ may not be called).
_active_sandboxes: set[E2BEnvironment] = set()


def _cleanup_all_sandboxes() -> None:
    """Kill all sandboxes that are still alive at interpreter shutdown."""
    for env in list(_active_sandboxes):
        env.cleanup()


atexit.register(_cleanup_all_sandboxes)


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

    # E2B authentication (can also be set via the E2B_API_KEY env var)
    api_key: str | None = None
    """E2B API key. Falls back to the E2B_API_KEY environment variable."""

    # Private registry credentials (passed to Template().from_image())
    registry_username: str | None = None
    """Username for authenticating against a private Docker registry."""
    registry_password: str | None = None
    """Password for authenticating against a private Docker registry."""


class E2BTemplateManager:
    """Converts Docker images to E2B templates and manages their lifecycle.

    Can be used independently of :class:`E2BEnvironment` for pre-building
    templates in batch scripts.
    """

    #: Config fields that change the built artifact and therefore the template identity.
    _BUILD_FIELDS = ("cpu_count", "memory_mb")

    def __init__(self, config: E2BEnvironmentConfig) -> None:
        self.config = config
        self.logger = logging.getLogger("minisweagent.environment.e2b")

    def _template_name(self, docker_image: str) -> str:
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
        """Return the E2B template name for *docker_image*, building it if needed."""
        from e2b import Template

        template_name = self._template_name(docker_image)
        if not Template.exists(template_name, api_key=self.config.api_key) or self.config.skip_cache:
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

    def rebuild(self, docker_image: str) -> str:
        """Force-rebuild the E2B template for *docker_image*."""
        template_name = self._template_name(docker_image)
        self.logger.info("Rebuilding E2B template %s...", template_name)
        self._build_template(docker_image, template_name)
        self.logger.info("E2B template %s rebuilt successfully.", template_name)
        return template_name

    def _build_template(self, docker_image: str, template_name: str) -> None:
        """Build an E2B template from *docker_image*.

        Uses :class:`concurrent.futures.ThreadPoolExecutor` for timeout
        enforcement because ``signal.alarm`` only works on the main thread
        and this method may be called from worker threads.
        """
        from e2b import Template

        template = Template().from_image(
            docker_image,
            username=self.config.registry_username,
            password=self.config.registry_password,
        )

        def _do_build() -> None:
            Template.build(
                template,
                template_name,
                cpu_count=self.config.cpu_count,
                memory_mb=self.config.memory_mb,
                skip_cache=self.config.skip_cache,
                tags=self.config.tags or None,
                api_key=self.config.api_key,
            )

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(_do_build)
        try:
            future.result(timeout=self.config.build_timeout)
        except concurrent.futures.TimeoutError as e:
            executor.shutdown(wait=False, cancel_futures=True)
            msg = f"E2B template build timed out after {self.config.build_timeout}s: {template_name}"
            raise TimeoutError(msg) from e
        except Exception:
            executor.shutdown(wait=False, cancel_futures=True)
            raise
        else:
            executor.shutdown(wait=True)


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
    def _is_stale_template_error(e: Exception) -> bool:
        """Return True if *e* is a 'template not found' (HTTP 404) error.

        e2b surfaces a missing template as a ``SandboxException`` whose message is
        formatted as ``"{status_code}: {message}"`` (see ``e2b.api.handle_api_exception``).
        Match the leading 404 status code rather than a bare ``"404"`` substring,
        which could appear inside a sandbox id or path and trigger a costly,
        unnecessary template rebuild.
        """
        match = re.match(r"\s*(\d{3})\b", str(e))
        return match is not None and match.group(1) == "404"

    def __init__(self, **kwargs: Any) -> None:
        from e2b import Sandbox
        from e2b.exceptions import SandboxException

        self.logger = logging.getLogger("minisweagent.environment.e2b")
        self.config = E2BEnvironmentConfig(**kwargs)
        manager = E2BTemplateManager(self.config)
        self.template = manager.get_or_build(self.config.image)
        self.logger.info("Creating E2B sandbox (template: %s)...", self.template)
        try:
            self.sandbox = Sandbox.create(
                template=self.template,
                timeout=self.config.sandbox_timeout,
                api_key=self.config.api_key,
            )
        except SandboxException as e:
            if not self._is_stale_template_error(e):
                raise
            self.logger.warning("Template %s not found (stale cache). Rebuilding...", self.template)
            manager.rebuild(self.config.image)
            self.sandbox = Sandbox.create(
                template=self.template,
                timeout=self.config.sandbox_timeout,
                api_key=self.config.api_key,
            )
        self.logger.info("E2B sandbox ready (id: %s)", self.sandbox.sandbox_id)
        _active_sandboxes.add(self)
        self._prepare_cwd()

    def _prepare_cwd(self) -> None:
        """Make ``cwd`` usable, mirroring what ``docker run -w`` gives us for free.

        Two things differ from a local container and both are silent-failure traps:

        1. ``docker run -w`` creates the working directory; a sandbox does not. A
           missing ``cwd`` fails at process spawn with no exit code attached, so
           :meth:`execute` reports it as an infrastructure error on *every* step.
        2. When the repository was created by a different uid than the one we run
           as, git refuses to touch it (``detected dubious ownership``), which
           breaks ``git diff`` and therefore the whole submission.

        Both commands are best-effort: in a prepared evaluation image the directory
        already exists and may not be writable by an unprivileged user.
        """
        for command in (
            f"mkdir -p {shlex.quote(self.config.cwd)}",
            f"git config --global --add safe.directory {shlex.quote(self.config.cwd)}",
        ):
            try:
                self.sandbox.commands.run(command, user=self.config.run_as_user or None, timeout=30)
            except Exception as e:
                self.logger.warning("Preparing cwd with %r failed: %s", command, e)

    def execute(self, action: dict, cwd: str = "", *, timeout: int | None = None) -> dict[str, Any]:
        """Execute a command in the sandbox and return the output."""
        command = action.get("command", "") if isinstance(action, dict) else action
        try:
            result = self.sandbox.commands.run(
                shlex.join([*self.config.interpreter, command]) if self.config.interpreter else command,
                user=self.config.run_as_user or None,
                cwd=cwd or self.config.cwd,
                timeout=timeout or self.config.timeout,
                envs=self.config.env or None,
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
                    "exception_info": "",
                }
            else:
                output = {
                    "output": "",
                    "returncode": -1,
                    "exception_info": f"An error occurred while executing the command: {e}",
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
        _active_sandboxes.discard(self)
        sandbox = getattr(self, "sandbox", None)
        if sandbox is not None:
            try:
                sandbox.kill()
            except Exception:
                pass

    def __del__(self) -> None:
        self.cleanup()
