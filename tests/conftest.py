"""Run each tests/test_*.py suite as its own script under pytest.

The suites are plain scripts (`python3 tests/test_x.py`), and several set up
the shared antigua_core.settings at import time, so importing them all into
one pytest process would let one suite's settings leak into the next. Each
file runs in a fresh subprocess instead, exactly as it does by hand. A
non-zero exit is a failure, and the script's output is the report.
"""

import subprocess
import sys

import pytest


class ScriptFailed(Exception):
    pass


def pytest_collect_file(parent, file_path):
    if file_path.suffix == ".py" and file_path.name.startswith("test_"):
        return SuiteFile.from_parent(parent, path=file_path)
    return None


class SuiteFile(pytest.File):
    def collect(self):
        yield SuiteItem.from_parent(self, name=self.path.stem)


class SuiteItem(pytest.Item):
    def runtest(self):
        r = subprocess.run(
            [sys.executable, str(self.path)],
            cwd=self.config.rootpath, capture_output=True, text=True, timeout=600,
        )
        if r.returncode:
            raise ScriptFailed(f"exit {r.returncode}\n{r.stdout[-6000:]}{r.stderr[-6000:]}")

    def repr_failure(self, excinfo):
        if isinstance(excinfo.value, ScriptFailed):
            return str(excinfo.value)
        return super().repr_failure(excinfo)

    def reportinfo(self):
        return self.path, 0, f"suite {self.path.name}"
