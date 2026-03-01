#!/usr/bin/env bash
# Regression test for stride-1 fix: an odd-aligned adjacent pair (lines 1-2)
# that can only be deleted together must be removed by reduce_lines.
#
# The file has 4 lines:
#   0: x = 1  # BUG_MARKER   <- must survive (carries the bug marker)
#   1: if x:                  <- noise, but only removable together with line 2
#   2:     pass               <- noise, but only removable together with line 1
#   3: print(x)               <- noise, removable individually
#
# Interestingness: file is valid Python AND contains BUG_MARKER.
# Deleting only line 1 leaves a top-level indented `pass` -> SyntaxError.
# Deleting only line 2 leaves `if x:` with no body -> SyntaxError.
# Deleting both lines 1+2 yields valid Python with BUG_MARKER -> still interesting.
#
# With the old even-aligned stride (stride=chunk_size=2), chunk_size=2 only
# tries [0:2] and [2:4] -- the pair [1:3] is never attempted as a unit, so
# the file cannot be fully reduced. The stride-1 fix generates [1:3] and
# removes the pair.
set -euo pipefail

SCRIPT="$(cd "$(dirname "$0")/.." && pwd)/reduce_repo.py"

REPO=$(mktemp -d)
trap 'rm -rf "$REPO"' EXIT

cd "$REPO"
git init -q
git config user.email "test@test"
git config user.name "test"

cat > script.py <<'EOF'
x = 1  # BUG_MARKER
if x:
    pass
print(x)
EOF

git add .
git commit -qm "initial"

INTERESTING=$(mktemp --suffix=.sh)
cat > "$INTERESTING" <<'EOF'
#!/usr/bin/env bash
python3 - <<'PY'
import ast, sys
try:
    ast.parse(open("script.py").read())
except SyntaxError:
    sys.exit(1)
PY
grep -q "BUG_MARKER" script.py
EOF
chmod +x "$INTERESTING"

python3 "$SCRIPT" -n 2 -c "$INTERESTING" "$REPO" >/dev/null

if grep -qE "^if x:|^    pass$" "$REPO/script.py"; then
    echo "FAIL: odd-aligned pair (lines 1-2) was not removed"
    cat "$REPO/script.py"
    exit 1
fi

if ! grep -q "BUG_MARKER" "$REPO/script.py"; then
    echo "FAIL: BUG_MARKER was incorrectly removed"
    cat "$REPO/script.py"
    exit 1
fi

echo "PASS: odd-aligned pair at lines 1-2 was correctly deleted"
