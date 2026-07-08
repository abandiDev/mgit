"""Data classes for mgit."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


@dataclass
class RepoInfo:
    """A registered repository in the workspace."""

    name: str
    path: str
    url: str | None = None
    default_branch: str = "main"
    setup: str | None = None


@dataclass
class FeatureInfo:
    """A cross-repo feature definition."""

    name: str
    description: str = ""
    branches: dict[str, str] = field(default_factory=dict)  # repo_name -> branch_name
    parent: str | None = None  # feature this was forked from
    fork_base: dict[str, str] = field(default_factory=dict)  # repo_name -> SHA pinned at fork
    wip_from: dict[str, str] = field(default_factory=dict)  # repo_name -> WIP snapshot SHA


@dataclass
class MemoryState:
    """Per-feature working-memory plan file (memory.toml)."""

    goal: str = ""
    status: str = ""
    next_steps: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    updated_at: str = ""


@dataclass
class CheckpointRepoState:
    """One repo's pinned state inside a checkpoint."""

    head: str  # committed state (branch tip at save time)
    snapshot: str  # WIP snapshot commit; == head when the tree was clean
    branch: str
    dirty: bool


@dataclass
class CheckpointInfo:
    """A cross-repo checkpoint manifest."""

    id: str
    feature: str
    label: str = ""
    created_at: str = ""
    repos: dict[str, CheckpointRepoState] = field(default_factory=dict)


class OpStatus(Enum):
    """Outcome of a single repo operation."""

    SUCCESS = "success"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass
class RepoOpResult:
    """Result of an operation on a single repo."""

    repo: str
    status: OpStatus
    message: str = ""
    output: str = ""

    def to_dict(self) -> dict:
        return {
            "repo": self.repo,
            "status": self.status.value,
            "message": self.message,
            "output": self.output,
        }


@dataclass
class BulkResult:
    """Aggregated results of a bulk operation across repos."""

    results: list[RepoOpResult] = field(default_factory=list)

    @property
    def succeeded(self) -> list[RepoOpResult]:
        return [r for r in self.results if r.status == OpStatus.SUCCESS]

    @property
    def skipped(self) -> list[RepoOpResult]:
        return [r for r in self.results if r.status == OpStatus.SKIPPED]

    @property
    def failed(self) -> list[RepoOpResult]:
        return [r for r in self.results if r.status == OpStatus.FAILED]

    @property
    def all_ok(self) -> bool:
        return len(self.failed) == 0

    @property
    def exit_code(self) -> int:
        return 0 if self.all_ok else 1

    def to_dict(self) -> dict:
        return {
            "results": [r.to_dict() for r in self.results],
            "summary": {
                "succeeded": len(self.succeeded),
                "skipped": len(self.skipped),
                "failed": len(self.failed),
            },
        }
