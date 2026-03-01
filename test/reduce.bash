#!/usr/bin/env bash
# Test reduce_files: given a repo with 4 files where only one matters,
# verify that the 3 irrelevant files are deleted by phase 1.
set -euo pipefail

SCRIPT="$(cd "$(dirname "$0")/.." && pwd)/reduce-repo.py"

# Setup
REPO=$(mktemp -d)
trap 'rm -rf "$REPO"' EXIT

cd "$REPO"
git init -q
git config user.email "test@test"
git config user.name "test"

# The "bug" lives in bug.txt; the rest are noise.
echo "contains the bug" > bug.txt
echo "noise a" > a.txt
echo "noise b" > b.txt
echo "noise c" > c.txt

git add .
git commit -qm "initial"

# Interestingness: interesting when bug.txt still contains "bug"
INTERESTING=$(mktemp --suffix=.sh)
cat > "$INTERESTING" <<'EOF'
#!/usr/bin/env bash
grep -q "bug" bug.txt 2>/dev/null
EOF
chmod +x "$INTERESTING"

python3 "$SCRIPT" -n 2 -c "$INTERESTING" "$REPO" >/dev/null

remaining=$(git -C "$REPO" ls-files | sort | tr '\n' ' ')
if [ "$remaining" = "bug.txt " ]; then
    echo "PASS: only bug.txt remains"
else
    echo "FAIL: expected 'bug.txt', got '$remaining'"
    exit 1
fi
