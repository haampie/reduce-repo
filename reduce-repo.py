#!/usr/bin/env python3
"""reduce-repo.py — reduce a git repository to a minimal bug reproduction.

Phase 1: delete entire tracked files
Phase 2: delete lines within remaining files

Each successful reduction is committed so the git history records progress.
"""

import argparse
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path


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
        next_index = self.index + self.chunk
        if next_index < self.instances:
            return BinaryState(
                instances=self.instances, chunk=self.chunk, index=next_index
            )
        # wrap around: halve the chunk
        next_chunk = self.chunk // 2
        if next_chunk == 0:
            return None
        return BinaryState(instances=self.instances, chunk=next_chunk, index=0)


def _state_iter(state: "BinaryState | None"):
    """Yield every BinaryState in sequence.

    The sequence tries the largest chunks first, then progressively halves:
      [0:N], [0:N/2], [N/2:N], [0:N/4], [N/4:N/2], [N/2:3N/4], [3N/4:N], ...

    With N persistent workers pulling from this iterator, round 1 launches:
      worker 0 → chunk=all, worker 1 → chunk=first-half,
      worker 2 → chunk=second-half, worker 3 → chunk=first-quarter.
    """
    s = state
    while s is not None:
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
    return sorted(f for f in result.stdout.splitlines() if f)


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
    subprocess.run(["git", "clean", "-fd", "."], cwd=wt, capture_output=True)
    subprocess.run(["git", "checkout", "HEAD", "--", "."], cwd=wt, capture_output=True)


def commit_change(wt: Path, msg: str) -> str:
    """Stage all changes and commit. Returns the new commit hash."""
    git(["add", "-A"], cwd=wt)
    git(["commit", "-m", msg], cwd=wt)
    result = git(["rev-parse", "HEAD"], cwd=wt)
    return result.stdout.strip()


def sync_worktrees_to_commit(commit: str, worktrees: list[Path]) -> None:
    """Reset all worktrees to a specific commit."""
    for wt in worktrees:
        subprocess.run(
            ["git", "reset", "--hard", commit],
            cwd=wt,
            capture_output=True,
        )


# ---------------------------------------------------------------------------
# Interestingness test runner
# ---------------------------------------------------------------------------


def run_test(cmd: str, wt: Path, stop_event: threading.Event) -> bool:
    """Run the interestingness test in wt. Return True if interesting (exit 0).

    Polls stop_event every 50ms and terminates the subprocess early if set.
    """
    proc = subprocess.Popen(
        cmd,
        shell=True,
        cwd=wt,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
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
    return proc.returncode == 0


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

    while True:
        state = BinaryState.create(len(files))
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
        else:
            break

    return files


# ---------------------------------------------------------------------------
# Phase 2: reduce lines
# ---------------------------------------------------------------------------


def reduce_lines(files: list[str], repo: Path, worktrees: list[Path], cmd: str) -> None:
    """For each file, delete lines until no further reduction is possible."""
    n = len(worktrees)

    for filepath in files:
        ref_wt = worktrees[0]
        full_path = ref_wt / filepath

        if not full_path.exists() or not is_text_file(full_path):
            continue

        print(f"[*] Reducing lines in {filepath}")

        while True:
            # Re-read lines from the reference worktree (always at HEAD)
            lines = full_path.read_text().splitlines(keepends=True)

            state = BinaryState.create(len(lines))
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

            def worker(wt, i):
                while not stop_event.is_set():
                    s = _next()
                    if s is None:
                        return
                    print(f"  [worker {i}] remove {s.end() - s.index} line(s) at [{s.index}:{s.end()}]")
                    remaining = lines[: s.index] + lines[s.end() :]
                    (wt / filepath).write_text("".join(remaining))
                    interesting = run_test(cmd, wt, stop_event)
                    if interesting:
                        with results_lock:
                            if not stop_event.is_set():
                                stop_event.set()
                                results.append((wt, s, remaining))
                                return
                    restore_worktree(wt)

            threads = [threading.Thread(target=worker, args=(worktrees[i], i)) for i in range(n)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            if results:
                winning_wt, winning_state, remaining = results[0]
                removed = len(lines) - len(remaining)
                msg = f"reduce: {filepath}: remove {removed} line(s) at [{winning_state.index}:{winning_state.end()}]"
                print(f"[+] {msg}")
                commit_hash = commit_change(winning_wt, msg)
                sync_worktrees_to_commit(commit_hash, [repo] + worktrees)
            else:
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
            print("\n[*] Phase 1: reducing files...")
            files = reduce_files(files, repo, worktrees, cmd)
            print(f"[*] Phase 1 done. Files remaining: {len(files)}")

            # Phase 2: line deletion
            print("\n[*] Phase 2: reducing lines...")
            reduce_lines(files, repo, worktrees, cmd)
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
