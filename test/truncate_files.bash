#!/usr/bin/env bash
# Test truncate_files (Phase 1.6): a file that must exist (so Phase 1 cannot
# delete it) but whose content is irrelevant should be truncated to empty.
#
# Repo has two files:
#   main.txt  -- contains the bug marker; must survive with content
#   bloated.txt -- large content; test only checks existence, not content
#
# Interestingness: main.txt contains "bug" AND bloated.txt exists.
# Phase 1 tries to delete bloated.txt -> fails (test -f bloated.txt fails).
# Phase 1.6 tries to truncate bloated.txt -> succeeds (empty file still exists).
set -euo pipefail

SCRIPT="$(cd "$(dirname "$0")/.." && pwd)/reduce_repo.py"

REPO=$(mktemp -d)
trap 'rm -rf "$REPO"' EXIT

cd "$REPO"
git init -q
git config user.email "test@test"
git config user.name "test"

echo "contains the bug" > main.txt
# bloated.txt has substantial content that is entirely irrelevant
python3 -c "
for i in range(200):
    print(f'noise line {i}: ' + 'x' * 60)
" > bloated.txt

git add .
git commit -qm "initial"

INTERESTING=$(mktemp --suffix=.sh)
cat > "$INTERESTING" <<'EOF'
#!/usr/bin/env bash
grep -q "bug" main.txt 2>/dev/null && test -f bloated.txt
EOF
chmod +x "$INTERESTING"

python3 "$SCRIPT" -n 2 -c "$INTERESTING" "$REPO" >/dev/null

# Phase 1.6 must have produced at least one truncate commit
if ! git -C "$REPO" log --oneline | grep -q "truncate"; then
    echo "FAIL: no truncate commits found in git log"
    git -C "$REPO" log --oneline
    exit 1
fi

# bloated.txt must still exist (test requires it) but be empty
if [ ! -f "$REPO/bloated.txt" ]; then
    echo "FAIL: bloated.txt was deleted (should have been truncated)"
    exit 1
fi
if [ -s "$REPO/bloated.txt" ]; then
    echo "FAIL: bloated.txt still has content ($(wc -c < "$REPO/bloated.txt") bytes)"
    exit 1
fi

# main.txt must still contain the bug marker
if ! grep -q "bug" "$REPO/main.txt"; then
    echo "FAIL: main.txt no longer contains the bug marker"
    cat "$REPO/main.txt"
    exit 1
fi

echo "PASS: Phase 1.6 truncated bloated.txt to empty while keeping main.txt intact"
