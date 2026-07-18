from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from copperbrain.services.updates import RepositoryUpdateService, UpdateRefusal

OFFICIAL_ORIGIN = "https://github.com/deadbringer17/Copperbrain.git"
LOCAL = "a" * 40
REMOTE = "b" * 40


@dataclass
class FakeUpdateBackend:
    origin: str = OFFICIAL_ORIGIN
    branch: str = "main"
    changes: tuple[str, ...] = ()
    head: str = LOCAL
    remote: str = LOCAL
    ancestor_pairs: set[tuple[str, str]] = field(default_factory=set)
    fetched: bool = False
    fast_forwarded: bool = False

    def verify_checkout(self) -> None:
        return None

    def remote_url(self) -> str:
        return self.origin

    def current_branch(self) -> str:
        return self.branch

    def working_tree_changes(self) -> tuple[str, ...]:
        return self.changes

    def fetch_main(self) -> None:
        self.fetched = True

    def revision(self, reference: str) -> str:
        return self.head if reference == "HEAD" else self.remote

    def is_ancestor(self, older: str, newer: str) -> bool:
        return (older, newer) in self.ancestor_pairs

    def fast_forward_main(self) -> None:
        self.fast_forwarded = True
        self.head = self.remote


def test_update_fast_forwards_clean_main() -> None:
    backend = FakeUpdateBackend(remote=REMOTE, ancestor_pairs={(LOCAL, REMOTE)})

    result = RepositoryUpdateService(backend).update()

    assert result.outcome == "updated"
    assert result.previous_revision == LOCAL
    assert result.current_revision == REMOTE
    assert backend.fetched is True
    assert backend.fast_forwarded is True


def test_update_reports_up_to_date_without_merge() -> None:
    backend = FakeUpdateBackend()

    result = RepositoryUpdateService(backend).update()

    assert result.outcome == "up_to_date"
    assert backend.fetched is True
    assert backend.fast_forwarded is False


def test_update_refuses_dirty_worktree_before_fetch() -> None:
    backend = FakeUpdateBackend(changes=(" M README.md",))

    with pytest.raises(UpdateRefusal, match="local changes") as error:
        RepositoryUpdateService(backend).update()

    assert error.value.reason == "dirty_worktree"
    assert backend.fetched is False


@pytest.mark.parametrize(
    ("backend", "reason"),
    [
        (FakeUpdateBackend(origin="https://example.com/not-copperbrain.git"), "unexpected_remote"),
        (FakeUpdateBackend(branch="feature/demo"), "wrong_branch"),
        (FakeUpdateBackend(branch=""), "wrong_branch"),
    ],
)
def test_update_refuses_untrusted_context(backend: FakeUpdateBackend, reason: str) -> None:
    with pytest.raises(UpdateRefusal) as error:
        RepositoryUpdateService(backend).update()

    assert error.value.reason == reason
    assert backend.fetched is False


def test_update_reports_local_ahead_without_mutation() -> None:
    backend = FakeUpdateBackend(remote=REMOTE, ancestor_pairs={(REMOTE, LOCAL)})

    result = RepositoryUpdateService(backend).update()

    assert result.outcome == "local_ahead"
    assert backend.fast_forwarded is False


def test_update_refuses_diverged_history() -> None:
    backend = FakeUpdateBackend(remote=REMOTE)

    with pytest.raises(UpdateRefusal, match="diverged") as error:
        RepositoryUpdateService(backend).update()

    assert error.value.reason == "diverged_history"
    assert backend.fast_forwarded is False
