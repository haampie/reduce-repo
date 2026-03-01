#!/usr/bin/env bash
# Test reduce_functions: given a repo with a Python file containing redundant
# functions, verify that phase 1.5 removes them and records it in commit messages.
set -euo pipefail

SCRIPT="$(cd "$(dirname "$0")/.." && pwd)/reduce_repo.py"

REPO=$(mktemp -d)
trap 'rm -rf "$REPO"' EXIT

cd "$REPO"
git init -q
git config user.email "test@test"
git config user.name "test"

cat > lib.py <<'EOF'
def important():
    x = "the_bug"
    return x

def noise1():
    y = "nothing useful"
    return y

@staticmethod
def noise2():
    z = "also useless"
    return z

@some_decorator
@another_decorator
def noise3():
    w = "triple noise"
    return w
EOF

git add .
git commit -qm "initial"

# Interesting when lib.py still contains the bug marker
INTERESTING=$(mktemp --suffix=.sh)
cat > "$INTERESTING" <<'EOF'
#!/usr/bin/env bash
grep -q "the_bug" lib.py 2>/dev/null
EOF
chmod +x "$INTERESTING"

python3 "$SCRIPT" -n 2 -c "$INTERESTING" "$REPO" >/dev/null

# Phase 1.5 must have produced at least one function-deletion commit
if ! git -C "$REPO" log --oneline | grep -q "delete.*function"; then
    echo "FAIL: no function-deletion commits found in git log"
    git -C "$REPO" log --oneline
    exit 1
fi

# The noise functions must be gone - including their decorator lines
if grep -qE "noise1|noise2|noise3|some_decorator|another_decorator" "$REPO/lib.py"; then
    echo "FAIL: noise functions (or their decorators) still present in lib.py"
    cat "$REPO/lib.py"
    exit 1
fi

# The important function must still be there
if ! grep -q "the_bug" "$REPO/lib.py"; then
    echo "FAIL: important function was incorrectly removed"
    cat "$REPO/lib.py"
    exit 1
fi

echo "PASS: phase 1.5 removed noise functions, kept important function"
