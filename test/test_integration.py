import pathlib
import subprocess

TEST_DIR = pathlib.Path(__file__).parent


def test_reduce_files():
    r = subprocess.run(["bash", TEST_DIR / "reduce.bash"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_reduce_functions():
    r = subprocess.run(["bash", TEST_DIR / "reduce_functions.bash"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_reduce_lines_odd_pair():
    r = subprocess.run(["bash", TEST_DIR / "reduce_lines_odd_pair.bash"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
