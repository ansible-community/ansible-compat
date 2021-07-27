"""Tests for Runtime class."""
# pylint: disable=protected-access
import logging
import os
import pathlib
import subprocess
from contextlib import contextmanager
from typing import Any, Iterator, List, Type

import pytest
from _pytest.monkeypatch import MonkeyPatch
from flaky import flaky
from packaging.version import Version
from pytest_mock import MockerFixture

from ansible_compat.constants import INVALID_PREREQUISITES_RC
from ansible_compat.errors import (
    AnsibleCommandError,
    AnsibleCompatError,
    InvalidPrerequisiteError,
)
from ansible_compat.runtime import CompletedProcess, Runtime, _update_env


def test_runtime_version(runtime: Runtime) -> None:
    """Tests version property."""
    version = runtime.version
    assert isinstance(version, Version)
    # tests that caching property value worked (coverage)
    assert version == runtime.version


@pytest.mark.parametrize(
    "require_module",
    (True, False),
    ids=("module-required", "module-unrequired"),
)
def test_runtime_version_outdated(require_module: bool) -> None:
    """Checks that instantiation raises if version is outdated."""
    with pytest.raises(RuntimeError, match="Found incompatible version of ansible"):
        Runtime(min_required_version="9999.9.9", require_module=require_module)


def test_runtime_missing_ansible_module(monkeypatch: MonkeyPatch) -> None:
    """Checks that we produce a RuntimeError when ansible module is missing."""

    class RaiseException:
        """Class to raise an exception."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise ModuleNotFoundError()

    monkeypatch.setattr("importlib.import_module", RaiseException)

    with pytest.raises(RuntimeError, match="Unable to find Ansible python module."):
        Runtime(require_module=True)


def test_runtime_mismatch_ansible_module(monkeypatch: MonkeyPatch) -> None:
    """Test that missing module is detected."""
    monkeypatch.setattr("ansible.release.__version__", "0.0.0", raising=False)
    with pytest.raises(RuntimeError, match="versions do not match"):
        Runtime(require_module=True)


def test_runtime_version_fail_module(mocker: MockerFixture) -> None:
    """Tests for failure to detect Ansible version."""
    mocker.patch(
        "ansible_compat.runtime.parse_ansible_version",
        return_value=("", "some error"),
        autospec=True,
    )
    runtime = Runtime()
    with pytest.raises(RuntimeError, match="some error"):
        runtime.version  # pylint: disable=pointless-statement


def test_runtime_version_fail_cli(mocker: MockerFixture) -> None:
    """Tests for failure to detect Ansible version."""
    mocker.patch(
        "ansible_compat.runtime.Runtime.exec",
        return_value=CompletedProcess(
            ["x"], returncode=123, stdout="oops", stderr="some error"
        ),
        autospec=True,
    )
    runtime = Runtime()
    with pytest.raises(
        RuntimeError, match="Unable to find a working copy of ansible executable."
    ):
        runtime.version  # pylint: disable=pointless-statement


def test_runtime_prepare_ansible_paths_validation() -> None:
    """Check that we validate collection_path."""
    runtime = Runtime()
    runtime.config.collections_path = "invalid"  # type: ignore
    with pytest.raises(RuntimeError, match="Unexpected collection_path value:"):
        runtime._prepare_ansible_paths()


@pytest.mark.parametrize(
    ("folder", "role_name"),
    (
        ("ansible-role-sample", "acme.sample"),
        ("acme.sample2", "acme.sample2"),
        ("sample3", "acme.sample3"),
    ),
    ids=("sample", "sample2", "sample3"),
)
def test_runtime_install_role(
    caplog: pytest.LogCaptureFixture, folder: str, role_name: str
) -> None:
    """Checks that we can install roles."""
    caplog.set_level(logging.INFO)
    project_dir = os.path.join(os.path.dirname(__file__), "roles", folder)
    runtime = Runtime(isolated=True, project_dir=project_dir)
    runtime.prepare_environment()
    assert (
        "symlink to current repository in order to enable Ansible to find the role"
        in caplog.text
    )
    # check that role appears as installed now
    result = runtime.exec(["ansible-galaxy", "list"])
    assert result.returncode == 0, result
    assert role_name in result.stdout
    runtime.clean()


def test_prepare_environment_with_collections(tmp_path: pathlib.Path) -> None:
    """Check that collections are correctly installed."""
    runtime = Runtime(isolated=True, project_dir=str(tmp_path))
    runtime.prepare_environment(required_collections={"community.molecule": "0.1.0"})


def test_runtime_install_requirements_missing_file() -> None:
    """Check that missing requirements file is ignored."""
    # Do not rely on this behavior, it may be removed in the future
    runtime = Runtime()
    runtime.install_requirements("/that/does/not/exist")


@pytest.mark.parametrize(
    ("file", "exc", "msg"),
    (
        (
            "/dev/null",
            InvalidPrerequisiteError,
            "file is not a valid Ansible requirements file",
        ),
        (
            os.path.join(
                os.path.dirname(__file__),
                "assets",
                "requirements-invalid-collection.yml",
            ),
            AnsibleCommandError,
            "Got 1 exit code while running: ansible-galaxy",
        ),
        (
            os.path.join(
                os.path.dirname(__file__),
                "assets",
                "requirements-invalid-role.yml",
            ),
            AnsibleCommandError,
            "Got 1 exit code while running: ansible-galaxy",
        ),
    ),
    ids=("empty", "invalid-collection", "invalid-role"),
)
def test_runtime_install_requirements_invalid_file(
    file: str, exc: Type[Any], msg: str
) -> None:
    """Check that invalid requirements file is raising."""
    runtime = Runtime()
    with pytest.raises(
        exc,
        match=msg,
    ):
        runtime.install_requirements(file)


@contextmanager
def remember_cwd(cwd: str) -> Iterator[None]:
    """Context manager for chdir."""
    curdir = os.getcwd()
    try:
        os.chdir(cwd)
        yield
    finally:
        os.chdir(curdir)


# # https://github.com/box/flaky/issues/170
@flaky(max_runs=3)  # type: ignore
def test_prerun_reqs_v1(caplog: pytest.LogCaptureFixture, runtime: Runtime) -> None:
    """Checks that the linter can auto-install requirements v1 when found."""
    cwd = os.path.realpath(
        os.path.join(
            os.path.dirname(os.path.realpath(__file__)), "..", "examples", "reqs_v1"
        )
    )
    with remember_cwd(cwd):
        with caplog.at_level(logging.INFO):
            runtime.prepare_environment()
    assert any(
        msg.startswith("Running ansible-galaxy role install") for msg in caplog.messages
    )
    assert all(
        "Running ansible-galaxy collection install" not in msg
        for msg in caplog.messages
    )


@flaky(max_runs=3)  # type: ignore
def test_prerun_reqs_v2(caplog: pytest.LogCaptureFixture, runtime: Runtime) -> None:
    """Checks that the linter can auto-install requirements v2 when found."""
    cwd = os.path.realpath(
        os.path.join(
            os.path.dirname(os.path.realpath(__file__)), "..", "examples", "reqs_v2"
        )
    )
    with remember_cwd(cwd):
        with caplog.at_level(logging.INFO):
            runtime.prepare_environment()
        assert any(
            msg.startswith("Running ansible-galaxy role install")
            for msg in caplog.messages
        )
        assert any(
            msg.startswith("Running ansible-galaxy collection install")
            for msg in caplog.messages
        )


def test__update_env_no_old_value_no_default_no_value(monkeypatch: MonkeyPatch) -> None:
    """Make sure empty value does not touch environment."""
    monkeypatch.delenv("DUMMY_VAR", raising=False)

    _update_env("DUMMY_VAR", [])

    assert "DUMMY_VAR" not in os.environ


def test__update_env_no_old_value_no_value(monkeypatch: MonkeyPatch) -> None:
    """Make sure empty value does not touch environment."""
    monkeypatch.delenv("DUMMY_VAR", raising=False)

    _update_env("DUMMY_VAR", [], "a:b")

    assert "DUMMY_VAR" not in os.environ


def test__update_env_no_default_no_value(monkeypatch: MonkeyPatch) -> None:
    """Make sure empty value does not touch environment."""
    monkeypatch.setenv("DUMMY_VAR", "a:b")

    _update_env("DUMMY_VAR", [])

    assert os.environ["DUMMY_VAR"] == "a:b"


@pytest.mark.parametrize(
    ("value", "result"),
    (
        (["a"], "a"),
        (["a", "b"], "a:b"),
        (["a", "b", "c"], "a:b:c"),
    ),
)
def test__update_env_no_old_value_no_default(
    monkeypatch: MonkeyPatch, value: List[str], result: str
) -> None:
    """Values are concatenated using : as the separator."""
    monkeypatch.delenv("DUMMY_VAR", raising=False)

    _update_env("DUMMY_VAR", value)

    assert os.environ["DUMMY_VAR"] == result


@pytest.mark.parametrize(
    ("default", "value", "result"),
    (
        ("a:b", ["c"], "c:a:b"),
        ("a:b", ["c:d"], "c:d:a:b"),
    ),
)
def test__update_env_no_old_value(
    monkeypatch: MonkeyPatch, default: str, value: List[str], result: str
) -> None:
    """Values are appended to default value."""
    monkeypatch.delenv("DUMMY_VAR", raising=False)

    _update_env("DUMMY_VAR", value, default)

    assert os.environ["DUMMY_VAR"] == result


@pytest.mark.parametrize(
    ("old_value", "value", "result"),
    (
        ("a:b", ["c"], "c:a:b"),
        ("a:b", ["c:d"], "c:d:a:b"),
    ),
)
def test__update_env_no_default(
    monkeypatch: MonkeyPatch, old_value: str, value: List[str], result: str
) -> None:
    """Values are appended to preexisting value."""
    monkeypatch.setenv("DUMMY_VAR", old_value)

    _update_env("DUMMY_VAR", value)

    assert os.environ["DUMMY_VAR"] == result


@pytest.mark.parametrize(
    ("old_value", "default", "value", "result"),
    (
        ("", "", ["e"], "e"),
        ("a", "", ["e"], "e:a"),
        ("", "c", ["e"], "e"),
        ("a", "c", ["e:f"], "e:f:a"),
    ),
)
def test__update_env(
    monkeypatch: MonkeyPatch,
    old_value: str,
    default: str,  # pylint: disable=unused-argument
    value: List[str],
    result: str,
) -> None:
    """Defaults are ignored when preexisting value is present."""
    monkeypatch.setenv("DUMMY_VAR", old_value)

    _update_env("DUMMY_VAR", value)

    assert os.environ["DUMMY_VAR"] == result


def test_require_collection_wrong_version(runtime: Runtime) -> None:
    """Tests behaviour of require_collection."""
    subprocess.check_output(
        [
            "ansible-galaxy",
            "collection",
            "install",
            "containers.podman",
            "-p",
            "~/.ansible/collections",
        ]
    )
    with pytest.raises(InvalidPrerequisiteError) as pytest_wrapped_e:
        runtime.require_collection("containers.podman", '9999.9.9')
    assert pytest_wrapped_e.type == InvalidPrerequisiteError
    assert pytest_wrapped_e.value.code == INVALID_PREREQUISITES_RC


def test_require_collection_invalid_name(runtime: Runtime) -> None:
    """Check that require_collection raise with invalid collection name."""
    with pytest.raises(
        InvalidPrerequisiteError, match="Invalid collection name supplied:"
    ):
        runtime.require_collection("that-is-invalid")


def test_require_collection_invalid_collections_path(runtime: Runtime) -> None:
    """Check that require_collection raise with invalid collections path."""
    runtime.config.collections_path = '/that/is/invalid'  # type: ignore
    with pytest.raises(
        InvalidPrerequisiteError, match="Unable to determine ansible collection paths"
    ):
        runtime.require_collection("community.molecule")


def test_require_collection_preexisting_broken(tmp_path: pathlib.Path) -> None:
    """Check that require_collection raise with broken pre-existing collection."""
    runtime = Runtime(isolated=True, project_dir=str(tmp_path))
    dest_path: str = runtime.config.collections_path[0]  # type: ignore
    dest = os.path.join(dest_path, "ansible_collections", "foo", "bar")
    os.makedirs(dest, exist_ok=True)
    with pytest.raises(InvalidPrerequisiteError, match="missing MANIFEST.json"):
        runtime.require_collection("foo.bar")


@pytest.mark.parametrize(
    ("name", "version"),
    (
        ("fake_namespace.fake_name", None),
        ("fake_namespace.fake_name", "9999.9.9"),
    ),
)
def test_require_collection_missing(name: str, version: str, runtime: Runtime) -> None:
    """Tests behaviour of require_collection, missing case."""
    with pytest.raises(AnsibleCompatError) as pytest_wrapped_e:
        runtime.require_collection(name, version)
    assert pytest_wrapped_e.type == InvalidPrerequisiteError
    assert pytest_wrapped_e.value.code == INVALID_PREREQUISITES_RC


def test_install_collection(runtime: Runtime) -> None:
    """Check that valid collection installs do not fail."""
    runtime.install_collection("containers.podman:>=1.0")


def test_install_collection_dest(runtime: Runtime, tmp_path: pathlib.Path) -> None:
    """Check that valid collection to custom destination passes."""
    runtime.install_collection("containers.podman:>=1.0", destination=tmp_path)
    expected_file = (
        tmp_path / "ansible_collections" / "containers" / "podman" / "MANIFEST.json"
    )
    assert expected_file.is_file()


def test_install_collection_fail(runtime: Runtime) -> None:
    """Check that invalid collection install fails."""
    with pytest.raises(AnsibleCompatError) as pytest_wrapped_e:
        runtime.install_collection("containers.podman:>=9999.0")
    assert pytest_wrapped_e.type == InvalidPrerequisiteError
    assert pytest_wrapped_e.value.code == INVALID_PREREQUISITES_RC
