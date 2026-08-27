"""Tests for the E2B cloud sandbox environment."""

import gc
import re
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from e2b.api.client.models import TemplateBuildStatus
from e2b.exceptions import TimeoutException

from minisweagent.environments.extra.e2b import (
    E2BEnvironment,
    E2BEnvironmentConfig,
    E2BTemplateManager,
)
from minisweagent.exceptions import Submitted


def _make_env(**kwargs) -> E2BEnvironment:
    """Create an E2BEnvironment without touching real E2B infrastructure."""
    with patch.object(E2BEnvironment, "__init__", lambda self, **kw: None):
        env = E2BEnvironment()
        env.config = E2BEnvironmentConfig(image="swebench/test-image:latest", **kwargs)
        env.sandbox = MagicMock()
        env.sandbox.sandbox_id = "sbx-test"
        env.template = "swebench-test-image-latest-deadbeef"
        env.logger = MagicMock()
        return env


def _make_manager(**kwargs) -> E2BTemplateManager:
    return E2BTemplateManager(E2BEnvironmentConfig(image="swebench/test-image:latest", **kwargs))


class TestE2BEnvironmentConfig:
    def test_defaults(self):
        cfg = E2BEnvironmentConfig(image="python:3.11")
        assert (cfg.cwd, cfg.timeout, cfg.sandbox_timeout, cfg.run_as_user) == ("/", 30, 3600, "root")
        assert (cfg.cpu_count, cfg.memory_mb, cfg.build_timeout) == (2, 2048, 1800)
        assert (cfg.skip_cache, cfg.tags) == (False, [])
        assert (cfg.api_key, cfg.registry_username, cfg.registry_password) == (None, None, None)

    def test_custom_values(self):
        cfg = E2BEnvironmentConfig(image="my-image:tag", sandbox_timeout=7200, cpu_count=4)
        assert (cfg.sandbox_timeout, cfg.cpu_count) == (7200, 4)


class TestApiParams:
    def test_unset_fields_are_omitted(self):
        # Anything we pass explicitly overrides the SDK's env-var default, so an
        # unset field must not reach the SDK as None.
        assert _make_env().config.api_params() == {}

    def test_passes_through_what_is_set(self):
        env = _make_env(api_key="k", domain="example.com")
        assert env.config.api_params() == {"api_key": "k", "domain": "example.com"}


class TestTemplateName:
    def test_basic_sanitization(self):
        assert re.match(r"^[a-z0-9-]+$", _make_manager().template_name("python:3.11"))

    def test_length_limit(self):
        assert len(_make_manager().template_name("a" * 100 + ":latest")) <= 63

    def test_deterministic(self):
        image = "swebench/sweb.eval.x86_64.django__django-11099:latest"
        assert _make_manager().template_name(image) == _make_manager().template_name(image)

    def test_different_images_different_names(self):
        assert _make_manager().template_name("image-a:latest") != _make_manager().template_name("image-b:latest")

    def test_no_triple_hyphens(self):
        # Dots and slashes become hyphens; consecutive runs are collapsed to "--"
        assert "---" not in _make_manager().template_name("a/b/c.d.e:latest")

    def test_empty_prefix_falls_back_to_hash(self):
        assert len(_make_manager().template_name("---")) == 8

    @pytest.mark.parametrize(("field", "value"), [("memory_mb", 8192), ("cpu_count", 4)])
    def test_build_options_change_the_name(self, field, value):
        # get_or_build() short-circuits on Template.exists(), so a name that ignored
        # the resource spec would silently hand back a template built with the old one.
        image = "swebench/test-image:latest"
        assert _make_manager().template_name(image) != _make_manager(**{field: value}).template_name(image)


class TestGetOrBuild:
    def test_concurrent_first_build_builds_once(self):
        # Without the per-name lock every thread sees exists() == False and builds the
        # same alias: one wins, the losers get "409 ... resource conflict" and each leaves
        # an orphan template record behind (measured: 4 threads → 3 losers, 8 → 7).
        manager = _make_manager()
        built: list[str] = []
        start = threading.Barrier(8)

        def slow_build(docker_image, template_name):
            # A real build takes seconds; an instant one would hide the race, because the
            # first thread would finish before the others get to their existence check.
            time.sleep(0.05)
            built.append(template_name)

        def enter_and_build():
            start.wait()
            manager.get_or_build("swebench/test-image:latest")

        with (
            patch("e2b.Template.exists", side_effect=lambda name, **kw: name in built),
            patch.object(E2BTemplateManager, "_build_template", side_effect=slow_build),
        ):
            threads = [threading.Thread(target=enter_and_build) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        assert built == [manager.template_name("swebench/test-image:latest")]

    def test_rebuild_deletes_first(self):
        # The control plane allows a single build per template, so building over an
        # existing one fails with "409: template build is not allowed in current status".
        calls = []
        with (
            patch.object(E2BTemplateManager, "_delete_template", side_effect=lambda name: calls.append("delete")),
            patch.object(E2BTemplateManager, "_build_template", side_effect=lambda image, name: calls.append("build")),
        ):
            _make_manager().rebuild("swebench/test-image:latest")
        assert calls == ["delete", "build"]

    def test_failed_forced_rebuild_stays_retryable(self):
        # Recording the rebuild before it lands would make one transient failure
        # permanent: every later instance would skip the rebuild and use the stale template.
        from minisweagent.environments.extra import e2b as e2b_mod

        manager = _make_manager(skip_cache=True)
        attempts = []

        def flaky_rebuild(docker_image):
            attempts.append(docker_image)
            if len(attempts) == 1:
                raise RuntimeError("503")

        e2b_mod._force_rebuilt.clear()
        with patch.object(E2BTemplateManager, "rebuild", side_effect=flaky_rebuild):
            with pytest.raises(RuntimeError, match="503"):
                manager.get_or_build("swebench/test-image:latest")
            manager.get_or_build("swebench/test-image:latest")
        assert len(attempts) == 2
        e2b_mod._force_rebuilt.clear()

    def test_forced_rebuild_is_tracked_per_control_plane(self):
        # The template name only hashes the image and its build parameters, so it repeats
        # across control planes: one shared key would rebuild on the first domain only.
        from minisweagent.environments.extra import e2b as e2b_mod

        rebuilt = []
        e2b_mod._force_rebuilt.clear()
        with patch.object(E2BTemplateManager, "rebuild", side_effect=lambda image: rebuilt.append(image)):
            for domain in ("one.example.com", "two.example.com"):
                _make_manager(skip_cache=True, domain=domain).get_or_build("swebench/test-image:latest")
        assert len(rebuilt) == 2
        e2b_mod._force_rebuilt.clear()


class TestRepair:
    def _manager(self, status, calls):
        manager = _make_manager()
        manager.template_status = lambda name: status
        manager._delete_template = lambda name: calls.append("delete")
        manager._build_template = lambda image, name: calls.append("build")
        manager.wait_until_ready = lambda name: calls.append("wait")
        return manager

    def test_ready_template_is_left_alone(self):
        # rebuild() deletes unconditionally, so this re-check under the lock is what stops
        # a second caller from deleting the template the first one has just repaired.
        calls = []
        self._manager(TemplateBuildStatus.READY, calls).repair("swebench/test-image:latest")
        assert calls == []

    @pytest.mark.parametrize("status", [TemplateBuildStatus.BUILDING, TemplateBuildStatus.WAITING])
    def test_build_in_flight_is_waited_for(self, status):
        # Deleting a build somebody else started would break that other runner.
        calls = []
        self._manager(status, calls).repair("swebench/test-image:latest")
        assert calls == ["wait"]

    @pytest.mark.parametrize("status", [TemplateBuildStatus.ERROR, None])
    def test_broken_or_missing_template_is_rebuilt(self, status):
        calls = []
        self._manager(status, calls).repair("swebench/test-image:latest")
        assert calls == ["delete", "build"]


class TestIsRecoverableTemplateError:
    @pytest.mark.parametrize(
        "message",
        [
            "404: template foo not found",
            "409: cannot create sandbox: template is CREATE_FAILED, only READY templates can be used",
        ],
    )
    def test_recoverable_statuses_match(self, message):
        # e2b formats API errors as "{status_code}: {message}". 409 must be recoverable:
        # treating it as fatal is what leaves a failed template permanently unusable.
        assert E2BEnvironment._is_recoverable_template_error(Exception(message)) is True

    @pytest.mark.parametrize("message", ["500: internal error", "429: rate limited"])
    def test_other_status_does_not_match(self, message):
        assert E2BEnvironment._is_recoverable_template_error(Exception(message)) is False

    @pytest.mark.parametrize("message", ["Sandbox abc404def failed", "error in /path/404/x"])
    def test_incidental_404_substring_does_not_match(self, message):
        # "404" appearing inside an id/path must not trigger a costly rebuild.
        assert E2BEnvironment._is_recoverable_template_error(Exception(message)) is False


class TestPrepareCwd:
    def _mock_run(self, env, stdout=""):
        result = MagicMock()
        result.stdout = stdout
        env.sandbox.commands.run.return_value = result
        return result

    def test_creates_cwd_and_marks_it_safe_for_git(self):
        # `docker run -w` creates the working directory and a sandbox does not; a
        # missing cwd fails at process spawn with no exit code, so execute() would
        # report an infrastructure error on every single step.
        env = _make_env(cwd="/testbed")
        self._mock_run(env)
        env._prepare_cwd()

        commands = [call.args[0] for call in env.sandbox.commands.run.call_args_list]
        assert commands == [
            f"test -d /testbed || echo {E2BEnvironment._CWD_MISSING_MARKER}; mkdir -p /testbed",
            "git config --global --add safe.directory /testbed",
        ]
        assert all(call.kwargs["user"] == "root" for call in env.sandbox.commands.run.call_args_list)

    def test_quotes_cwd(self):
        env = _make_env(cwd="/two words")
        self._mock_run(env)
        env._prepare_cwd()
        assert "mkdir -p '/two words'" in env.sandbox.commands.run.call_args_list[0].args[0]

    def test_unusable_cwd_aborts(self):
        # Not best-effort: if the directory cannot be created, every later command fails
        # for a reason unrelated to the task, so initialization must stop here.
        env = _make_env(cwd="/testbed")
        env.sandbox.commands.run.side_effect = RuntimeError("permission denied")
        with pytest.raises(RuntimeError, match="Could not create working directory"):
            env._prepare_cwd()

    def test_git_config_failure_is_tolerated(self):
        # In a prepared evaluation image the repository is usually already owned by the
        # user we run as, so this one only matters when it is not -- warn and continue.
        env = _make_env(cwd="/testbed")
        self._mock_run(env)
        env.sandbox.commands.run.side_effect = [env.sandbox.commands.run.return_value, RuntimeError("no git")]
        env._prepare_cwd()
        assert "safe git directory" in env.logger.warning.call_args.args[0]

    def test_existing_cwd_is_not_reported(self):
        env = _make_env(cwd="/testbed")
        self._mock_run(env)
        env._prepare_cwd()
        assert env.logger.warning.call_count == 0

    def test_created_cwd_is_reported(self):
        # A cwd that is not in the image means the repository is elsewhere, and nothing
        # downstream notices: the agent works in an empty directory and submits nothing.
        env = _make_env(cwd="/testbed")
        self._mock_run(env, stdout=f"{E2BEnvironment._CWD_MISSING_MARKER}\n")
        env._prepare_cwd()
        assert "created empty" in env.logger.warning.call_args.args[0]

    def test_require_existing_cwd_fails_loudly(self):
        env = _make_env(cwd="/testbed", require_existing_cwd=True)
        self._mock_run(env, stdout=f"{E2BEnvironment._CWD_MISSING_MARKER}\n")
        with pytest.raises(RuntimeError, match="created empty"):
            env._prepare_cwd()


class TestE2BEnvironmentExecute:
    def _mock_result(self, env, stdout="hello\n", stderr="", exit_code=0):
        result = MagicMock()
        result.stdout, result.stderr, result.exit_code = stdout, stderr, exit_code
        env.sandbox.commands.run.return_value = result
        return result

    def test_execute_dict_action(self):
        env = _make_env()
        self._mock_result(env)
        assert env.execute({"command": "echo hello"}) == {
            "output": "hello\n",
            "returncode": 0,
            "exception_info": "",
        }

    def test_execute_string_action(self):
        env = _make_env()
        self._mock_result(env, stdout="ok\n")
        assert env.execute("echo ok")["output"] == "ok\n"

    def test_execute_hands_action_to_the_interpreter(self):
        # swebench.yaml configures `interpreter`, so ignoring it would silently run
        # actions under a shell the user did not ask for.
        env = _make_env()
        self._mock_result(env)
        env.execute({"command": "echo 'hi there'"})
        assert env.sandbox.commands.run.call_args.args[0] == """bash -c 'echo '"'"'hi there'"'"''"""

    def test_empty_interpreter_runs_command_directly(self):
        env = _make_env(interpreter=[])
        self._mock_result(env)
        env.execute({"command": "echo hi"})
        assert env.sandbox.commands.run.call_args.args[0] == "echo hi"

    def test_execute_passes_user_and_cwd(self):
        env = _make_env(cwd="/testbed", run_as_user="worker", timeout=90)
        self._mock_result(env)
        env.execute({"command": "ls"})
        assert env.sandbox.commands.run.call_args.kwargs["user"] == "worker"
        assert env.sandbox.commands.run.call_args.kwargs["cwd"] == "/testbed"
        assert env.sandbox.commands.run.call_args.kwargs["timeout"] == 90

    def test_forwards_host_env_but_env_wins(self, monkeypatch):
        monkeypatch.setenv("FORWARDED", "from-host")
        monkeypatch.setenv("SHADOWED", "from-host")
        env = _make_env(forward_env=["FORWARDED", "SHADOWED", "NOT_SET_ON_HOST"], env={"SHADOWED": "from-config"})
        self._mock_result(env)
        env.execute({"command": "env"})
        assert env.sandbox.commands.run.call_args.kwargs["envs"] == {
            "FORWARDED": "from-host",
            "SHADOWED": "from-config",
        }

    def test_empty_run_as_user_defers_to_template_default(self):
        env = _make_env(run_as_user="")
        self._mock_result(env)
        env.execute({"command": "ls"})
        assert env.sandbox.commands.run.call_args.kwargs["user"] is None

    def test_execute_nonzero_exit(self):
        # e2b's commands.run() RAISES CommandExitException (carrying stdout/stderr/
        # exit_code) on any non-zero exit. A failing command is a normal result,
        # not an infrastructure error: its real output and exit code must survive.
        env = _make_env()
        exc = Exception("Command exited with code 1")
        exc.stdout, exc.stderr, exc.exit_code = "partial stdout\n", "boom\n", 1
        env.sandbox.commands.run.side_effect = exc

        output = env.execute({"command": "false"})

        assert output["returncode"] == 1
        assert output["output"] == "partial stdout\nboom\n"
        assert output["exception_info"] == ""

    def test_signal_terminated_process_is_labelled(self):
        # The SDK reports -1 for any signal, and -1 is also what this class uses for an
        # infrastructure error. An empty exception_info would claim this was an ordinary
        # command result -- the exit code stays as it is, but it has to be explained.
        env = _make_env()
        exc = Exception("Command exited with code -1")
        exc.stdout, exc.stderr, exc.exit_code = "", "", -1
        env.sandbox.commands.run.side_effect = exc

        output = env.execute({"command": "kill -9 $$"})

        assert output["returncode"] == -1
        assert "terminated by a signal" in output["exception_info"]

    def test_execute_exception(self):
        env = _make_env()
        env.sandbox.commands.run.side_effect = RuntimeError("connection lost")

        output = env.execute({"command": "ls"})

        assert output["returncode"] == -1
        assert "connection lost" in output["exception_info"]
        assert output["extra"]["exception_type"] == "RuntimeError"

    def test_timeout_keeps_what_the_command_already_printed(self):
        # The SDK's timeout carries no stdout/stderr at all, so the streaming callbacks are
        # the only way to keep the output of a command that ran out of time.
        env = _make_env()

        def times_out(cmd, **kwargs):
            kwargs["on_stdout"]("collected 42 tests\n")
            raise TimeoutException("command timed out")

        env.sandbox.commands.run.side_effect = times_out

        output = env.execute({"command": "pytest --collect-only"}, timeout=3)

        assert output["output"] == "collected 42 tests\n"
        assert output["returncode"] == -1
        assert "timed out after 3 seconds" in output["exception_info"]

    def test_stderr_noise_does_not_shadow_the_submission_sentinel(self):
        # bash writes its warnings before the command produces anything, so merging the two
        # streams in arrival order would put noise on the first line and silently break the
        # submission protocol. stdout has to stay in front of stderr.
        env = _make_env()
        submission = "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\ndiff --git a/f.py b/f.py\n"

        def noisy(cmd, **kwargs):
            kwargs["on_stderr"]("bash: /root/.bashrc: Permission denied\n")
            kwargs["on_stdout"](submission)
            return self._mock_result(env, stdout=submission, stderr="bash: /root/.bashrc: Permission denied\n")

        env.sandbox.commands.run.side_effect = noisy

        with pytest.raises(Submitted) as exc_info:
            env.execute({"command": "submit"})

        assert exc_info.value.messages[0]["extra"]["submission"].startswith("diff --git")

    def test_execute_raises_submitted(self):
        env = _make_env()
        self._mock_result(env, stdout="COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\ndiff --git a/f.py b/f.py\n")

        with pytest.raises(Submitted) as exc_info:
            env.execute({"command": "submit"})

        msg = exc_info.value.messages[0]
        assert msg["extra"]["exit_status"] == "Submitted"
        assert "diff --git" in msg["extra"]["submission"]

    def test_sentinel_with_nonzero_exit_is_not_a_submission(self):
        env = _make_env()
        self._mock_result(env, stdout="COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\npatch\n", exit_code=1)
        assert env.execute({"command": "submit"})["returncode"] == 1


class TestE2BEnvironmentTemplateVars:
    def test_includes_platform_uname(self):
        # Default configs (mini/default) render {{system}}/{{machine}}/... under
        # Jinja StrictUndefined, so these keys must be present like docker/local.
        result = _make_env().get_template_vars()
        assert {"system", "release", "version", "machine", "node", "processor"} <= set(result)

    def test_excludes_credentials(self):
        # Template vars feed the Jinja prompt context; secrets must not leak there.
        env = _make_env(api_key="secret-key", registry_password="secret-pass", registry_username="user")
        assert not {"api_key", "registry_password", "registry_username"} & set(env.get_template_vars())

    def test_kwargs_override(self):
        assert _make_env().get_template_vars(extra="value")["extra"] == "value"


class TestE2BEnvironmentSerialize:
    def test_serialize_structure(self):
        config = _make_env().serialize()["info"]["config"]
        assert "E2BEnvironment" in config["environment_type"]
        assert config["environment"]["image"] == "swebench/test-image:latest"

    def test_serialize_records_sandbox_identity(self):
        # A trajectory that cannot be traced back to its sandbox is unusable when
        # debugging a batch run.
        config = _make_env().serialize()["info"]["config"]
        assert (config["sandbox_id"], config["template"]) == ("sbx-test", "swebench-test-image-latest-deadbeef")

    def test_serialize_survives_failed_init(self):
        with patch.object(E2BEnvironment, "__init__", lambda self, **kw: None):
            env = E2BEnvironment()
            env.config = E2BEnvironmentConfig(image="python:3.11")
            assert env.serialize()["info"]["config"]["sandbox_id"] is None

    def test_serialize_excludes_credentials(self):
        env = _make_env(api_key="secret-key", registry_password="secret-pass")
        assert not {"api_key", "registry_password"} & set(env.serialize()["info"]["config"]["environment"])


class TestE2BEnvironmentCleanup:
    def test_cleanup_kills_sandbox(self):
        env = _make_env()
        env.cleanup()
        env.sandbox.kill.assert_called_once()

    def test_cleanup_tolerates_missing_sandbox(self):
        with patch.object(E2BEnvironment, "__init__", lambda self, **kw: None):
            E2BEnvironment().cleanup()  # sandbox was never set; must not raise

    def test_cleanup_tolerates_kill_exception(self):
        env = _make_env()
        env.sandbox.kill.side_effect = RuntimeError("already dead")
        env.cleanup()  # must not raise

    def test_cleanup_kills_only_once(self):
        # `__del__` calls cleanup() again after an explicit one. Without this guard the
        # second call issues another kill request, and when the object survives until
        # interpreter shutdown -- as it does in any module-level script -- that request
        # crashes the SDK's native runtime: correct output, exit code 139.
        env = _make_env()
        env.cleanup()
        env.cleanup()
        env.sandbox.kill.assert_called_once()
        assert env.sandbox.sandbox_id  # batch reconciliation reads this after cleanup

    def test_cleanup_retries_after_a_failed_kill(self):
        # Recording completion before the kill lands would let one transient API error
        # keep the sandbox billed until its TTL: neither atexit nor __del__ would retry.
        from minisweagent.environments.extra import e2b as e2b_mod

        env = _make_env()
        e2b_mod._active_sandboxes.add(env)
        env.sandbox.kill.side_effect = [RuntimeError("503"), None]

        env.cleanup()
        assert env in e2b_mod._active_sandboxes

        env.cleanup()
        assert env.sandbox.kill.call_count == 2
        assert env not in e2b_mod._active_sandboxes


class TestAtexitCleanup:
    def test_cleanup_removes_from_active_sandboxes(self):
        from minisweagent.environments.extra import e2b as e2b_mod

        env = _make_env()
        e2b_mod._active_sandboxes.add(env)
        env.cleanup()
        assert env not in e2b_mod._active_sandboxes

    def test_cleanup_all_sandboxes_kills_all(self):
        from minisweagent.environments.extra import e2b as e2b_mod

        env1, env2 = _make_env(), _make_env()
        e2b_mod._active_sandboxes.update([env1, env2])

        e2b_mod._cleanup_all_sandboxes()

        env1.sandbox.kill.assert_called_once()
        env2.sandbox.kill.assert_called_once()
        assert env1 not in e2b_mod._active_sandboxes and env2 not in e2b_mod._active_sandboxes

    def test_dropped_environment_kills_its_sandbox(self):
        # The registry must not keep the environment alive: a strong one holds every
        # reference count above zero, which makes __del__ unreachable and leaves the
        # sandbox running -- and billed -- until the process ends. The tests above do not
        # catch that, because they all release the sandbox explicitly.
        from minisweagent.environments.extra import e2b as e2b_mod

        env = _make_env()
        sandbox = env.sandbox
        e2b_mod._active_sandboxes.add(env)

        del env
        gc.collect()

        sandbox.kill.assert_called_once()
