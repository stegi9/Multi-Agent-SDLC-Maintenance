"""Astrazione del provider Git (disaccoppia l'orchestrazione dal servizio concreto).

`git_flow` ragiona in termini di :class:`GitProvider` e non conosce GitHub. Per
aggiungere GitLab/Bitbucket/self-hosted basta una nuova implementazione del
protocollo e un ramo nella factory :func:`get_provider`, senza toccare la logica
di orchestrazione delle Pull Request.
"""
from typing import Protocol, runtime_checkable

from app.services import github_client
from app.services.github_client import GitError, MergeConflictError

__all__ = ["GitProvider", "GitError", "MergeConflictError", "get_provider"]


@runtime_checkable
class GitProvider(Protocol):
    """Operazioni Git necessarie all'orchestrazione dei Work Order."""

    name: str

    async def default_branch(self) -> str: ...
    async def branch_exists(self, branch: str) -> bool: ...
    async def ensure_branch(self, branch: str, base: str) -> bool: ...
    async def commit_file(
        self, branch: str, path: str, content: str, message: str, author: dict | None = None
    ) -> bool: ...
    async def open_pr(self, head: str, base: str, title: str, body: str = "") -> dict: ...
    async def request_reviewers(self, number: int, reviewers: list[str]) -> None: ...
    async def add_assignees(self, number: int, assignees: list[str]) -> None: ...
    async def pr_mergeability(self, number: int) -> dict: ...
    async def ci_status(self, sha: str) -> str: ...
    async def merge_pr(self, number: int, method: str = "merge", sha: str | None = None) -> bool: ...
    async def close_pr(self, number: int) -> bool: ...
    async def delete_branch(self, branch: str) -> bool: ...


class GitHubProvider:
    """Implementazione GitHub del :class:`GitProvider` (adapter su github_client)."""

    name = "github"

    def __init__(self, cfg: github_client.GitHubConfig) -> None:
        self._cfg = cfg

    async def default_branch(self) -> str:
        return await github_client.get_default_branch(self._cfg)

    async def branch_exists(self, branch: str) -> bool:
        return await github_client.branch_exists(self._cfg, branch)

    async def ensure_branch(self, branch: str, base: str) -> bool:
        return await github_client.ensure_branch(self._cfg, branch, base)

    async def commit_file(
        self, branch: str, path: str, content: str, message: str, author: dict | None = None
    ) -> bool:
        return await github_client.put_file(self._cfg, branch, path, content, message, author=author)

    async def open_pr(self, head: str, base: str, title: str, body: str = "") -> dict:
        return await github_client.open_pull_request(self._cfg, head=head, base=base, title=title, body=body)

    async def request_reviewers(self, number: int, reviewers: list[str]) -> None:
        await github_client.request_reviewers(self._cfg, number, reviewers)

    async def add_assignees(self, number: int, assignees: list[str]) -> None:
        await github_client.add_assignees(self._cfg, number, assignees)

    async def pr_mergeability(self, number: int) -> dict:
        return await github_client.wait_until_mergeable(self._cfg, number)

    async def ci_status(self, sha: str) -> str:
        return await github_client.commit_ci_status(self._cfg, sha)

    async def merge_pr(self, number: int, method: str = "merge", sha: str | None = None) -> bool:
        return await github_client.merge_pull_request(self._cfg, number, method=method, sha=sha)

    async def close_pr(self, number: int) -> bool:
        return await github_client.close_pull_request(self._cfg, number)

    async def delete_branch(self, branch: str) -> bool:
        return await github_client.delete_branch(self._cfg, branch)


async def get_provider(repo_url: str) -> GitProvider | None:
    """Ritorna il provider adatto all'URL del repository, o None se non gestito.

    Oggi è supportato solo GitHub; l'aggiunta di altri provider passa da qui.
    """
    cfg = await github_client.config_for_repo(repo_url)
    if cfg is not None:
        return GitHubProvider(cfg)
    return None
