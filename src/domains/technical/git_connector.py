"""Incremental Git connector with revision-level provenance."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from services.ingest.common.document_model import Document
from src.domains.technical.model import sanitize_repository_url
from src.ingestion.connectors.base import Connector, RawDocument, SourceRef
from src.ingestion.connectors.registry import register_connector

_MANIFEST_NAMES = {
    "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
    "pyproject.toml", "poetry.lock", "requirements.txt", "Cargo.toml",
    "Cargo.lock", "go.mod", "go.sum", "pom.xml", "build.gradle",
    "build.gradle.kts", "Gemfile", "Gemfile.lock", "composer.json",
}


class GitConnectorError(RuntimeError):
    pass


@register_connector
class GitRepositoryConnector(Connector):
    """Harvest a local or authenticated remote repository without persisting secrets."""

    source_type = "note"
    name = "git-repository"

    def __init__(
        self,
        *,
        cache_dir: str | Path | None = None,
        runner=subprocess.run,
        max_file_bytes: int = 1_000_000,
        max_commits: int = 500,
    ) -> None:
        self.cache_dir = Path(cache_dir) if cache_dir else Path(tempfile.gettempdir()) / "noesis-git"
        self.runner = runner
        self.max_file_bytes = int(max_file_bytes)
        self.max_commits = int(max_commits)
        self._heads: dict[str, str] = {}

    def discover(self, query: Any = None) -> Iterable[SourceRef]:
        if isinstance(query, str):
            query = {"repository": query}
        query = dict(query or {})
        locator = str(query.get("repository") or query.get("path") or query.get("url") or "").strip()
        if not locator:
            raise GitConnectorError("repository path or URL is required")
        safe = sanitize_repository_url(locator)
        source_id = "git:" + hashlib.sha256(safe.encode()).hexdigest()[:20]
        metadata = {
            "source_id": source_id,
            "previous_head": query.get("previous_head") or self._heads.get(source_id),
            "auth_env": query.get("auth_env"),
            "revision": query.get("revision") or "HEAD",
            "max_file_bytes": int(query.get("max_file_bytes") or self.max_file_bytes),
        }
        yield SourceRef(locator=safe, title=Path(safe).name.removesuffix(".git"), metadata=metadata)

    def _command(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        auth_env: str | None = None,
        check: bool = True,
    ) -> str:
        env = os.environ.copy()
        if auth_env:
            secret = os.getenv(auth_env)
            if not secret:
                raise GitConnectorError(f"credential environment variable {auth_env!r} is unset")
            env.update(
                {
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "http.extraHeader",
                    "GIT_CONFIG_VALUE_0": f"Authorization: Bearer {secret}",
                }
            )
        result = self.runner(
            args,
            cwd=str(cwd) if cwd else None,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        if check and result.returncode:
            message = (result.stderr or result.stdout or "git command failed").strip()
            if auth_env and os.getenv(auth_env):
                message = message.replace(os.environ[auth_env], "[redacted]")
            raise GitConnectorError(message)
        return result.stdout

    def _repository(self, ref: SourceRef) -> Path:
        locator = ref.locator
        local = Path(locator)
        if local.exists():
            return local.resolve()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        target = self.cache_dir / ref.source_id.replace(":", "-")
        auth_env = ref.metadata.get("auth_env")
        if not target.exists():
            self._command(
                ["git", "clone", "--filter=blob:none", "--no-checkout", locator, str(target)],
                auth_env=auth_env,
            )
        else:
            self._command(
                ["git", "fetch", "--prune", "--tags", "origin"],
                cwd=target,
                auth_env=auth_env,
            )
        return target

    def _git(self, repo: Path, *args: str, check: bool = True) -> str:
        return self._command(["git", *args], cwd=repo, check=check)

    def fetch(self, ref: SourceRef) -> RawDocument:
        repo = self._repository(ref)
        revision = str(ref.metadata.get("revision") or "HEAD")
        head = self._git(repo, "rev-parse", revision).strip()
        previous = ref.metadata.get("previous_head")
        force_push = False
        if previous:
            force_push = self.runner(
                ["git", "merge-base", "--is-ancestor", str(previous), head],
                cwd=str(repo),
                env=os.environ.copy(),
                check=False,
                capture_output=True,
                text=True,
            ).returncode != 0
        commit_range = f"{previous}..{head}" if previous and not force_push else head
        log = self._git(
            repo, "log", f"--max-count={self.max_commits}",
            "--format=%H%x1f%an%x1f%aI%x1f%s", commit_range,
        )
        commits = [
            dict(zip(("revision", "author", "timestamp", "subject"), line.split("\x1f", 3)))
            for line in log.splitlines() if line.count("\x1f") == 3
        ]
        refs = self._git(
            repo, "for-each-ref", "--format=%(refname)%09%(objectname)",
            "refs/heads", "refs/remotes", "refs/tags",
        )
        tree_paths = self._git(repo, "ls-tree", "-r", "--name-only", head).splitlines()
        selected = [
            path for path in tree_paths
            if Path(path).name in _MANIFEST_NAMES
            or Path(path).name.casefold().startswith("readme")
            or path.startswith("docs/")
        ]
        files, omitted = [], []
        limit = int(ref.metadata.get("max_file_bytes") or self.max_file_bytes)
        for path in selected:
            size_raw = self._git(repo, "cat-file", "-s", f"{head}:{path}", check=False).strip()
            size = int(size_raw) if size_raw.isdigit() else 0
            if size > limit:
                omitted.append({"path": path, "size": size, "reason": "large_file"})
                continue
            content = self._git(repo, "show", f"{head}:{path}", check=False)
            files.append({"path": path, "size": size, "content": content})
        deleted: list[str] = []
        if previous and not force_push:
            deleted = [
                line[2:] for line in self._git(
                    repo, "diff", "--name-status", str(previous), head, check=False
                ).splitlines() if line.startswith("D\t")
            ]
        stages = self._git(repo, "ls-tree", "-r", head).splitlines()
        submodules = [
            line.split("\t", 1)[1] for line in stages
            if line.startswith("160000 commit ") and "\t" in line
        ]
        root_git = repo / ".git"
        shallow = (root_git / "shallow").exists() if root_git.is_dir() else False
        payload = {
            "repository": ref.locator,
            "source_id": ref.source_id,
            "head": head,
            "previous_head": previous,
            "force_push_detected": force_push,
            "shallow": shallow,
            "refs": [
                {"name": line.split("\t", 1)[0], "revision": line.split("\t", 1)[1]}
                for line in refs.splitlines() if "\t" in line
            ],
            "commits": commits,
            "files": files,
            "omitted": omitted,
            "deleted_paths": deleted,
            "submodules": submodules,
            "credential_mode": "environment" if ref.metadata.get("auth_env") else "none",
        }
        self._heads[ref.source_id] = head
        return RawDocument(ref=ref, content=json.dumps(payload), content_type="application/json")

    def cursors(self) -> dict[str, str]:
        """Return resumable repository heads for durable caller-side storage."""

        return dict(self._heads)

    def parse(self, raw: RawDocument) -> list[Document]:
        payload = json.loads(raw.content)
        repo = payload["repository"]
        head = payload["head"]
        common = {
            "repository": repo,
            "revision": head,
            "force_push_detected": payload["force_push_detected"],
            "shallow": payload["shallow"],
            "submodules": payload["submodules"],
            "deleted_paths": payload["deleted_paths"],
            "omitted": payload["omitted"],
            "credential_mode": payload["credential_mode"],
        }
        documents = [
            Document(
                document_id=f"technical:git:{payload['source_id']}:{head}",
                source_type=self.source_type,
                language="en",
                ingested_at=raw.fetched_at,
                source_id=payload["source_id"],
                url=repo,
                title=f"{raw.ref.title or repo} repository snapshot",
                content="\n".join(
                    f"{item['revision']} {item['subject']}" for item in payload["commits"]
                ),
                authors=list(dict.fromkeys(item["author"] for item in payload["commits"])),
                metadata={**common, "kind": "repository", "refs": payload["refs"], "commits": payload["commits"]},
            )
        ]
        commit_by_revision = {item["revision"]: item for item in payload["commits"]}
        head_commit = commit_by_revision.get(head, {})
        for item in payload["files"]:
            documents.append(
                Document(
                    document_id=(
                        "technical:git-file:"
                        + hashlib.sha256(f"{payload['source_id']}:{head}:{item['path']}".encode()).hexdigest()[:24]
                    ),
                    source_type=self.source_type,
                    language="en",
                    ingested_at=raw.fetched_at,
                    source_id=payload["source_id"],
                    url=f"{repo}#rev={head}&path={item['path']}",
                    title=item["path"],
                    content=item["content"],
                    authors=[head_commit["author"]] if head_commit.get("author") else [],
                    created_at=_iso_millis(head_commit.get("timestamp")),
                    metadata={
                        **common, "kind": "repository_file", "path": item["path"],
                        "size": item["size"], "author": head_commit.get("author"),
                        "timestamp": head_commit.get("timestamp"),
                    },
                )
            )
        return documents


def _iso_millis(value: str | None) -> int | None:
    if not value:
        return None
    from src.kb.temporal import parse_source_time

    return parse_source_time(value, field="commit_timestamp")[0]


__all__ = ["GitConnectorError", "GitRepositoryConnector"]
