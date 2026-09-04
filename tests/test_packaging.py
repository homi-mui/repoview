import re
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "repoview.spec"
MANPAGE_PATH = ROOT / "repoview.8"
SCRIPT_PATH = ROOT / "repoview.py"
ARCHIVE_SCRIPT = ROOT / "scripts" / "create-source-archive.sh"


def project_version():
    match = re.search(
        r'^version = "([^"]+)"$',
        (ROOT / "pyproject.toml").read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    assert match is not None
    return match.group(1)


def test_packaging_versions_stay_in_sync():
    version = project_version()

    script_match = re.search(r"^VERSION = '([^']+)'$", SCRIPT_PATH.read_text(encoding="utf-8"), re.MULTILINE)
    assert script_match is not None
    assert script_match.group(1) == version

    spec_match = re.search(r"^Version:\s+(.+)$", SPEC_PATH.read_text(encoding="utf-8"), re.MULTILINE)
    assert spec_match is not None
    assert spec_match.group(1).strip() == version

    manpage_match = re.search(r'^\.TH "repoview" "8" "([^"]+)"', MANPAGE_PATH.read_text(encoding="utf-8"), re.MULTILINE)
    assert manpage_match is not None
    assert manpage_match.group(1) == version


def test_template_install_path_matches_runtime_default():
    script_match = re.search(
        r"^DEFAULT_TEMPLATEDIR = '([^']+)'$",
        SCRIPT_PATH.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    assert script_match is not None
    assert script_match.group(1) == "/usr/share/repoview/templates"
    assert "%{_datadir}/repoview/templates" in SPEC_PATH.read_text(encoding="utf-8")


def test_source_archive_contains_spec_inputs_and_excludes_build_artifacts(tmp_path):
    subprocess.run([str(ARCHIVE_SCRIPT), str(tmp_path)], check=True, cwd=ROOT)

    version = project_version()
    archive_path = tmp_path / f"repoview-{version}.tar.gz"
    root_prefix = f"repoview-{version}/"

    with tarfile.open(archive_path, "r:gz") as archive:
        names = archive.getnames()

    assert f"{root_prefix}repoview.py" in names
    assert f"{root_prefix}repoview.8" in names
    assert f"{root_prefix}repoview.spec" in names
    assert f"{root_prefix}COPYING" in names
    assert f"{root_prefix}README.md" in names
    assert f"{root_prefix}templates/index.j2" in names
    assert f"{root_prefix}templates/group.j2" in names
    assert f"{root_prefix}templates/package.j2" in names
    assert f"{root_prefix}templates/rss.j2" in names
    assert f"{root_prefix}templates/layout/repostyle.css" in names
    assert all("__pycache__/" not in name for name in names)
    assert all("/.pytest_cache/" not in name for name in names)
    assert all(not name.startswith(f"{root_prefix}dist/") for name in names)
    assert all(not name.startswith(f"{root_prefix}RPMS/") for name in names)
