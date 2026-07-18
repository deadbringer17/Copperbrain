from __future__ import annotations

import subprocess
from pathlib import Path

from copperbrain.adapters.repository_updates import GitRepositoryUpdateAdapter


def test_adapter_uses_fixed_fetch_command(tmp_path: Path) -> None:
    calls: list[tuple[list[str], Path]] = []

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs["cwd"]))  # type: ignore[arg-type]
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    adapter = GitRepositoryUpdateAdapter(tmp_path, git_executable="git", runner=runner)
    adapter.fetch_main()

    assert calls == [(["git", "fetch", "--prune", "origin", "main"], tmp_path.resolve())]


def test_adapter_limits_revision_references(tmp_path: Path) -> None:
    adapter = GitRepositoryUpdateAdapter(tmp_path, git_executable="git")

    try:
        adapter.revision("user-controlled-ref")
    except ValueError as error:
        assert "Unsupported revision" in str(error)
    else:
        raise AssertionError("Expected an unsupported revision to be rejected")
