"""ThreadPoolExecutor-based bulk runner for repo operations."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

from mgit.models.types import BulkResult, OpStatus, RepoOpResult


def run_bulk(
    repo_names: list[str],
    operation: Callable[[str], RepoOpResult],
    max_workers: int = 8,
    fail_fast: bool = False,
) -> BulkResult:
    """Run an operation across multiple repos concurrently.

    Args:
        repo_names: List of repo names to operate on.
        operation: Function that takes a repo name and returns a RepoOpResult.
        max_workers: Maximum number of concurrent threads.
        fail_fast: If True, stop after first failure.

    Returns:
        BulkResult with all repo outcomes.
    """
    result = BulkResult()

    if not repo_names:
        return result

    with ThreadPoolExecutor(max_workers=min(max_workers, len(repo_names))) as pool:
        futures = {
            pool.submit(operation, name): name
            for name in repo_names
        }

        for future in as_completed(futures):
            repo_name = futures[future]
            try:
                op_result = future.result()
                result.results.append(op_result)
            except Exception as e:
                result.results.append(RepoOpResult(
                    repo=repo_name,
                    status=OpStatus.FAILED,
                    message=str(e),
                ))

            if fail_fast and result.failed:
                # Cancel remaining futures
                for f in futures:
                    f.cancel()
                break

    # Sort results by repo name for consistent output
    result.results.sort(key=lambda r: r.repo)
    return result
