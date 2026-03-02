#!/usr/bin/env python3
"""reduce-repo.py - reduce a git repository to a minimal bug reproduction.

Phase 1: delete entire tracked files
Phase 2: delete lines within remaining files

Each successful reduction is committed so the git history records progress.
"""

import argparse
import ast
import itertools
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

#: Number of consecutive successful reductions before restarting with larger chunk sizes.
RETRY_AFTER = 8

#: Min chunk size for function/call deletions. Smaller chunks are often overhead
MIN_FUNC_CHUNK = 4


# ---------------------------------------------------------------------------
# BinaryState - pure-functional, frozen, thread-safe
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
        # Walk backwards within the current chunk size (end -> start).
        if self.index >= self.chunk:
            return BinaryState(
                instances=self.instances,
                chunk=self.chunk,
                index=self.index - self.chunk,
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
      worker 0 -> chunk=all, worker 1 -> chunk=last-half,
      worker 2 -> chunk=first-half, worker 3 -> chunk=last-quarter.
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
    return sorted(
        f for f in result.stdout.splitlines() if f and Path(f).name != ".gitignore"
    )


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
    """Bring worktree to exact match of HEAD - remove test artifacts, restore tracked files."""
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


def run_test(
    cmd: str, wt: Path, stop_event: threading.Event, verbose: bool = False
) -> bool:
    """Run the interestingness test in wt. Return True if interesting (exit 0).

    Polls stop_event every 50ms and terminates the subprocess early if set.
    """
    with (
        tempfile.TemporaryFile(mode="w+b") as stdout_f,
        tempfile.TemporaryFile(mode="w+b") as stderr_f,
    ):
        start = time.monotonic()  # ensure monotonic time for timeout
        proc = subprocess.Popen(
            cmd,
            shell=True,
            cwd=wt,
            stdout=stdout_f,  # Capture stdout
            stderr=stderr_f,  # Capture stderr
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

        passed = proc.returncode == 0

        if passed or verbose:
            # Rewind files to read captured output
            stdout_f.seek(0)
            stderr_f.seek(0)
            out = stdout_f.read().decode(errors="replace").strip()
            err = stderr_f.read().decode(errors="replace").strip()

            # Build a debug message
            msg = []
            if out:
                msg.append(f"STDOUT:\n{out}\n")
            if err:
                msg.append(f"STDERR:\n{err}\n")
            msg.append(
                f"\n--- [{'SUCCESS' if passed else 'FAILURE'}] Test passed in {wt.name} in {time.monotonic() - start:.2f}s ---"
            )

            # Print strictly one message at a time to avoid garbled text
            print("\n".join(msg), flush=True)
        return passed


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


def _apply_deletions(lines: list[str], intervals: list[tuple[int, int]]) -> list[str]:
    """Return lines with given (start, end) intervals removed.

    Intervals are 0-based, end-exclusive. Out-of-bounds ends are clipped.
    Overlapping and adjacent intervals are merged. Input need not be sorted.
    """
    sorted_ivs = sorted(intervals)
    merged: list[tuple[int, int]] = []
    for s, e in sorted_ivs:
        e = min(e, len(lines))
        if s >= e:
            continue
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    result: list[str] = []
    prev = 0
    for s, e in merged:
        result.extend(lines[prev:s])
        prev = e
    result.extend(lines[prev:])
    return result


# ---------------------------------------------------------------------------
# Phase 1: reduce files, functions and calls
# ---------------------------------------------------------------------------


def _collect_funcs_and_calls(
    tree: ast.AST, filepath: str, lines_list: list[str]
) -> tuple[list[tuple[str, int, int]], list[tuple[str, int, int]]]:
    """Return (function_defs, function_calls).

    Traverses the AST with a stack. Does NOT recurse into FunctionDef/
    AsyncFunctionDef bodies or into ast.Expr(Call) subtrees, so only
    top-level (outer) definitions and calls are collected.

    function_defs: (filepath, start0, end1) including decorators and trailing blank lines.
    function_calls: (filepath, start0, end1) for bare call statements.
    """
    function_defs: list[tuple[str, int, int]] = []
    function_calls: list[tuple[str, int, int]] = []
    stack = list(ast.iter_child_nodes(tree))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            first_line = (
                node.decorator_list[0].lineno if node.decorator_list else node.lineno
            )
            end = node.end_lineno
            while end < len(lines_list) and not lines_list[end].strip():
                end += 1
            function_defs.append((filepath, first_line - 1, end))
            # Do not recurse into function body
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            function_calls.append((filepath, node.lineno - 1, node.end_lineno))
            # Do not recurse into the call expression
        else:
            stack.extend(ast.iter_child_nodes(node))
    return function_defs, function_calls


def reduce_files_and_functions(
    files: list[str], repo: Path, worktrees: list[Path], cmd: str
) -> None:
    """Delete function definitions and bare call statements until no further reduction is possible."""
    apply_wt = worktrees[-1]
    producer_wts = worktrees[:-1]
    n_producers = len(producer_wts)
    assert n_producers >= 1, "Need at least 2 worktrees (-n >= 2)"
    ref_wt = worktrees[0]

    next_start_chunk_files = None
    next_start_chunk_funcs = None

    def _apply_deletions_to_wt(
        wt: Path, files_to_del: list[str], funcs_to_del: list[tuple[str, str, int, int]]
    ) -> bool:
        """Apply both file and function deletions; return True if changed."""
        changed = False

        if files_to_del:
            for f in files_to_del:
                (wt / f).unlink(missing_ok=True)
            git(["rm", "--cached", "--ignore-unmatch", "--"] + files_to_del, cwd=wt)
            changed = True

        if funcs_to_del:
            by_file: dict[str, list[tuple[int, int]]] = {}
            for tag, fp, s, e in funcs_to_del:
                if fp in files_to_del:
                    continue  # skip modifying a file that we are deleting entirely
                by_file.setdefault(fp, []).append((s, e))

            for fp, intervals in by_file.items():
                full_path = wt / fp
                if not full_path.exists():
                    continue
                lines = full_path.read_text(errors="ignore").splitlines(keepends=True)
                new_lines = _apply_deletions(lines, intervals)
                if new_lines == lines:
                    continue
                full_path.write_text("".join(new_lines))
                git(["add", "--", fp], cwd=wt)
                changed = True

        return changed

    def _get_states(N: int, next_sc: int | None) -> list[BinaryState]:
        if N == 0:
            return []
        sc = min(next_sc, N) if next_sc else N
        if sc >= N:
            state = BinaryState.create(N)
        else:
            last_idx = ((N - 1) // sc) * sc
            state = BinaryState(instances=N, chunk=sc, index=last_idx)
        if state is None:
            return []
        return list(_state_iter(state))

    while True:
        # Rotate files for fairness round-over-round
        if files:
            files.reverse()
            rot = secrets.randbelow(len(files))
            files = files[rot:] + files[:rot]

        current_files = []
        all_defs = []
        all_calls = []

        # Re-parse existing ASTs on remaining files
        for filepath in files:
            if not (ref_wt / filepath).exists():
                continue
            current_files.append(filepath)

            if not filepath.endswith(".py"):
                continue

            content = (ref_wt / filepath).read_text(errors="ignore")
            try:
                tree = ast.parse(content)
            except SyntaxError:
                continue

            lines_list = content.splitlines()
            defs, calls = _collect_funcs_and_calls(tree, filepath, lines_list)
            all_defs.extend(defs)
            all_calls.extend(calls)

        # Shuffle the calls in blocks of MIN_FUNC_CHUNK so we're likely to delete from multiple files instead
        # of all calls from the same file.
        block_calls = [
            all_calls[i : i + MIN_FUNC_CHUNK]
            for i in range(0, len(all_calls), MIN_FUNC_CHUNK)
        ]
        random.shuffle(block_calls)
        all_calls = list(itertools.chain.from_iterable(block_calls))

        files = current_files
        funcs_items = [("FUNC", *t) for t in all_defs] + [
            ("CALL", *t) for t in all_calls
        ]

        N_files = len(files)
        N_funcs = len(funcs_items)

        if N_files == 0 and N_funcs == 0:
            break

        start_chunk_files = next_start_chunk_files
        start_chunk_funcs = min(128, next_start_chunk_funcs or 128)

        states_files = _get_states(N_files, start_chunk_files)
        states_funcs = _get_states(N_funcs, start_chunk_funcs)
        states_funcs = [s for s in states_funcs if s.chunk >= MIN_FUNC_CHUNK]

        # Build an interleaved task queue of independent bisection sequences
        # FILE, FILE, FILE, FUNC, FILE, FILE, FILE, FUNC, FILE, FILE, FILE, FUNC, ...
        states_files.reverse()
        states_funcs.reverse()
        tasks = []
        while states_files or states_funcs:
            if states_files:
                tasks.append(("FILE", states_files.pop()))
            if states_files:
                tasks.append(("FILE", states_files.pop()))
            if states_files:
                tasks.append(("FILE", states_files.pop()))
            if states_funcs:
                tasks.append(("FUNC", states_funcs.pop()))

        if not tasks:
            break

        _n_states = len(tasks)
        _it = iter(tasks)
        _dispatched = [0]
        _it_lock = threading.Lock()
        restart_event = threading.Event()

        def _next():
            with _it_lock:
                if restart_event.is_set():
                    return None
                task = next(_it, None)
                if task is not None:
                    _dispatched[0] += 1
                return task

        initial_commit = git(["rev-parse", "HEAD"], cwd=repo).stdout.strip()
        latest_commit: list[str] = [initial_commit]
        latest_commit_lock = threading.Lock()
        patch_queue: queue.Queue = queue.Queue()
        producers_done = threading.Event()

        restart_chunk_size_files = [None]
        restart_chunk_size_funcs = [None]

        def producer_worker(wt, i):
            my_commit: str | None = None
            no_cancel = threading.Event()
            while True:
                with latest_commit_lock:
                    lc = latest_commit[0]
                if lc != my_commit:
                    if my_commit is not None:
                        subprocess.run(
                            ["git", "reset", "--hard", lc], cwd=wt, capture_output=True
                        )
                    my_commit = lc
                subprocess.run(
                    ["git", "clean", "-fdx", "."], cwd=wt, capture_output=True
                )

                task = _next()
                if task is None:
                    return

                kind, s = task
                files_to_del = []
                funcs_to_del = []

                if kind == "FILE":
                    files_to_del = [
                        f for f in files[s.index : s.end()] if (wt / f).exists()
                    ]
                else:
                    funcs_to_del = [
                        x
                        for x in funcs_items[s.index : s.end()]
                        if (wt / x[1]).exists()
                    ]

                if not files_to_del and not funcs_to_del:
                    continue

                pct = _dispatched[0] * 100 // _n_states
                if kind == "FILE":
                    print(f"  [worker {i}] ({pct:3d}%) try {len(files_to_del)} file(s)")
                else:
                    print(
                        f"  [worker {i}] ({pct:3d}%) try {len(funcs_to_del)} func/call(s)"
                    )

                if _apply_deletions_to_wt(wt, files_to_del, funcs_to_del) and run_test(
                    cmd, wt, no_cancel
                ):
                    patch_queue.put(
                        (kind, files_to_del, funcs_to_del, s.chunk, my_commit)
                    )

                restore_worktree(wt)

        def applier_worker():
            # pending contents: (kind, files_to_del, funcs_to_del, chunk_size, commit_hash)
            pending = []
            dummy = threading.Event()

            min_successful_chunk_files = N_files
            min_successful_chunk_funcs = N_funcs
            n_files_applied = 0
            n_funcs_applied = 0

            while True:
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

                # Get current commit
                current_commit = git(["rev-parse", "HEAD"], cwd=apply_wt).stdout.strip()

                # Check if any pending item matches our current commit
                matching_idx = None
                skip_test = False
                for idx, (_, _, _, _, p_commit) in enumerate(pending):
                    if p_commit == current_commit:
                        matching_idx = idx
                        skip_test = True
                        break

                # Define pending_subset: either single matched item or all pending items
                if matching_idx is not None:
                    # Extract the matching item and remove it from pending
                    matched_item = pending.pop(matching_idx)
                    pending_subset = [matched_item]
                else:
                    # Process all pending items
                    pending_subset = pending

                # Accumulate deletions from pending_subset
                files_to_delete = sorted(
                    {
                        f
                        for _, p_files, _, _, _ in pending_subset
                        for f in p_files
                        if (apply_wt / f).exists()
                    }
                )

                funcs_to_delete = []
                seen_funcs = set()
                pending_meta = []  # keep track of successful kinds and their chunk sizes

                for p_kind, _, p_funcs, p_chunk, _ in pending_subset:
                    pending_meta.append((p_kind, p_chunk))
                    for tag, fp, s, e in p_funcs:
                        if (apply_wt / fp).exists() and (fp, s, e) not in seen_funcs:
                            seen_funcs.add((fp, s, e))
                            funcs_to_delete.append((tag, fp, s, e))

                if not files_to_delete and not funcs_to_delete:
                    pending = []
                    continue

                if skip_test:
                    print(
                        f"[*] Applying verified deletions: {len(files_to_delete)} file(s), {len(funcs_to_delete)} func/call(s) (skip test)"
                    )
                else:
                    print(
                        f"[*] Trying pending deletions: {len(files_to_delete)} file(s), {len(funcs_to_delete)} func/call(s)"
                    )

                # Always apply deletions
                applied = _apply_deletions_to_wt(
                    apply_wt, files_to_delete, funcs_to_delete
                )

                # Test only if not skipping
                test_passed = applied and (skip_test or run_test(cmd, apply_wt, dummy))

                if test_passed:
                    parts = []
                    if files_to_delete:
                        parts.append(f"{len(files_to_delete)} file(s)")
                        n_files_applied += 1

                    n_funcs = sum(1 for tag, *_ in funcs_to_delete if tag == "FUNC")
                    n_calls = sum(1 for tag, *_ in funcs_to_delete if tag == "CALL")
                    if n_funcs:
                        parts.append(f"{n_funcs} function(s)")
                    if n_calls:
                        parts.append(f"{n_calls} call(s)")
                    if n_funcs or n_calls:
                        n_funcs_applied += 1

                    all_affected_names = files_to_delete + sorted(
                        {fp for _, fp, _, _ in funcs_to_delete}
                    )
                    seen_names = set()
                    unique_names = [
                        nm
                        for nm in all_affected_names
                        if not (nm in seen_names or seen_names.add(nm))
                    ]
                    names_str = ", ".join(unique_names[:3]) + (
                        "..." if len(unique_names) > 3 else ""
                    )

                    msg = f"reduce: delete {' and '.join(parts)} in {names_str}"
                    print(f"[+] {msg}")

                    commit_hash = commit_change(apply_wt, msg)
                    with latest_commit_lock:
                        latest_commit[0] = commit_hash

                    # Calculate new min chunks by kind
                    f_chunks = [c for k, c in pending_meta if k == "FILE"]
                    if f_chunks:
                        min_successful_chunk_files = min(
                            min_successful_chunk_files, min(f_chunks)
                        )
                        restart_chunk_size_files[0] = min_successful_chunk_files * 2

                    u_chunks = [c for k, c in pending_meta if k == "FUNC"]
                    if u_chunks:
                        min_successful_chunk_funcs = min(
                            min_successful_chunk_funcs, min(u_chunks)
                        )
                        restart_chunk_size_funcs[0] = min_successful_chunk_funcs * 2
                    pending = []

                    # Trigger a restart signal after RETRY_AFTER consecutive successes
                    if n_files_applied >= RETRY_AFTER or n_funcs_applied >= RETRY_AFTER:
                        restart_event.set()
                else:
                    n_discard = max(1, len(pending) // 2)
                    pending = pending[n_discard:]

                restore_worktree(apply_wt)

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

        committed = latest_commit[0] != initial_commit
        if committed:
            sync_worktrees_to_commit(latest_commit[0], [repo] + worktrees)
            if restart_chunk_size_files[0] is not None:
                next_start_chunk_files = restart_chunk_size_files[0]
            if restart_chunk_size_funcs[0] is not None:
                next_start_chunk_funcs = restart_chunk_size_funcs[0]
            continue
        else:
            break

    return files


# ---------------------------------------------------------------------------
# Phase 1.75: reduce empty lines
# ---------------------------------------------------------------------------


def _has_empty_lines(path: Path) -> bool:
    try:
        return any(not l.strip() for l in path.read_text(errors="ignore").splitlines())
    except OSError:
        return False


def _strip_empty_lines(wt: Path, batch: list[str]) -> bool:
    """Remove empty/whitespace-only lines from each file in batch.
    Returns True if any file was changed and staged."""
    changed = False
    for fp in batch:
        full_path = wt / fp
        if not full_path.exists():
            continue
        lines = full_path.read_text(errors="ignore").splitlines(keepends=True)
        new_lines = [l for l in lines if l.strip()]
        if new_lines == lines:
            continue
        full_path.write_text("".join(new_lines))
        git(["add", "--", fp], cwd=wt)
        changed = True
    return changed


def _truncate_files(wt: Path, batch: list[str]) -> bool:
    """Write empty content to each file in batch.
    Returns True if any file was changed and staged."""
    changed = False
    for fp in batch:
        full_path = wt / fp
        if not full_path.exists() or full_path.read_text(errors="ignore") == "":
            continue
        full_path.write_text("")
        git(["add", "--", fp], cwd=wt)
        changed = True
    return changed


def truncate_files(
    files: list[str], repo: Path, worktrees: list[Path], cmd: str
) -> None:
    """Truncate files to empty content until no further reduction is possible."""
    apply_wt = worktrees[-1]
    producer_wts = worktrees[:-1]
    n_producers = len(producer_wts)
    assert n_producers >= 1, "Need at least 2 worktrees (-n >= 2)"
    ref_wt = worktrees[0]

    targets = [
        f for f in files if (ref_wt / f).exists() and (ref_wt / f).stat().st_size > 0
    ]
    state = BinaryState.create(len(targets))
    if state is None:
        return

    _all_states = list(_state_iter(state))
    _n_states = len(_all_states)
    _it = iter(_all_states)
    _dispatched = [0]
    _it_lock = threading.Lock()

    def _next():
        with _it_lock:
            s = next(_it, None)
            if s is not None:
                _dispatched[0] += 1
            return s

    initial_commit = git(["rev-parse", "HEAD"], cwd=repo).stdout.strip()
    latest_commit: list[str] = [initial_commit]
    latest_commit_lock = threading.Lock()
    patch_queue: queue.Queue = queue.Queue()
    producers_done = threading.Event()

    def producer_worker(wt, i):
        my_commit: str | None = None
        no_cancel = threading.Event()
        while True:
            with latest_commit_lock:
                lc = latest_commit[0]
            if lc != my_commit:
                if my_commit is not None:
                    subprocess.run(
                        ["git", "reset", "--hard", lc], cwd=wt, capture_output=True
                    )
                my_commit = lc
            subprocess.run(["git", "clean", "-fdx", "."], cwd=wt, capture_output=True)

            s = _next()
            if s is None:
                return
            batch = [f for f in targets[s.index : s.end()] if (wt / f).exists()]
            if not batch:
                continue
            pct = _dispatched[0] * 100 // _n_states
            print(f"  [worker {i}] ({pct:3d}%) truncate {len(batch)} file(s)")
            if _truncate_files(wt, batch) and run_test(cmd, wt, no_cancel):
                patch_queue.put(batch)
            restore_worktree(wt)

    def applier_worker():
        pending: list[list[str]] = []
        dummy = threading.Event()
        while True:
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

            all_files = sorted(
                {f for batch in pending for f in batch if (apply_wt / f).exists()}
            )
            if not all_files:
                pending = []
                continue

            names = ", ".join(all_files[:3]) + ("..." if len(all_files) > 3 else "")

            if not _truncate_files(apply_wt, all_files):
                pending = []
                continue

            if run_test(cmd, apply_wt, dummy):
                msg = f"reduce: truncate {len(all_files)} file(s): {names}"
                print(f"[+] {msg}")
                commit_hash = commit_change(apply_wt, msg)
                with latest_commit_lock:
                    latest_commit[0] = commit_hash
                pending = []
            else:
                n_discard = max(1, len(pending) // 2)
                pending = pending[n_discard:]
            restore_worktree(apply_wt)

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

    committed = latest_commit[0] != initial_commit
    if committed:
        sync_worktrees_to_commit(latest_commit[0], [repo] + worktrees)


def reduce_empty_lines(
    files: list[str], repo: Path, worktrees: list[Path], cmd: str
) -> None:
    """Remove empty/whitespace-only lines from files until no further reduction is possible."""
    apply_wt = worktrees[-1]
    producer_wts = worktrees[:-1]
    n_producers = len(producer_wts)
    assert n_producers >= 1, "Need at least 2 worktrees (-n >= 2)"
    ref_wt = worktrees[0]

    targets = [
        f for f in files if (ref_wt / f).exists() and _has_empty_lines(ref_wt / f)
    ]
    state = BinaryState.create(len(targets))
    if state is None:
        return

    _all_states = list(_state_iter(state))
    _n_states = len(_all_states)
    _it = iter(_all_states)
    _dispatched = [0]
    _it_lock = threading.Lock()

    def _next():
        with _it_lock:
            s = next(_it, None)
            if s is not None:
                _dispatched[0] += 1
            return s

    initial_commit = git(["rev-parse", "HEAD"], cwd=repo).stdout.strip()
    latest_commit: list[str] = [initial_commit]
    latest_commit_lock = threading.Lock()
    patch_queue: queue.Queue = queue.Queue()
    producers_done = threading.Event()

    def producer_worker(wt, i):
        my_commit: str | None = None
        no_cancel = threading.Event()
        while True:
            with latest_commit_lock:
                lc = latest_commit[0]
            if lc != my_commit:
                if my_commit is not None:
                    subprocess.run(
                        ["git", "reset", "--hard", lc], cwd=wt, capture_output=True
                    )
                my_commit = lc
            subprocess.run(["git", "clean", "-fdx", "."], cwd=wt, capture_output=True)

            s = _next()
            if s is None:
                return
            batch = [f for f in targets[s.index : s.end()] if (wt / f).exists()]
            if not batch:
                continue
            pct = _dispatched[0] * 100 // _n_states
            print(
                f"  [worker {i}] ({pct:3d}%) strip empty lines from {len(batch)} file(s)"
            )
            if _strip_empty_lines(wt, batch) and run_test(cmd, wt, no_cancel):
                patch_queue.put(batch)
            restore_worktree(wt)

    def applier_worker():
        pending: list[list[str]] = []
        dummy = threading.Event()
        while True:
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

            all_files = sorted(
                {f for batch in pending for f in batch if (apply_wt / f).exists()}
            )
            if not all_files:
                pending = []
                continue

            names = ", ".join(all_files[:3]) + ("..." if len(all_files) > 3 else "")

            if _strip_empty_lines(apply_wt, all_files) and run_test(
                cmd, apply_wt, dummy
            ):
                msg = (
                    f"reduce: strip empty lines from {len(all_files)} file(s): {names}"
                )
                print(f"[+] {msg}")
                commit_hash = commit_change(apply_wt, msg)
                with latest_commit_lock:
                    latest_commit[0] = commit_hash
                pending = []
            else:
                n_discard = max(1, len(pending) // 2)
                pending = pending[n_discard:]
            restore_worktree(apply_wt)

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

    committed = latest_commit[0] != initial_commit
    if committed:
        sync_worktrees_to_commit(latest_commit[0], [repo] + worktrees)


# ---------------------------------------------------------------------------
# Phase 2: reduce lines
# ---------------------------------------------------------------------------


def reduce_lines(
    files: list[str],
    repo: Path,
    worktrees: list[Path],
    cmd: str,
    jitter: float = 0.05,
    min_chunk_size: int = 1,
) -> None:
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
    changed file(s) are cleared so their new content gets a fresh look.
    """
    n = len(worktrees)
    ref_wt = worktrees[0]
    apply_wt = worktrees[-1]  # dedicated applier worktree
    producer_wts = worktrees[:-1]  # n-1 producer worktrees
    n_producers = len(producer_wts)
    assert n_producers >= 1, "Need at least 2 worktrees (-n >= 2)"
    failed_pairs: set[tuple[str, int, int]] = set()
    failed_pairs_lock = threading.Lock()
    latest_commit: list[str] = [git(["rev-parse", "HEAD"], cwd=repo).stdout.strip()]
    latest_commit_lock = threading.Lock()

    while True:
        # Build per-file line lists (skip empty / binary / missing)
        file_lines: dict[str, list[str]] = {}
        for filepath in files:
            full_path = ref_wt / filepath
            if not full_path.exists():
                continue
            lines = full_path.read_text(errors="ignore").splitlines(keepends=True)
            if lines:
                file_lines[filepath] = lines

        if not file_lines:
            break

        # Sort files largest-first so the most impactful deletions appear first.
        sorted_files = sorted(
            file_lines, key=lambda fp: len(file_lines[fp]), reverse=True
        )

        # Use the largest file as the reference for chunk sizes so every depth round
        # has a single chunk granularity.  Files smaller than the current chunk are
        # skipped to avoid pointless whole-file attempts on tiny files.
        max_lines = len(file_lines[sorted_files[0]])
        ref_chunk_sizes = [
            grp[0].chunk
            for grp in (
                list(g)
                for _, g in groupby(
                    _state_iter(BinaryState.create(max_lines)), key=lambda s: s.chunk
                )
            )
        ]

        file_depth_groups: dict[str, list[list[BinaryState]]] = {}
        for filepath in sorted_files:
            n = len(file_lines[filepath])
            groups: list[list[BinaryState]] = []
            for chunk_size in ref_chunk_sizes:
                if chunk_size > n:
                    groups.append([])
                    continue
                if chunk_size == 2:
                    # Use stride 1 to cover all adjacent pairs (even- and odd-aligned).
                    # Jitter has no effect at size 2 (round(2*0.05)=0), so without this
                    # odd-aligned pairs like [1:3] are never tried as a unit.
                    last_start = n - 2
                    stride = 1
                else:
                    last_start = ((n - 1) // chunk_size) * chunk_size
                    stride = chunk_size
                states: list[BinaryState] = []
                for k_start in range(last_start, -1, -stride):
                    s = BinaryState(instances=n, chunk=chunk_size, index=k_start)
                    if (
                        s.end() - k_start
                    ) * 2 >= chunk_size:  # same filter as _state_iter
                        states.append(s)
                groups.append(states)
            file_depth_groups[filepath] = groups

        found = False
        for depth in range(len(ref_chunk_sizes)):
            if ref_chunk_sizes[depth] < min_chunk_size:
                break
            # Build task list for this depth across all files (largest-first),
            # skipping (filepath, start, end) triples that already failed.
            task_list = []
            has_any_at_depth = False
            for filepath in sorted_files:
                if not file_depth_groups[filepath][depth]:
                    continue
                has_any_at_depth = True
                lines = file_lines[filepath]
                n = len(lines)
                for s in file_depth_groups[filepath][depth]:
                    raw_end = s.end()
                    chunk_size = raw_end - s.index
                    if jitter > 0 and chunk_size > 1:
                        delta = round(chunk_size * random.uniform(-jitter, jitter))
                        end = max(s.index + 1, min(len(lines), raw_end + delta))
                    else:
                        end = raw_end
                    key = (filepath, s.index, end)
                    if key not in failed_pairs:
                        task_list.append((filepath, s.index, end, lines))

            if not has_any_at_depth:
                continue  # no file has content at this chunk granularity; try next depth

            if not task_list:
                continue  # every task at this depth already failed; try next depth

            _n_tasks = len(task_list)
            _dispatched = [0]
            task_iter = iter(task_list)
            task_iter_lock = threading.Lock()

            def _next_task():
                with task_iter_lock:
                    t = next(task_iter, None)
                    if t is not None:
                        _dispatched[0] += 1
                    return t

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
                            subprocess.run(
                                ["git", "reset", "--hard", lc],
                                cwd=wt,
                                capture_output=True,
                            )
                        my_commit = lc
                    subprocess.run(
                        ["git", "clean", "-fdx", "."], cwd=wt, capture_output=True
                    )

                    task = _next_task()
                    if task is None:
                        return
                    filepath, start, end, lines = task
                    remaining = lines[:start] + lines[end:]
                    removed = end - start
                    pct = _dispatched[0] * 100 // _n_tasks
                    print(
                        f"  [worker {i}] ({pct:3d}%) {filepath}: remove {removed} line(s) [{start}:{end}]"
                    )
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
                        new_lines = _apply_deletions(lines, dels)
                        (apply_wt / fp).write_text("".join(new_lines))
                        git(["add", "--", fp], cwd=apply_wt)

                    if run_test(cmd, apply_wt, dummy):
                        total = sum(r[3] for r in pending)
                        parts = "; ".join(
                            f"{fp} [{s}:{e}]" for fp, s, e, _, _ in pending
                        )
                        msg = f"reduce: {total} line(s) across {len(pending)} regions: {parts}"
                        print(f"[+] {msg}")
                        commit_hash = commit_change(apply_wt, msg)
                        depth_applier_commits.append(commit_hash)
                        depth_changed_files.update(fp for fp, *_ in pending)
                        with latest_commit_lock:
                            latest_commit[0] = commit_hash
                        pending = []
                    else:
                        n_discard = max(1, len(pending) // 2)
                        pending = pending[n_discard:]
                    restore_worktree(apply_wt)

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
    if not run_test(cmd, repo, dummy_event, verbose=True):
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
        "--jitter",
        type=float,
        default=0.05,
        metavar="F",
        help="randomize each chunk end by +/-F fraction of chunk size (default: 0.05)",
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
    start = time.monotonic()

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
            cycle = 0
            while True:
                cycle += 1
                head_before = git(["rev-parse", "HEAD"], cwd=repo).stdout.strip()

                print(
                    f"\n[*] Phase 1 (cycle {cycle}): reducing files, functions and calls..."
                )
                reduce_files_and_functions(files, repo, worktrees, cmd)
                print("[*] Phase 1 done.")

                print(f"\n[*] Phase 1.5 (cycle {cycle}): truncating files...")
                truncate_files(files, repo, worktrees, cmd)
                print("[*] Phase 1.5 done.")

                print(f"\n[*] Phase 1.75 (cycle {cycle}): reducing empty lines...")
                reduce_empty_lines(files, repo, worktrees, cmd)
                print("[*] Phase 1.75 done.")

                print(f"\n[*] Phase 2 (cycle {cycle}): reducing lines...")
                reduce_lines(
                    files,
                    repo,
                    worktrees,
                    cmd,
                    jitter=args.jitter,
                    min_chunk_size=16 if cycle == 1 else 1,
                )  # coarse first pass; fine-grained on cycle 2+
                print("[*] Phase 2 done.")

                head_after = git(["rev-parse", "HEAD"], cwd=repo).stdout.strip()
                if head_after == head_before and cycle >= 2:
                    break  # cycle 1 uses coarse chunks, so always allow a cycle-2 fine-grained pass
                print(f"\n[*] Cycle {cycle} made progress; restarting phase cycle...")

        finally:
            print(f"\n[*] Removing worktrees...")
            remove_worktrees(repo, worktrees)

        print("\n[*] Reduction complete. Commits:")
        subprocess.run(["git", "log", "--oneline", "-20"], cwd=repo)
        print(f"\n[*] Done in {time.monotonic() - start:.1f}s")

    finally:
        shutil.rmtree(parent, ignore_errors=True)


if __name__ == "__main__":
    main()
