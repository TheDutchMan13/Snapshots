"""Builds must be repeatable: every dependency has an upper bound, so a new major release of a library
(OpenCV 5 broke the build once) can't arrive unnoticed. The build records the exact versions it used."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_every_dependency_has_an_upper_bound():
    for name in ("requirements.txt", "requirements-dev.txt"):
        for line in (ROOT / name).read_text(encoding="utf-8").splitlines():
            line = line.split("#")[0].strip()
            if not line or line.startswith("-r "):
                continue
            spec = line.split(";")[0]
            assert re.search(r"<\d|==|~=", spec), f"{name}: '{line}' has no upper bound"


def test_the_build_records_its_versions_and_limits_test_reruns():
    wf = (ROOT / ".github/workflows/build-exe.yml").read_text(encoding="utf-8")
    assert "pip freeze" in wf and "build-requirements" in wf
    assert re.search(r"--reruns 1\b", wf) and "-rR" in wf and "--reruns 2" not in wf
    assert 'pyinstaller>=6,<7' in wf
