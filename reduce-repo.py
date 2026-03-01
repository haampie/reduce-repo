#!/usr/bin/env python3
"""reduce-repo.py — reduce a git repository to a minimal bug reproduction.

Phase 1: delete entire tracked files
Phase 2: delete lines within remaining files

Each successful reduction is committed so the git history records progress.
"""

import argparse
import random
import secrets
import shutil
import subprocess
import sys
import tempfile
import queue
import threading
from dataclasses import dataclass
from itertools import groupby
from pathlib import Path
import time


# ---------------------------------------------------------------------------
# BinaryState — pure-functional, frozen, thread-safe
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BinaryState:
    instances: int
    chunk: int
    index: int

    @staticmethod
    def create(instances: int) -> "BinaryState | None":
        if instances == 0:
            return None
        return BinaryState(instances=instances, chunk=instances, index=0)

    def end(self) -> int:
        return min(self.index + self.chunk, self.instances)

    def advance(self) -> "BinaryState | None":
        # Walk backwards within the current chunk size (end → start).
        if self.index >= self.chunk:
            return BinaryState(
                instances=self.instances, chunk=self.chunk, index=self.index - self.chunk
            )
        # Exhausted this chunk size: halve and start from the last chunk.
        next_chunk = self.chunk // 2
        if next_chunk == 0:
            return None
        last_index = ((self.instances - 1) // next_chunk) * next_chunk
        return BinaryState(instances=self.instances, chunk=next_chunk, index=last_index)


def _state_iter(state: "BinaryState | None"):
    """Yield every BinaryState in sequence.

    The sequence tries the largest chunks first, then progressively halves.
    Within each chunk size the chunks are tried from the end backwards:
      [0:N], [N/2:N], [0:N/2], [3N/4:N], [N/2:3N/4], [N/4:N/2], [0:N/4], ...

    With N persistent workers pulling from this iterator, round 1 launches:
      worker 0 → chunk=all, worker 1 → chunk=last-half,
      worker 2 → chunk=first-half, worker 3 → chunk=last-quarter.
    """
    s = state
    while s is not None:
        # Skip remainder chunks smaller than half the nominal chunk size.
        # E.g. with 522 files and chunk=128 the tail [512:522] (10 items) is
        # skipped; a successful deletion there would wastefully restart from
        # chunk=1024 rather than continuing at the current granularity.
        if (s.end() - s.index) * 2 >= s.chunk:
            yield s
        s = s.advance()


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def git(
    args: list[str], cwd: Path, check: bool = True, capture: bool = True
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=capture,
        text=True,
        check=check,
    )


def git_ls_files(repo: Path) -> list[str]:
    result = git(["ls-files"], cwd=repo)
    # Sort so that files in the same directory are contiguous. Binary-search
    # chunks then tend to cover whole directories, making early deletions more
    # likely to be interesting.
    return sorted(f for f in result.stdout.splitlines() if f and Path(f).name != ".gitignore")


def create_worktrees(repo: Path, n: int, parent: Path) -> list[Path]:
    worktrees: list[Path] = []
    for i in range(n):
        wt_dir = parent / f"wt{i}"
        # wt_dir must NOT exist; git worktree add creates it
        git(["worktree", "add", "--detach", str(wt_dir)], cwd=repo)
        worktrees.append(wt_dir)
    return worktrees


def remove_worktrees(repo: Path, worktrees: list[Path]) -> None:
    for wt in worktrees:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(wt)],
            cwd=repo,
            capture_output=True,
        )
    subprocess.run(["git", "worktree", "prune"], cwd=repo, capture_output=True)


def restore_worktree(wt: Path) -> None:
    """Bring worktree to exact match of HEAD — remove test artifacts, restore tracked files."""
    subprocess.run(["git", "clean", "-fdx", "."], cwd=wt, capture_output=True)
    subprocess.run(["git", "checkout", "HEAD", "--", "."], cwd=wt, capture_output=True)


def commit_change(wt: Path, msg: str) -> str:
    """Commit what is already staged. Returns the new commit hash."""
    git(["commit", "-m", msg], cwd=wt)
    result = git(["rev-parse", "HEAD"], cwd=wt)
    return result.stdout.strip()


def sync_worktrees_to_commit(commit: str, worktrees: list[Path]) -> None:
    """Reset all worktrees to a specific commit."""
    for wt in worktrees:
        subprocess.run(["git", "reset", "--hard", commit], cwd=wt, capture_output=True)
        subprocess.run(["git", "clean", "-fdx", "."], cwd=wt, capture_output=True)


# ---------------------------------------------------------------------------
# Interestingness test runner
# ---------------------------------------------------------------------------


def run_test(cmd: str, wt: Path, stop_event: threading.Event) -> bool:
    """Run the interestingness test in wt. Return True if interesting (exit 0).

    Polls stop_event every 50ms and terminates the subprocess early if set.
    """
    with tempfile.TemporaryFile(mode='w+') as stdout_f, tempfile.TemporaryFile(mode='w+') as stderr_f:
        start = time.monotonic()  # ensure monotonic time for timeout
        proc = subprocess.Popen(
            cmd,
            shell=True,
            cwd=wt,
            stdout=stdout_f,  # Capture stdout
            stderr=stderr_f,  # Capture stderr
            text=True,        # Treat output as text
            errors="replace", # Don't crash on decoding errors
        )
        while True:
            try:
                proc.wait(timeout=0.05)
                break
            except subprocess.TimeoutExpired:
                if stop_event.is_set():
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    return False

        if proc.returncode == 0:
            # Rewind files to read captured output
            stdout_f.seek(0)
            stderr_f.seek(0)
            out = stdout_f.read().strip()
            err = stderr_f.read().strip()

            # Build a debug message
            msg = [f"\n--- [SUCCESS] Test passed in {wt.name} in {time.monotonic() - start:.2f}s ---"]
            if out:
                msg.append(f"STDOUT:\n{out}")
            if err:
                msg.append(f"STDERR:\n{err}")
            msg.append("------------------------------------------\n")
            
            # Print strictly one message at a time to avoid garbled text
            print("\n".join(msg), flush=True)
            return True
        return False


# ---------------------------------------------------------------------------
# Binary file detection
# ---------------------------------------------------------------------------


def is_text_file(path: Path) -> bool:
    """Return True if the file looks like a text file (not binary)."""
    try:
        chunk = path.read_bytes()[:8192]
        if b"\x00" in chunk:
            return False
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Command resolution
# ---------------------------------------------------------------------------


def resolve_command(cmd: str, repo: Path, tempdir: Path) -> str:
    """Absolutize the script path in cmd and, if it lives inside the repo, copy it out.

    Relative paths are resolved against the caller's CWD (not the repo), so that
    e.g. `./interesting.sh` works even when the test later runs from a worktree.
    """
    first = cmd.split()[0]
    # Resolve relative to caller's CWD so the path survives cwd changes
    if Path(first).is_absolute():
        candidate = Path(first).resolve()
    else:
        candidate = (Path.cwd() / first).resolve()

    if not candidate.is_file():
        return cmd  # not a file path (shell builtin, env var, etc.)

    try:
        is_inside = candidate.is_relative_to(repo.resolve())
    except ValueError:
        is_inside = False

    if is_inside:
        # Copy outside the repo so Phase 1 can't delete it
        safe_copy = tempdir / "interestingness-test.sh"
        shutil.copy2(candidate, safe_copy)
        safe_copy.chmod(safe_copy.stat().st_mode | 0o111)
        return str(safe_copy) + cmd[len(first) :]

    # Outside the repo: just ensure the path is absolute
    return str(candidate) + cmd[len(first) :]


# ---------------------------------------------------------------------------
# Phase 1: reduce files
# ---------------------------------------------------------------------------


def reduce_files(files: list[str], repo: Path, worktrees: list[Path], cmd: str) -> list[str]:
    """Delete whole files until no further reduction is possible. Returns reduced file list."""
    n = len(worktrees)
    next_chunk: int | None = None

    while True:
        if next_chunk is None:
            state = BinaryState.create(len(files))
        else:
            state = BinaryState(
                instances=len(files),
                chunk=next_chunk,
                index=((len(files) - 1) // next_chunk) * next_chunk,
            )
        if state is None:
            break

        _it = _state_iter(state)
        _it_lock = threading.Lock()

        def _next():
            with _it_lock:
                return next(_it, None)

        stop_event = threading.Event()
        results: list = []
        results_lock = threading.Lock()

        files.reverse()  # alternate each round
        # and do a random rotation
        rot = secrets.randbelow(len(files))
        files = files[rot:] + files[:rot]

        def worker(wt, i):
            while not stop_event.is_set():
                s = _next()
                if s is None:
                    return
                to_delete = files[s.index : s.end()]
                names = ", ".join(to_delete[:3]) + ("..." if len(to_delete) > 3 else "")
                print(f"  [worker {i}] delete {len(to_delete)} file(s): {names}")
                for f in to_delete:
                    (wt / f).unlink(missing_ok=True)
                git(["rm", "--cached", "--"] + to_delete, cwd=wt)
                interesting = run_test(cmd, wt, stop_event)
                if interesting:
                    with results_lock:
                        if not stop_event.is_set():
                            stop_event.set()
                            results.append((wt, to_delete))
                            return  # leave dirty; caller commits
                restore_worktree(wt)

        threads = [threading.Thread(target=worker, args=(worktrees[i], i)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        if results:
            winning_wt, deleted = results[0]
            files = [f for f in files if f not in set(deleted)]
            msg = f"reduce: delete {len(deleted)} file(s): {', '.join(deleted[:3])}{'...' if len(deleted) > 3 else ''}"
            print(f"[+] {msg}")
            commit_hash = commit_change(winning_wt, msg)
            sync_worktrees_to_commit(commit_hash, [repo] + worktrees)
            next_chunk = None
        else:
            if next_chunk is not None:
                # Started mid-sequence; retry from top before declaring done.
                # The file list changed, so larger chunks might now succeed.
                next_chunk = None
                continue
            break

    return files


# ---------------------------------------------------------------------------
# Phase 2: reduce lines
# ---------------------------------------------------------------------------


def reduce_lines(files: list[str], repo: Path, worktrees: list[Path], cmd: str, restart_after: int = 8, jitter: float = 0.05) -> None:
    """Delete lines within files until no further reduction is possible.

    Uses depth-major ordering: all files are processed at depth 0 (whole-file
    removal), then depth 1 (half-file), etc. Within each depth files are sorted
    largest-first.

    Within each depth level, n-1 producer threads test candidate deletions
    independently and push passing patches to a shared queue. One applier thread
    continuously drains the queue, combines all pending patches, and commits if
    the combined test passes. On failure it discards the oldest half of its
    accumulated patches and retries. Producers sync to the latest commit before
    each task so stale patches are self-correcting.

    Already-failed (filepath, start, end) triples are remembered in failed_pairs
    and skipped in subsequent passes. On success, failed_pairs entries for the
    changed file(s) are cleared so their new content gets a fresh look. After
    restart_after consecutive successes the entire set is cleared, allowing a
    coarse-level sweep to find chunks that are newly removable.
    """
    n = len(worktrees)
    ref_wt = worktrees[0]
    apply_wt = worktrees[-1]       # dedicated applier worktree
    producer_wts = worktrees[:-1]  # n−1 producer worktrees
    n_producers = len(producer_wts)
    assert n_producers >= 1, "Need at least 2 worktrees (-n >= 2)"
    failed_pairs: set[tuple[str, int, int]] = set()
    failed_pairs_lock = threading.Lock()
    successes_since_restart = 0
    latest_commit: list[str] = [git(["rev-parse", "HEAD"], cwd=repo).stdout.strip()]
    latest_commit_lock = threading.Lock()

    while True:
        # Build per-file line lists (skip empty / binary / missing)
        file_lines: dict[str, list[str]] = {}
        for filepath in files:
            full_path = ref_wt / filepath
            if not full_path.exists() or not is_text_file(full_path):
                continue
            try:
                lines = full_path.read_text().splitlines(keepends=True)
            except UnicodeDecodeError:
                continue
            if lines:
                file_lines[filepath] = lines

        if not file_lines:
            break

        # Blank-line pass: delete all blank/whitespace-only lines at once.
        blank_deletions: dict[str, list[int]] = {}
        for filepath, lines in file_lines.items():
            idxs = [i for i, ln in enumerate(lines) if not ln.strip()]
            if idxs:
                blank_deletions[filepath] = idxs

        if blank_deletions:
            for fp, idxs in blank_deletions.items():
                idx_set = set(idxs)
                new_lines = [ln for i, ln in enumerate(file_lines[fp]) if i not in idx_set]
                (apply_wt / fp).write_text("".join(new_lines))
                git(["add", "--", fp], cwd=apply_wt)
            total = sum(len(v) for v in blank_deletions.values())
            print(f"  [blank pass] {total} blank line(s) across {len(blank_deletions)} file(s)")
            dummy = threading.Event()
            if run_test(cmd, apply_wt, dummy):
                msg = f"reduce: remove {total} blank line(s)"
                print(f"[+] {msg}")
                commit_hash = commit_change(apply_wt, msg)
                sync_worktrees_to_commit(commit_hash, [repo] + worktrees)
                with latest_commit_lock:
                    latest_commit[0] = commit_hash
                changed = set(blank_deletions)
                failed_pairs -= {k for k in failed_pairs if k[0] in changed}
                successes_since_restart += 1
                if successes_since_restart >= restart_after:
                    successes_since_restart = 0
                    failed_pairs.clear()
                continue  # restart outer loop; re-read file_lines
            else:
                restore_worktree(apply_wt)

        # Sort files largest-first so the most impactful deletions appear first.
        sorted_files = sorted(file_lines, key=lambda fp: len(file_lines[fp]), reverse=True)

        # Build per-file depth groups: list of [states] per depth level.
        file_depth_groups: dict[str, list[list]] = {}
        for filepath in sorted_files:
            lines = file_lines[filepath]
            s0 = BinaryState.create(len(lines))
            s0 = s0.advance() if s0 is not None else None  # start at N/2, not N
            groups = [
                list(grp)
                for _, grp in groupby(
                    _state_iter(s0), key=lambda s: s.chunk
                )
            ]
            file_depth_groups[filepath] = groups

        found = False
        for depth in range(64):
            # Build task list for this depth across all files (largest-first),
            # skipping (filepath, start, end) triples that already failed.
            task_list = []
            has_any_at_depth = False
            for filepath in sorted_files:
                groups = file_depth_groups[filepath]
                if depth < len(groups):
                    has_any_at_depth = True
                    lines = file_lines[filepath]
                    for s in groups[depth]:
                        raw_end = s.end()
                        chunk_size = raw_end - s.index
                        if jitter > 0 and chunk_size > 1:
                            delta = round(chunk_size * random.uniform(-jitter, jitter))
                            end = max(s.index + 1, min(len(lines), raw_end + delta))
                        else:
                            end = raw_end
                        # heuristic, class Foo is typically in the first 10 lines.
                        if s.index == 0 and end > 10:
                            continue
                        key = (filepath, s.index, end)
                        if key not in failed_pairs:
                            task_list.append((filepath, s.index, end, lines))

            if not has_any_at_depth:
                break  # all depths exhausted — nothing more to try

            if not task_list:
                continue  # every task at this depth already failed; try next depth

            task_iter = iter(task_list)
            task_iter_lock = threading.Lock()

            def _next_task():
                with task_iter_lock:
                    return next(task_iter, None)

            patch_queue: queue.Queue = queue.Queue()
            producers_done = threading.Event()
            depth_applier_commits: list[str] = []
            depth_changed_files: set[str] = set()

            def producer_worker(wt, i):
                no_cancel = threading.Event()  # producers never cancel each other
                my_commit: str | None = None
                while True:
                    # Sync worktree to latest commit before each task
                    with latest_commit_lock:
                        lc = latest_commit[0]
                    if lc != my_commit:
                        if my_commit is not None:
                            subprocess.run(["git", "reset", "--hard", lc], cwd=wt, capture_output=True)
                        my_commit = lc
                    subprocess.run(["git", "clean", "-fdx", "."], cwd=wt, capture_output=True)

                    task = _next_task()
                    if task is None:
                        return
                    filepath, start, end, lines = task
                    remaining = lines[:start] + lines[end:]
                    removed = end - start
                    print(f"  [worker {i}] {filepath}: remove {removed} line(s) [{start}:{end}]")
                    (wt / filepath).write_text("".join(remaining))
                    if run_test(cmd, wt, no_cancel):
                        patch_queue.put((filepath, start, end, removed, lines))
                    else:
                        with failed_pairs_lock:
                            failed_pairs.add((filepath, start, end))
                    restore_worktree(wt)

            def applier_worker():
                pending: list = []  # (filepath, start, end, removed, lines)
                dummy = threading.Event()
                while True:
                    # Drain all available patches
                    try:
                        while True:
                            pending.append(patch_queue.get_nowait())
                    except queue.Empty:
                        pass

                    if not pending:
                        if producers_done.is_set() and patch_queue.empty():
                            break
                        time.sleep(0.05)
                        continue

                    # Combine patches: group by file, apply all deletions per file
                    # using the lines snapshot embedded in each patch.
                    file_dels: dict[str, tuple[list[str], list[tuple[int, int]]]] = {}
                    for fp, s, e, _, lines in pending:
                        if fp not in file_dels:
                            file_dels[fp] = (lines, [])
                        file_dels[fp][1].append((s, e))

                    for fp, (lines, dels) in file_dels.items():
                        sorted_dels = sorted(dels, key=lambda x: x[0])
                        new_lines: list[str] = []
                        prev = 0
                        for s, e in sorted_dels:
                            if s > prev:
                                new_lines.extend(lines[prev:s])
                            prev = max(prev, e)
                        new_lines.extend(lines[prev:])
                        (apply_wt / fp).write_text("".join(new_lines))
                        git(["add", "--", fp], cwd=apply_wt)

                    if run_test(cmd, apply_wt, dummy):
                        total = sum(r[3] for r in pending)
                        parts = "; ".join(f"{fp} [{s}:{e}]" for fp, s, e, _, _ in pending)
                        msg = f"reduce: {total} line(s) across {len(pending)} regions: {parts}"
                        print(f"[+] {msg}")
                        commit_hash = commit_change(apply_wt, msg)
                        depth_applier_commits.append(commit_hash)
                        depth_changed_files.update(fp for fp, *_ in pending)
                        with latest_commit_lock:
                            latest_commit[0] = commit_hash
                        subprocess.run(["git", "reset", "--hard", commit_hash], cwd=repo, capture_output=True)
                        pending = []
                    else:
                        restore_worktree(apply_wt)
                        n_discard = max(1, len(pending) // 2)
                        pending = pending[n_discard:]

            producer_threads = [
                threading.Thread(target=producer_worker, args=(producer_wts[i], i))
                for i in range(n_producers)
            ]
            applier_thread = threading.Thread(target=applier_worker)

            for t in producer_threads:
                t.start()
            applier_thread.start()
            for t in producer_threads:
                t.join()
            producers_done.set()
            applier_thread.join()

            if depth_applier_commits:
                last_commit = depth_applier_commits[-1]
                sync_worktrees_to_commit(last_commit, [repo] + worktrees)
                with latest_commit_lock:
                    latest_commit[0] = last_commit
                failed_pairs -= {k for k in failed_pairs if k[0] in depth_changed_files}
                successes_since_restart += len(depth_applier_commits)
                if successes_since_restart >= restart_after:
                    successes_since_restart = 0
                    failed_pairs.clear()
                found = True
                break

        if not found:
            break


# ---------------------------------------------------------------------------
# Sanity checks
# ---------------------------------------------------------------------------


def sanity_check(repo: Path, cmd: str) -> None:
    # 1. Is it a git repo?
    r = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=repo,
        capture_output=True,
    )
    if r.returncode != 0:
        sys.exit(f"error: {repo} is not a git repository")

    # 2. Clean working tree?
    r = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if r.stdout.strip():
        sys.exit("error: working tree is not clean; commit or stash changes first")

    # 3. Test passes at HEAD?
    print("[*] Running sanity check (test must pass at HEAD)...")
    dummy_event = threading.Event()
    if not run_test(cmd, repo, dummy_event):
        sys.exit(
            "error: interestingness test does not pass at HEAD (exit non-0); nothing to reduce"
        )

    print("[*] Sanity check passed.")
    subprocess.run(["git", "clean", "-fd", "."], cwd=repo, capture_output=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reduce a git repository to a minimal bug reproduction."
    )
    parser.add_argument(
        "-n",
        type=int,
        default=4,
        metavar="N",
        help="number of parallel workers (default: 4)",
    )
    parser.add_argument(
        "-c",
        "--command",
        required=True,
        metavar="COMMAND",
        help="interestingness test command (exit 0 = interesting)",
    )
    parser.add_argument(
        "--lines-only",
        action="store_true",
        help="skip Phase 1 (file deletion) and go straight to line reduction",
    )
    parser.add_argument(
        "--restart-after",
        type=int,
        default=8,
        metavar="N",
        help="restart to coarsest chunks after N successful reductions (default: 8)",
    )
    parser.add_argument(
        "--jitter",
        type=float,
        default=0.05,
        metavar="F",
        help="randomize each chunk end by ±F fraction of chunk size (default: 0.05)",
    )
    parser.add_argument(
        "repo",
        metavar="REPO_PATH",
        type=Path,
        help="path to the git repository to reduce",
    )
    args = parser.parse_args()

    repo = args.repo.resolve()
    n = args.n
    cmd_raw = args.command

    # Create a parent temp dir for worktrees and resolved scripts
    parent = Path(tempfile.mkdtemp(prefix="reduce-repo-"))
    print(f"[*] Working directory: {parent}")

    try:
        # Resolve the command before sanity check so it works even if the
        # script is inside the repo
        cmd = resolve_command(cmd_raw, repo, parent)
        if cmd != cmd_raw:
            print(f"[*] Test script copied outside repo: {cmd.split()[0]}")

        sanity_check(repo, cmd)

        branch = f"reduce-{secrets.token_hex(4)}"
        git(["checkout", "-b", branch], cwd=repo)
        print(f"[*] Created branch: {branch}")

        files = git_ls_files(repo)
        print(f"[*] Tracked files: {len(files)}")

        print(f"[*] Creating {n} git worktrees...")
        worktrees = create_worktrees(repo, n, parent)

        try:
            # Phase 1: file deletion
            if args.lines_only:
                print("\n[*] Phase 1 skipped (--lines-only).")
            else:
                print("\n[*] Phase 1: reducing files...")
                files = reduce_files(files, repo, worktrees, cmd)
                print(f"[*] Phase 1 done. Files remaining: {len(files)}")

            # Phase 2: line deletion
            print("\n[*] Phase 2: reducing lines...")
            reduce_lines(files, repo, worktrees, cmd, restart_after=args.restart_after, jitter=args.jitter)
            print("[*] Phase 2 done.")

        finally:
            print(f"\n[*] Removing worktrees...")
            remove_worktrees(repo, worktrees)

        print("\n[*] Reduction complete. Commits:")
        subprocess.run(["git", "log", "--oneline", "-20"], cwd=repo)

    finally:
        shutil.rmtree(parent, ignore_errors=True)


if __name__ == "__main__":
    main()
