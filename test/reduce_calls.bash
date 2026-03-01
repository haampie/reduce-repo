#!/usr/bin/env bash
# Test reduce_calls: given a repo with a Python file containing a noise call,
# verify that phase 0 removes it and records it in commit messages.
set -euo pipefail

SCRIPT="$(cd "$(dirname "$0")/.." && pwd)/reduce_repo.py"

REPO=$(mktemp -d)
trap 'rm -rf "$REPO"' EXIT

cd "$REPO"
git init -q
git config user.email "test@test"
git config user.name "test"

cat > lib.py <<'EOF'
# BUG
noise()
EOF

git add .
git commit -qm "initial"

# Interesting when lib.py still contains the bug marker
INTERESTING=$(mktemp --suffix=.sh)
cat > "$INTERESTING" <<'EOF'
#!/usr/bin/env bash
grep -q "# BUG" lib.py 2>/dev/null
EOF
chmod +x "$INTERESTING"

python3 "$SCRIPT" -n 2 -c "$INTERESTING" "$REPO" >/dev/null

# Phase 0 must have produced at least one call-deletion commit
if ! git -C "$REPO" log --oneline | grep -qE "reduce: delete.*call"; then
    echo "FAIL: no call-deletion commits found in git log"
    git -C "$REPO" log --oneline
    exit 1
fi

# The noise() call must be gone
if grep -q "noise()" "$REPO/lib.py"; then
    echo "FAIL: noise() call still present in lib.py"
    cat "$REPO/lib.py"
    exit 1
fi

# The bug marker must still be there
if ! grep -q "# BUG" "$REPO/lib.py"; then
    echo "FAIL: bug marker was incorrectly removed"
    cat "$REPO/lib.py"
    exit 1
fi

echo "PASS: phase 0 removed noise() call, kept # BUG line"
