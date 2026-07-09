"""Custom exceptions for mgit."""


class MgitError(Exception):
    """Base exception for mgit."""


class WorkspaceNotFoundError(MgitError):
    """No .mgit/ directory found in current or parent directories."""


class WorkspaceExistsError(MgitError):
    """A workspace already exists at this location."""


class RepoNotFoundError(MgitError):
    """Referenced repo is not registered in the workspace."""


class RepoExistsError(MgitError):
    """A repo with this name already exists in the workspace."""


class FeatureNotFoundError(MgitError):
    """Referenced feature does not exist."""


class FeatureExistsError(MgitError):
    """A feature with this name already exists."""


class SkillNotFoundError(MgitError):
    """Referenced skill draft or active skill does not exist."""


class GitError(MgitError):
    """A git command failed."""

    def __init__(self, message: str, returncode: int = 1, stderr: str = ""):
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


class DirtyTreeError(MgitError):
    """One or more repos have uncommitted changes."""

    def __init__(self, dirty_repos: list[str]):
        self.dirty_repos = dirty_repos
        super().__init__(
            f"Repos with uncommitted changes: {', '.join(dirty_repos)}"
        )
