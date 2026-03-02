# reduce-repo

Reduce a git repository to a minimal bug reproduction.

Given a repository and an *interestingness test* (a shell command that exits 0
when the bug is still present), `reduce-repo` systematically deletes files,
functions, and lines until nothing further can be removed without making the
test fail. Every successful reduction is committed, so the git history records
the progress and the result is always a valid repo.

## How it works

Reduction runs as a fixed-point loop over four phases:

1. **Phase 1 - file and function deletion**: tries deleting each tracked file,
   interleaved with deleting Python functions/methods (including decorators)
2. **Phase 1.75 - empty line deletion**: strips blank/whitespace-only lines
   left behind after function bodies are removed, using a single binary-search
   pass over files.
3. **Phase 2 - line deletion**: tries deleting contiguous chunks of lines
   within each file, from coarse (whole file) down to fine (single line),
   using a depth-major binary-search strategy across all files in parallel.

Each phase loops internally until it can make no further progress. The three
phases are then repeated as an outer fixed-point loop until a full cycle
produces no new commits, ensuring that reductions in one phase can unlock
further reductions in another.

Within phase 2, an `(file, start, end)` cache skips line ranges that are known
to still fail for unchanged files. Before giving up, one final sweep clears the
cache to catch ranges that became feasible due to changes in other files.

Parallel workers test candidate deletions concurrently. A dedicated applier
thread combines successful patches and commits them, keeping all worktrees
in sync after each commit.

## Installation

Requires Python 3.11+.

```
pip install -e .
```

Or run directly:

```
python3 reduce_repo.py -c COMMAND REPO_PATH
```

## Usage

```
reduce-repo -c COMMAND [options] REPO_PATH
```

`COMMAND` is executed with the repository as the working directory. Exit 0
means the bug is still present (*interesting*); any other exit code means the
bug is gone.

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `-c COMMAND` | *(required)* | Interestingness test command |
| `-n N` | `4` | Number of parallel workers |
| `--lines-only` | off | Skip file deletion (phase 1), go straight to line reduction |
| `--jitter F` | `0.05` | Randomise each chunk boundary by +/-F x chunk size |

### Example

```bash
reduce-repo -n 8 -c './interesting.sh' /path/to/repo
```

`interesting.sh` might look like:

```bash
#!/bin/sh
make 2>&1 | grep -q 'segmentation fault'
```

The script is automatically copied outside the repo before reduction starts,
so it is safe to keep it inside the repository being reduced.

## Output

`reduce-repo` creates a new branch (`reduce-XXXXXXXX`) and commits each
successful reduction to it. The original branch is left untouched. At the end,
the reduced repository is on the new branch.

## Tests

```bash
python -m pytest
```
