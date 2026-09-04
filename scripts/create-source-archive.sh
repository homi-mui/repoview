#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
output_dir=${1:-"$repo_root/dist"}

mkdir -p "$output_dir"

python3 - "$repo_root" "$output_dir" <<'PY'
import gzip
import os
import pathlib
import re
import sys
import tarfile

repo_root = pathlib.Path(sys.argv[1]).resolve()
output_dir = pathlib.Path(sys.argv[2]).resolve()

match = re.search(
    r'^version = "([^"]+)"$',
    (repo_root / "pyproject.toml").read_text(encoding="utf-8"),
    re.MULTILINE,
)
if match is None:
    raise SystemExit("Unable to determine version from pyproject.toml")

version = match.group(1)
archive_root = f"repoview-{version}"
archive_path = output_dir / f"{archive_root}.tar.gz"
source_date_epoch = int(os.environ.get("SOURCE_DATE_EPOCH", "0"))

excluded_dir_names = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    "build",
    "dist",
    ".rpmbuild",
    "BUILD",
    "BUILDROOT",
    "RPMS",
    "SOURCES",
    "SPECS",
    "SRPMS",
}
excluded_suffixes = {".pyc", ".pyo", ".rpm", ".srpm"}

with gzip.GzipFile(filename="", mode="wb", fileobj=archive_path.open("wb"), mtime=source_date_epoch) as gz_file:
    with tarfile.open(fileobj=gz_file, mode="w") as archive:
        for path in sorted(repo_root.rglob("*")):
            relative_path = path.relative_to(repo_root)
            parts = relative_path.parts
            if any(part in excluded_dir_names for part in parts):
                continue
            if path.suffix in excluded_suffixes:
                continue
            if not path.is_file():
                continue

            tar_info = archive.gettarinfo(str(path), arcname=f"{archive_root}/{relative_path.as_posix()}")
            tar_info.uid = 0
            tar_info.gid = 0
            tar_info.uname = "root"
            tar_info.gname = "root"
            tar_info.mtime = source_date_epoch

            with path.open("rb") as source_file:
                archive.addfile(tar_info, source_file)

print(archive_path)
PY
