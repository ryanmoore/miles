"""Every script the web app serves must parse: a syntax error in a page's one
inline script blanks the whole page, and no Python test loads the pages."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).parent.parent / "miles" / "static"
INLINE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S)


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to parse JavaScript")
def test_static_scripts_parse(tmp_path: Path) -> None:
    scripts = {p.relative_to(STATIC).as_posix(): p.read_text() for p in STATIC.rglob("*.js")}
    for page in STATIC.rglob("*.html"):
        for i, body in enumerate(INLINE.findall(page.read_text())):
            scripts[f"{page.name}#{i}"] = body
    assert scripts
    failures = []
    for name, body in scripts.items():
        path = tmp_path / "script.js"
        path.write_text(body)
        result = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True, timeout=30)
        if result.returncode:
            failures.append(f"{name}: {result.stderr.strip().splitlines()[-1]}")
    assert not failures, "\n".join(failures)
