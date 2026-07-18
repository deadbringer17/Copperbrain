"""Safe application service for updating a Copperbrain source checkout."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

EXPECTED_ORIGIN_URLS = frozenset(
    {
        "https://github.com/deadbringer17/Copperbrain",
        "https://github.com/deadbringer17/Copperbrain.git",
    }
)


class RepositoryUpdateBackend(Protocol):
    """Fixed-operation repository boundary used by the update service."""

    def verify_checkout(self) -> None: ...

    def remote_url(self) -> str: ...

    def current_branch(self) -> str: ...

    def working_tree_changes(self) -> tuple[str, ...]: ...

    def fetch_main(self) -> None: ...

    def revision(self, reference: str) -> str: ...

    def is_ancestor(self, older: str, newer: str) -> bool: ...

    def fast_forward_main(self) -> None: ...


class UpdateRefusal(RuntimeError):
    """An actionable reason why a source update was not applied."""

    def __init__(self, reason: str, message: str, *, hint: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.hint = hint


@dataclass(frozen=True)
class RepositoryUpdateResult:
    """Deterministic outcome of one explicit update request."""

    outcome: Literal["updated", "up_to_date", "local_ahead"]
    previous_revision: str
    current_revision: str


class RepositoryUpdateService:
    """Apply only a clean, reviewed-origin, main-branch fast-forward."""

    def __init__(
        self,
        backend: RepositoryUpdateBackend,
        *,
        expected_origin_urls: frozenset[str] = EXPECTED_ORIGIN_URLS,
    ) -> None:
        self._backend = backend
        self._expected_origin_urls = expected_origin_urls

    def update(self) -> RepositoryUpdateResult:
        self._backend.verify_checkout()
        remote_url = self._backend.remote_url().rstrip("/")
        if remote_url not in self._expected_origin_urls:
            raise UpdateRefusal(
                "unexpected_remote",
                f"Refusing to update from unexpected origin URL: {remote_url}",
                hint="Set origin to the official Copperbrain GitHub repository and retry.",
            )

        branch = self._backend.current_branch()
        if branch != "main":
            raise UpdateRefusal(
                "wrong_branch",
                f"Refusing to update branch {branch or '<detached HEAD>'}; expected main.",
                hint="Switch to main after preserving your current work, then retry.",
            )

        changes = self._backend.working_tree_changes()
        if changes:
            raise UpdateRefusal(
                "dirty_worktree",
                "Refusing to update because the Copperbrain checkout has local changes.",
                hint="Commit, stash, or otherwise preserve the local changes before retrying.",
            )

        previous = self._backend.revision("HEAD")
        self._backend.fetch_main()
        remote = self._backend.revision("origin/main")
        if previous == remote:
            return RepositoryUpdateResult("up_to_date", previous, previous)

        if self._backend.is_ancestor(previous, remote):
            self._backend.fast_forward_main()
            current = self._backend.revision("HEAD")
            if current != remote:
                raise UpdateRefusal(
                    "verification_failed",
                    "The update command completed but HEAD does not match origin/main.",
                    hint="Inspect the checkout before starting Copperbrain again.",
                )
            return RepositoryUpdateResult("updated", previous, current)

        if self._backend.is_ancestor(remote, previous):
            return RepositoryUpdateResult("local_ahead", previous, previous)

        raise UpdateRefusal(
            "diverged_history",
            "Local main and origin/main have diverged; no update was applied.",
            hint="Review and reconcile the Git history manually.",
        )
