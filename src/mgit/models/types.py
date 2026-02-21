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


@dataclass
class FeatureInfo:
    """A cross-repo feature definition."""

    name: str
    description: str = ""
    branches: dict[str, str] = field(default_factory=dict)  # repo_name -> branch_name


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
