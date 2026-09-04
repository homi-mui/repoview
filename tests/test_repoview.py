import bz2
import gzip
import lzma
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest

import repoview

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = ROOT / "templates"


def make_options(repo_dir, **overrides):
    values = {
        "repodir": str(repo_dir),
        "quiet": True,
        "force": False,
        "statedir": None,
        "ignore": [],
        "xarch": [],
        "comps": None,
        "templatedir": str(TEMPLATE_DIR),
        "outdir": "repoview-output",
        "title": "Test Repo",
        "env": None,
        "url": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def make_primary_db(path, rows):
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE packages (
        pkgKey INTEGER,
        name TEXT,
        epoch TEXT,
        version TEXT,
        release TEXT,
        arch TEXT,
        summary TEXT,
        description TEXT,
        url TEXT,
        time_build INTEGER,
        rpm_license TEXT,
        rpm_sourcerpm TEXT,
        size_package INTEGER,
        location_href TEXT,
        rpm_vendor TEXT,
        rpm_group TEXT
    )"""
    )
    conn.executemany(
        "INSERT INTO packages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def make_other_db(path, rows):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE changelog (pkgKey INTEGER, author TEXT, date INTEGER, changelog TEXT)")
    conn.executemany("INSERT INTO changelog VALUES (?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()


def make_filelists_db(path, rows):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE filelist (pkgKey INTEGER, dirname TEXT, filenames TEXT, filetypes TEXT)")
    conn.executemany("INSERT INTO filelist VALUES (?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()


def compress(path, suffix):
    data = path.read_bytes()
    target = path.with_suffix(path.suffix + suffix)
    if suffix == ".gz":
        target.write_bytes(gzip.compress(data))
    elif suffix == ".bz2":
        target.write_bytes(bz2.compress(data))
    elif suffix == ".xz":
        target.write_bytes(lzma.compress(data))
    elif suffix == ".zst":
        if repoview.zstandard is None:
            pytest.skip("zstandard module not available")
        target.write_bytes(repoview.zstandard.ZstdCompressor().compress(data))
    else:
        raise AssertionError("unsupported suffix")
    path.unlink()
    return target


def write_repomd(repo_dir, entries, namespace=repoview.REPO_XML_NAMESPACE):
    repodata = repo_dir / "repodata"
    repodata.mkdir(parents=True, exist_ok=True)
    root = ET.Element(f"{{{namespace}}}repomd")
    for entry_type, href, dbversion in entries:
        data = ET.SubElement(root, f"{{{namespace}}}data", {"type": entry_type})
        ET.SubElement(data, f"{{{namespace}}}location", {"href": href})
        if dbversion is not None:
            version = ET.SubElement(data, f"{{{namespace}}}database_version")
            version.text = str(dbversion)
    ET.ElementTree(root).write(repodata / "repomd.xml", encoding="utf-8", xml_declaration=True)


def write_repository(repo_dir, *, primary_rows, changelog_rows, filelist_rows, compressions=None):
    compressions = compressions or {}
    repodata = repo_dir / "repodata"
    repodata.mkdir(parents=True, exist_ok=True)

    primary = repodata / "primary.sqlite"
    other = repodata / "other.sqlite"
    filelists = repodata / "filelists.sqlite"
    make_primary_db(primary, primary_rows)
    make_other_db(other, changelog_rows)
    make_filelists_db(filelists, filelist_rows)

    entries = []
    for entry_type, path in (
        ("primary_db", primary),
        ("other_db", other),
        ("filelists_db", filelists),
    ):
        suffix = compressions.get(entry_type)
        if suffix:
            path = compress(path, suffix)
        entries.append((entry_type, path.relative_to(repo_dir).as_posix(), 10 if entry_type == "primary_db" else None))

    write_repomd(repo_dir, entries)


@pytest.fixture()
def package_rows():
    return [
        (1, "john's-app", "0", "1.0", "1", "x86_64", "Old summary", "Old desc", "https://example.invalid/old", 1000, "GPL", "johns.src.rpm", 1024, "Packages/johns-old.rpm", "Vendor", "Utilities"),
        (2, "john's-app", "0", "2.0", "1", "x86_64", "New summary", "New desc", "https://example.invalid/new", 2000, "GPL", "johns.src.rpm", 4096, "Packages/johns-new.rpm", "Vendor", "Utilities"),
        (3, "other", "0", "1.0", "3", "noarch", "Other summary", "Other desc", "https://example.invalid/other", 1500, "MIT", "other.src.rpm", 2048, "Packages/other.rpm", "Vendor", "Utilities"),
    ]


@pytest.fixture()
def changelog_rows():
    return [
        (1, "Maintainer <old@example.invalid>", 1000, "old"),
        (2, "Maintainer <new@example.invalid>", 2000, "new"),
        (3, "Other Person <other@example.invalid>", 1500, "other"),
    ]


@pytest.fixture()
def filelist_rows():
    return [
        (2, "/etc/johns", "config//bin", "fdf"),
        (3, "/usr/share/other", "doc", "f"),
    ]


@pytest.fixture()
def repo_dir(tmp_path, package_rows, changelog_rows, filelist_rows):
    repo = tmp_path / "repo"
    write_repository(
        repo,
        primary_rows=package_rows,
        changelog_rows=changelog_rows,
        filelist_rows=filelist_rows,
    )
    return repo


@pytest.fixture(autouse=True)
def fake_rpm_compare(monkeypatch):
    def compare(left, right):
        left_key = tuple(int(part) for part in left[1].split("."))
        right_key = tuple(int(part) for part in right[1].split("."))
        if left_key < right_key:
            return -1
        if left_key > right_key:
            return 1
        return 0

    monkeypatch.setattr(repoview, "rpm_label_compare", compare)


def test_read_repomd_metadata_discovers_required_sqlite_entries(repo_dir):
    metadata = repoview.read_repomd_metadata(repo_dir)
    assert metadata.primary == (repo_dir / "repodata" / "primary.sqlite").resolve()
    assert metadata.other == (repo_dir / "repodata" / "other.sqlite").resolve()
    assert metadata.filelists == (repo_dir / "repodata" / "filelists.sqlite").resolve()
    assert metadata.dbversion == 10


@pytest.mark.parametrize(
    "entries, message",
    [
        ([
            ("primary_db", "repodata/primary.sqlite", 10),
            ("other_db", "repodata/other.sqlite", None),
        ], "filelists_db"),
        ([
            ("other_db", "repodata/other.sqlite", None),
            ("filelists_db", "repodata/filelists.sqlite", None),
        ], "primary_db"),
    ],
)
def test_read_repomd_metadata_reports_missing_required_entries(tmp_path, entries, message):
    repo_dir = tmp_path / "repo"
    write_repomd(repo_dir, entries)
    with pytest.raises(repoview.RepoviewError, match=message):
        repoview.read_repomd_metadata(repo_dir)


def test_read_repomd_metadata_requires_expected_namespace(tmp_path):
    repo_dir = tmp_path / "repo"
    write_repomd(repo_dir, [("primary_db", "repodata/primary.sqlite", 10)], namespace="urn:wrong")
    with pytest.raises(repoview.RepoviewError, match="unsupported namespace"):
        repoview.read_repomd_metadata(repo_dir)


def test_read_repomd_metadata_reports_malformed_xml(tmp_path):
    repo_dir = tmp_path / "repo"
    repodata = repo_dir / "repodata"
    repodata.mkdir(parents=True)
    (repodata / "repomd.xml").write_text("<repomd>", encoding="utf-8")
    with pytest.raises(repoview.RepoviewError, match="Failed to parse"):
        repoview.read_repomd_metadata(repo_dir)


@pytest.mark.parametrize("suffix", [".gz", ".bz2", ".xz"])
def test_z_handler_decompresses_stdlib_formats(tmp_path, suffix):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    data_file = repo_dir / "metadata.sqlite"
    data_file.write_bytes(b"metadata")
    compressed = compress(data_file, suffix)
    app = repoview.Repoview(make_options(repo_dir))
    try:
        result = app.z_handler(compressed)
        assert result.read_bytes() == b"metadata"
        assert result in app.cleanup
    finally:
        app.close()
        assert not any(path.exists() for path in app.cleanup)


def test_z_handler_uses_python_zstandard_when_available(tmp_path):
    if repoview.zstandard is None:
        pytest.skip("zstandard module not available")

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    data_file = repo_dir / "metadata.sqlite"
    data_file.write_bytes(b"zstd-metadata")
    compressed = compress(data_file, ".zst")

    app = repoview.Repoview(make_options(repo_dir))
    try:
        result = app.z_handler(compressed)
        assert result.read_bytes() == b"zstd-metadata"
    finally:
        app.close()


def test_z_handler_reports_clear_error_without_zstd_support(tmp_path, monkeypatch):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    data_file = repo_dir / "metadata.sqlite.zst"
    data_file.write_bytes(b"not-used")
    app = repoview.Repoview(make_options(repo_dir))
    monkeypatch.setattr(repoview, "zstandard", None)
    monkeypatch.setattr(repoview.shutil, "which", lambda name: None)
    try:
        with pytest.raises(repoview.RepoviewError, match="Zstandard-compressed"):
            app.z_handler(data_file)
    finally:
        app.close()


def test_z_handler_falls_back_to_external_zstd(tmp_path, monkeypatch):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    data_file = repo_dir / "metadata.sqlite.zst"
    data_file.write_bytes(b"unused")
    app = repoview.Repoview(make_options(repo_dir))

    monkeypatch.setattr(repoview, "zstandard", None)
    monkeypatch.setattr(repoview.shutil, "which", lambda name: "/usr/bin/zstd")

    def fake_run(args, check, stdout):
        assert args[0] == "zstd"
        stdout.write(b"external-zstd")
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(repoview.subprocess, "run", fake_run)
    try:
        result = app.z_handler(data_file)
        assert result.read_bytes() == b"external-zstd"
    finally:
        app.close()


def test_parse_filelist_entries_preserves_alignment_and_empty_components():
    assert repoview.parse_filelist_entries("/etc/johns", "config//bin", "fdf") == [
        ("f", "/etc/johns/config"),
        ("d", "/etc/johns"),
        ("f", "/etc/johns/bin"),
    ]


def test_get_package_data_supports_apostrophes_and_rpm_ordering(repo_dir):
    app = repoview.Repoview(make_options(repo_dir))
    try:
        app.setup_repo()
        app.setup_excludes()
        pkg = app.get_package_data("john's-app")
        assert pkg["name"] == "john's-app"
        assert [rpm[1] for rpm in pkg["rpms"]] == ["2.0", "1.0"]
        assert pkg["rpms"][0][-1] == [
            ("f", "/etc/johns/config"),
            ("d", "/etc/johns"),
            ("f", "/etc/johns/bin"),
        ]
    finally:
        app.close()


def test_exclusions_with_apostrophes_are_parameterized(repo_dir):
    app = repoview.Repoview(make_options(repo_dir, ignore=["john's-*"]))
    try:
        app.setup_repo()
        app.setup_excludes()
        latest = app.get_latest_packages()
        assert [row[0] for row in latest] == ["other"]
    finally:
        app.close()


def test_run_renders_templates_and_removes_stale_files(tmp_path, package_rows, changelog_rows, filelist_rows, monkeypatch):
    repo_dir = tmp_path / "repo"
    write_repository(
        repo_dir,
        primary_rows=package_rows,
        changelog_rows=changelog_rows,
        filelist_rows=filelist_rows,
        compressions={"primary_db": ".gz", "other_db": ".bz2", "filelists_db": ".xz"},
    )
    options = make_options(repo_dir, url="https://example.invalid/repo")

    rendered = []
    app = repoview.Repoview(options)
    original = app.render_template_to_path

    def record_render(template, destination, **context):
        rendered.append((template.name, destination.name, sorted(context.keys())))
        return original(template, destination, **context)

    monkeypatch.setattr(app, "render_template_to_path", record_render)
    try:
        app.run()
    finally:
        app.close()

    outdir = repo_dir / "repoview-output"
    stale = outdir / "stale.html"
    stale.write_text("stale", encoding="utf-8")
    with sqlite3.connect(outdir / "state.sqlite") as conn:
        conn.execute(
            "INSERT OR REPLACE INTO state (filename, checksum) VALUES (?, ?)",
            ("stale.html", "old"),
        )
        conn.commit()

    updated_rows = [row for row in package_rows if row[1] != "other"]
    write_repository(
        repo_dir,
        primary_rows=updated_rows,
        changelog_rows=[row for row in changelog_rows if row[0] != 3],
        filelist_rows=filelist_rows,
    )

    second = repoview.Repoview(make_options(repo_dir, url="https://example.invalid/repo"))
    try:
        second.run()
    finally:
        second.close()

    assert (outdir / "index.html").exists()
    assert (outdir / "john's-app.html").exists()
    assert (outdir / "latest-feed.xml").exists()
    assert not (outdir / "other.html").exists()
    assert not stale.exists()
    assert {item[0] for item in rendered} >= {"package.j2", "group.j2", "index.j2"}


def test_state_dir_supports_nested_directories(repo_dir, tmp_path):
    state_dir = tmp_path / "nested" / "state"
    app = repoview.Repoview(make_options(repo_dir, statedir=str(state_dir)))
    try:
        app.setup_outdir()
        app.setup_state_db()
    finally:
        app.close()
    assert any(path.name.endswith(".state.sqlite") for path in state_dir.iterdir())


def test_resolve_output_dir_uses_repository_relative_paths(repo_dir):
    resolved = repoview.resolve_output_dir(repo_dir, "nested/output")
    assert resolved == (repo_dir / "nested" / "output").resolve()
