import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from reduce_repo import _apply_deletions

LINES = ["a\n", "b\n", "c\n", "d\n", "e\n"]  # indices 0-4


def test_non_overlapping():
    assert _apply_deletions(LINES, [(0, 1), (3, 5)]) == ["b\n", "c\n"]


def test_overlapping():
    assert _apply_deletions(LINES, [(1, 3), (2, 4)]) == ["a\n", "e\n"]


def test_adjacent():
    assert _apply_deletions(LINES, [(1, 2), (2, 3)]) == ["a\n", "d\n", "e\n"]


def test_nested():
    assert _apply_deletions(LINES, [(1, 4), (2, 3)]) == ["a\n", "e\n"]


def test_out_of_bounds_end():
    assert _apply_deletions(LINES, [(3, 99)]) == ["a\n", "b\n", "c\n"]


def test_invalid_interval_skipped():
    assert _apply_deletions(LINES, [(3, 2)]) == LINES  # s >= e -> no-op


def test_empty_intervals():
    assert _apply_deletions(LINES, []) == LINES


def test_all_removed():
    assert _apply_deletions(LINES, [(0, 5)]) == []


def test_unsorted_input():
    assert _apply_deletions(LINES, [(3, 5), (0, 1)]) == ["b\n", "c\n"]
