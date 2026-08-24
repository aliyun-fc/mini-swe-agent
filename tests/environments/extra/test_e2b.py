"""Tests for the E2B cloud sandbox environment."""

import re
from unittest.mock import MagicMock, patch

import pytest

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
        assert re.match(r"^[a-z0-9-]+$", _make_manager()._template_name("python:3.11"))

    def test_length_limit(self):
        assert len(_make_manager()._template_name("a" * 100 + ":latest")) <= 63

    def test_deterministic(self):
        image = "swebench/sweb.eval.x86_64.django__django-11099:latest"
        assert _make_manager()._template_name(image) == _make_manager()._template_name(image)

    def test_different_images_different_names(self):
        assert _make_manager()._template_name("image-a:latest") != _make_manager()._template_name("image-b:latest")

    def test_no_triple_hyphens(self):
        # Dots and slashes become hyphens; consecutive runs are collapsed to "--"
        assert "---" not in _make_manager()._template_name("a/b/c.d.e:latest")

    def test_empty_prefix_falls_back_to_hash(self):
        assert len(_make_manager()._template_name("---")) == 8

    @pytest.mark.parametrize(("field", "value"), [("memory_mb", 8192), ("cpu_count", 4)])
    def test_build_options_change_the_name(self, field, value):
        # get_or_build() short-circuits on Template.exists(), so a name that ignored
        # the resource spec would silently hand back a template built with the old one.
        image = "swebench/test-image:latest"
        assert _make_manager()._template_name(image) != _make_manager(**{field: value})._template_name(image)


class TestIsStaleTemplateError:
    def test_404_status_prefix_matches(self):
        # e2b formats API errors as "{status_code}: {message}".
        assert E2BEnvironment._is_stale_template_error(Exception("404: template foo not found")) is True

    @pytest.mark.parametrize("message", ["500: internal error", "429: rate limited"])
    def test_other_status_does_not_match(self, message):
        assert E2BEnvironment._is_stale_template_error(Exception(message)) is False

    @pytest.mark.parametrize("message", ["Sandbox abc404def failed", "error in /path/404/x"])
    def test_incidental_404_substring_does_not_match(self, message):
        # "404" appearing inside an id/path must not trigger a costly rebuild.
        assert E2BEnvironment._is_stale_template_error(Exception(message)) is False


class TestPrepareCwd:
    def test_creates_cwd_and_marks_it_safe_for_git(self):
        # `docker run -w` creates the working directory and a sandbox does not; a
        # missing cwd fails at process spawn with no exit code, so execute() would
        # report an infrastructure error on every single step.
        env = _make_env(cwd="/testbed")
        env._prepare_cwd()

        commands = [call.args[0] for call in env.sandbox.commands.run.call_args_list]
        assert commands == ["mkdir -p /testbed", "git config --global --add safe.directory /testbed"]
        assert all(call.kwargs["user"] == "root" for call in env.sandbox.commands.run.call_args_list)

    def test_quotes_cwd(self):
        env = _make_env(cwd="/two words")
        env._prepare_cwd()
        assert env.sandbox.commands.run.call_args_list[0].args[0] == "mkdir -p '/two words'"

    def test_tolerates_failure(self):
        # In a prepared evaluation image the directory exists already and may not be
        # writable by an unprivileged user -- that must not abort the run.
        env = _make_env(cwd="/testbed")
        env.sandbox.commands.run.side_effect = RuntimeError("permission denied")
        env._prepare_cwd()
        assert env.sandbox.commands.run.call_count == 2


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

    def test_execute_exception(self):
        env = _make_env()
        env.sandbox.commands.run.side_effect = RuntimeError("connection lost")

        output = env.execute({"command": "ls"})

        assert output["returncode"] == -1
        assert "connection lost" in output["exception_info"]
        assert output["extra"]["exception_type"] == "RuntimeError"

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
        assert not {env1, env2} & e2b_mod._active_sandboxes
