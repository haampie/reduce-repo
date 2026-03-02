import ast
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from reduce_repo import _collect_funcs_and_calls

EXAMPLE = pathlib.Path(__file__).parent / "function_calls_to_remove.py"


def test_function_defs_collected():
    source = EXAMPLE.read_text()
    lines_list = source.splitlines()
    tree = ast.parse(source)
    defs, calls = _collect_funcs_and_calls(tree, "example.py", lines_list)
    # top_level() and method() — 2 function defs
    assert len(defs) == 2
    names = [source.splitlines()[s] for _, s, _ in defs]
    assert any("top_level" in n for n in names)
    assert any("method" in n for n in names)


def test_calls_collected():
    source = EXAMPLE.read_text()
    lines_list = source.splitlines()
    tree = ast.parse(source)
    defs, calls = _collect_funcs_and_calls(tree, "example.py", lines_list)
    # top_level() at module level and class_call() in class body
    assert len(calls) == 2
    call_lines = [source.splitlines()[s] for _, s, _ in calls]
    assert any("top_level()" in l for l in call_lines)
    assert any("class_call()" in l for l in call_lines)


def test_method_body_excluded():
    source = EXAMPLE.read_text()
    lines_list = source.splitlines()
    tree = ast.parse(source)
    defs, calls = _collect_funcs_and_calls(tree, "example.py", lines_list)
    print(defs, calls)
    # inner_call() and method_call() are inside function bodies — must not appear as separate calls
    call_lines = set()
    for _, s, e in calls:
        call_lines.update(range(s, e))
    for i, line in enumerate(source.splitlines()):
        if "inner_call()" in line or "method_call()" in line:
            assert i not in call_lines, (
                f"Line {i} ({line!r}) should not be collected as a call"
            )


def test_partition_order():
    source = EXAMPLE.read_text()
    lines_list = source.splitlines()
    tree = ast.parse(source)
    defs, calls = _collect_funcs_and_calls(tree, "example.py", lines_list)
    # Result must be two separate lists, not interleaved
    assert isinstance(defs, list)
    assert isinstance(calls, list)
    assert len(defs) >= 1
    assert len(calls) >= 1
