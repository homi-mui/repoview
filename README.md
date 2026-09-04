# repoview

Repoview parses RPM repository metadata and generates static HTML pages for browsing over HTTP.  
This is a fork from https://github.com/philfry/repoview extended with support for xz and Zstandard-compressed repository metadata files and works with new-ish python versions

## Supported platform and repository metadata

- Target platforms: Enterprise Linux 9 and Enterprise Linux 10 (EL9/EL10).
- Python compatibility: CPython 3.9, 3.10, and 3.11 (`>=3.9,<3.12`).
- Repository metadata requirement: SQLite repodata entries only — `primary_db`, `other_db`, and `filelists_db` must be present in `repodata/repomd.xml`.

## Installation (EL9/EL10)

1. Build the RPM locally (see [Source archive and RPM build](#source-archive-and-rpm-build)).
2. Install it with `dnf`:

```bash
sudo dnf install /path/to/repoview-<version>-<release>.noarch.rpm
```

The RPM pulls required runtime dependencies (`python3-rpm`, `python3dist(jinja2)`, `python3dist(zstandard) >= 0.19`).

## Development and testing

clone this repo and use a virtual environment and a local install for development/testing:

```bash
mkdir -p $HOME/.venv
python3 -m venv $HOME/.venv/repoview
source $HOME/.venv/repoview/bin/activate
python -m pip install --upgrade pip
python -m pip install pytest
python -m pip install .
python -m pytest
```

## Source archive and RPM build

On an EL9/EL10 build host, ensure the `rpmbuild` tooling is available and that spec dependencies are installed (`python3` for build; runtime requirements in the resulting RPM are listed in `repoview.spec`).

Create the source archive expected by the spec:

```bash
./scripts/create-source-archive.sh
archive=$(./scripts/create-source-archive.sh)
```

Set up a private rpmbuild tree and copy the source tarball:

```bash
topdir="$PWD/.rpmbuild"
mkdir -p "$topdir"/{BUILD,BUILDROOT,RPMS,SOURCES,SPECS,SRPMS}
cp "$archive" "$topdir/SOURCES/"
```

Build the SRPM and binary RPM:

```bash
rpmbuild -bs --define "_topdir $topdir" repoview.spec
rpmbuild -bb --define "_topdir $topdir" repoview.spec
```

Artifacts are written under that `_topdir`:

- SRPM: `$topdir/SRPMS/repoview-<version>-<release>.<dist>.src.rpm`
- RPM: `$topdir/RPMS/noarch/repoview-<version>-<release>.<dist>.noarch.rpm`

## Usage

Minimal invocation against a repository root:

```bash
repoview /srv/repo
```

Set title and repository URL (URL enables RSS output):

```bash
repoview -t "My EL mirror" -u "https://mirror.example.com/el/repo" /srv/repo
```

Use a relative output directory (resolved inside the repository root):

```bash
repoview -o html/repoview /srv/repo
```

For `/srv/repo`, this writes to `/srv/repo/html/repoview`.

Exclude packages by glob and exclude architectures (quote globs so your shell does not expand them):

```bash
repoview -i '*debuginfo*' -i '*-devel*' -x src -x i686 /srv/repo
```

Use an alternative comps file:

```bash
repoview -c /path/to/comps.xml /srv/repo
```

See all options with `repoview --help` and `man repoview`.

## Authors

- Konstantin Ryabitsev
- Philippe Kueck

## Thanks

- Ville Skyttä
- Michael Schwendt

## URL

https://github.com/homi-mui/repoview

## Copyright and license

- Copyright (C) 2005 Duke University
- Copyright (C) 2006-2007 Konstantin Ryabitsev and contributors

For licensing and copying information see `COPYING`.
