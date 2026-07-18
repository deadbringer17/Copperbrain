"""Fixed-command Git adapter for explicit Copperbrain source updates."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path


class RepositoryUpdateAdapterError(RuntimeError):
    """A fixed Git operation could not be completed."""


class GitRepositoryUpdateAdapter:
    """Expose only the Git operations required for a safe fast-forward update."""

    def __init__(
        self,
        root: Path,
        *,
        git_executable: str | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        executable = git_executable or shutil.which("git")
        if not executable:
            raise RepositoryUpdateAdapterError("Git is not installed or is not available on PATH.")
        self.root = root.resolve()
        self._git = executable
        self._runner = runner

    def _run(
        self,
        arguments: Sequence[str],
        *,
        accepted_codes: frozenset[int] = frozenset({0}),
        timeout_seconds: int = 120,
    ) -> subprocess.CompletedProcess[str]:
        result = self._runner(
            [self._git, *arguments],
            cwd=self.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout_seconds,
        )
        if result.returncode not in accepted_codes:
            detail = (result.stderr or result.stdout).strip()[-1000:]
            raise RepositoryUpdateAdapterError(
                f"Git operation failed ({' '.join(arguments)}): {detail or 'no details'}"
            )
        return result

    def verify_checkout(self) -> None:
        result = self._run(("rev-parse", "--show-toplevel"))
        reported = Path(result.stdout.strip()).resolve()
        if reported != self.root:
            raise RepositoryUpdateAdapterError(
                f"Expected repository root {self.root}, but Git reported {reported}."
            )

    def remote_url(self) -> str:
        return self._run(("remote", "get-url", "origin")).stdout.strip()

    def current_branch(self) -> str:
        return self._run(("branch", "--show-current")).stdout.strip()

    def working_tree_changes(self) -> tuple[str, ...]:
        output = self._run(("status", "--porcelain=v1", "--untracked-files=all")).stdout
        return tuple(line for line in output.splitlines() if line)

    def fetch_main(self) -> None:
        self._run(("fetch", "--prune", "origin", "main"), timeout_seconds=180)

    def revision(self, reference: str) -> str:
        if reference not in {"HEAD", "origin/main"}:
            raise ValueError(f"Unsupported revision reference: {reference}")
        return self._run(("rev-parse", reference)).stdout.strip().lower()

    def is_ancestor(self, older: str, newer: str) -> bool:
        result = self._run(
            ("merge-base", "--is-ancestor", older, newer),
            accepted_codes=frozenset({0, 1}),
        )
        return result.returncode == 0

    def fast_forward_main(self) -> None:
        self._run(("merge", "--ff-only", "origin/main"))
