#!/usr/libexec/platform-python
# -*- mode: Python; indent-tabs-mode: nil; -*-
"""
Repoview is a small utility to generate static HTML pages for a repodata
directory, to make it easily browseable.

@author:    Konstantin Ryabitsev & contributors
@copyright: 2005 by Duke University, 2006-2007 by Konstantin Ryabitsev & co
@license:   GPL
"""
##
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Library General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA 02111-1307, USA.
#
# Copyright (C) 2005 by Duke University, http://www.duke.edu/
# Copyright (C) 2006 by McGill University, http://www.mcgill.ca/
# Copyright (C) 2007 by Konstantin Ryabitsev and contributors
# Author: Konstantin Ryabitsev <icon@fedoraproject.org>
#
#pylint: disable-msg=F0401

import bz2
import gzip
import hashlib as md5
import lzma
import os
from argparse import ArgumentParser
from contextlib import suppress
from dataclasses import dataclass
from functools import cmp_to_key
from pathlib import Path
import shutil
import sqlite3 as sqlite
import subprocess
import sys
import tempfile
import time
from typing import Dict, List, Optional, Sequence, Tuple
from xml.etree.ElementTree import ElementTree, ParseError, TreeBuilder

import jinja2

try:
    from rpm import labelCompare as rpm_label_compare
except ImportError:  # pragma: no cover - exercised via tests/CI without rpm
    rpm_label_compare = None

try:
    import zstandard
except ImportError:  # pragma: no cover - exercised conditionally in tests
    zstandard = None

##
# Some hardcoded constants
#
PKGKID = 'package.j2'
PKGFILE = '%s.html'
GRPKID = 'group.j2'
GRPFILE = '%s.group.html'
IDXKID = 'index.j2'
IDXFILE = 'index.html'
RSSKID = 'rss.j2'
RSSFILE = 'latest-feed.xml'
ISOFORMAT = '%a, %d %b %Y %H:%M:%S %z'
REPO_XML_NAMESPACE = 'http://linux.duke.edu/metadata/repo'

VERSION = '0.7.2'
SUPPORTED_DB_VERSION = 10
DEFAULT_TEMPLATEDIR = '/usr/share/repoview/templates'


class RepoviewError(Exception):
    """Raised for repository and rendering failures."""


@dataclass
class RepositoryMetadata:
    """Resolved SQLite metadata files from repomd.xml."""

    primary: Path
    other: Path
    filelists: Path
    dbversion: int
    comps: Optional[Path] = None


def _mkid(text):
    """
    Make a web-friendly filename out of group names and package names.

    @param text: the text to clean up
    @type  text: str

    @return: a web-friendly filename
    @rtype:  str
    """
    text = text.replace('/', '.')
    text = text.replace(' ', '_')
    return text


def _humansize(bytez):
    """
    This will return the size in sane units (KiB or MiB).

    @param bytes: number of bytes
    @type  bytes: int

    @return: human-readable string
    @rtype:  str
    """
    if bytez < 1024:
        return '%d Bytes' % bytez
    bytez = int(bytez)
    kbytes = bytez / 1024
    if kbytes / 1024 < 1:
        return '%d KiB' % kbytes
    return '%0.1f MiB' % (float(kbytes) / 1024)


def _extract_xml_namespace(tag):
    if tag.startswith('{') and '}' in tag:
        return tag[1:].split('}', 1)[0]
    return None


def _coerce_repo_relative_path(repodir, href):
    return (repodir / href).resolve()


def _join_filelist_path(dirname, filename):
    dirname = dirname or ''
    if not filename:
        return dirname or '/'
    if dirname in ('', '/'):
        return '/%s' % filename.lstrip('/')
    return '%s/%s' % (dirname.rstrip('/'), filename)


def parse_filelist_entries(dirname, filenames, filetypes):
    """Return aligned filelist entries from sqlite metadata fields."""
    entries = []
    parts = (filenames or '').split('/')
    for index, filename in enumerate(parts):
        if filetypes and index < len(filetypes) and filetypes[index]:
            filetype = filetypes[index]
        else:
            filetype = 'file'
        entries.append((filetype, _join_filelist_path(dirname, filename)))
    return entries


def resolve_output_dir(repodir, outdir):
    """Resolve output paths, keeping relative paths inside the repository."""
    repodir = repodir.resolve()
    outdir_path = Path(outdir or 'repoview').expanduser()
    if not outdir_path.is_absolute():
        outdir_path = repodir / outdir_path
    outdir_path = outdir_path.resolve()
    if outdir_path == repodir:
        raise RepoviewError('--output-dir must not resolve to the repository root')
    return outdir_path


def read_repomd_metadata(repodir):
    """Read required SQLite repository metadata from repodata/repomd.xml."""
    repodir = repodir.resolve()
    repomd_path = repodir / 'repodata' / 'repomd.xml'
    if not repomd_path.is_file():
        raise RepoviewError(
            'Not found: %s\nDoes not look like a repository.' % repomd_path
        )

    try:
        root = ElementTree(file=str(repomd_path)).getroot()
    except ParseError as exc:
        raise RepoviewError('Failed to parse %s: %s' % (repomd_path, exc))

    namespace = _extract_xml_namespace(root.tag)
    if namespace != REPO_XML_NAMESPACE:
        if namespace is None:
            raise RepoviewError(
                'repomd.xml is missing the expected "%s" namespace.'
                % REPO_XML_NAMESPACE
            )
        raise RepoviewError(
            'repomd.xml uses unsupported namespace "%s".' % namespace
        )

    metadata = {
        'primary_db': None,
        'other_db': None,
        'filelists_db': None,
        'group': None,
    }
    dbversion = None
    data_tag = '{%s}data' % namespace
    location_tag = '{%s}location' % namespace
    dbversion_tag = '{%s}database_version' % namespace

    for datanode in root.findall(data_tag):
        entry_type = datanode.attrib.get('type')
        location = datanode.find(location_tag)
        if location is None or 'href' not in location.attrib:
            raise RepoviewError(
                'repomd.xml entry %r is missing a location href.' % entry_type
            )
        href = location.attrib['href']
        if entry_type in metadata:
            metadata[entry_type] = _coerce_repo_relative_path(repodir, href)
        if entry_type == 'primary_db':
            dbversion_text = datanode.findtext(dbversion_tag)
            if not dbversion_text:
                raise RepoviewError(
                    'repomd.xml entry "primary_db" is missing a database_version.'
                )
            try:
                dbversion = int(dbversion_text)
            except ValueError:
                raise RepoviewError(
                    'repomd.xml entry "primary_db" has an invalid '
                    'database_version %r.' % dbversion_text
                )

    missing = [
        entry_type
        for entry_type in ('primary_db', 'other_db', 'filelists_db')
        if metadata[entry_type] is None
    ]
    if missing:
        raise RepoviewError(
            'repomd.xml is missing required SQLite metadata entries: %s.'
            % ', '.join(missing)
        )

    return RepositoryMetadata(
        primary=metadata['primary_db'],
        other=metadata['other_db'],
        filelists=metadata['filelists_db'],
        dbversion=dbversion,
        comps=metadata['group'],
    )


class Repoview:
    """
    The working horse class.
    """

    def __init__(self, opts):
        """
        @param opts: ArgumentParser's opts
        @type  opts: ArgumentParser
        """
        self.opts = opts
        self.repodir = Path(opts.repodir).resolve()
        self.templatedir = Path(opts.templatedir).expanduser().resolve()
        self.outdir = resolve_output_dir(self.repodir, opts.outdir)

        self.cleanup = []
        self.exclude_clauses = []
        self.exclude_params = []
        self.state_data = {}
        self.written = {}
        self.groups = []
        self.letter_groups = []

        self.pconn = None  # primary.sqlite
        self.oconn = None  # other.sqlite
        self.sconn = None  # state db
        self.fconn = None  # filelists.sqlite

        def ymd(stamp):
            return time.strftime('%Y-%m-%d', time.localtime(int(stamp)))

        self.j2loader = jinja2.FileSystemLoader(str(self.templatedir))
        self.j2env = jinja2.Environment(
            autoescape=True,
            trim_blocks=True,
            loader=self.j2loader,
        )
        self.j2env.filters['ymd'] = ymd

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, exc_tb):
        self.close()
        return False

    def close(self):
        """Close sqlite handles and remove any temporary metadata files."""
        for conn in (self.pconn, self.oconn, self.fconn, self.sconn):
            if conn is not None:
                conn.close()
        self.pconn = self.oconn = self.fconn = self.sconn = None

        for entry in self.cleanup:
            with suppress(FileNotFoundError):
                Path(entry).unlink()
        self.cleanup = []

    def run(self):
        self.setup_repo()
        self.setup_outdir()
        self.setup_state_db()
        self.setup_excludes()

        if not self.groups:
            self.setup_rpm_groups()

        letters = self.setup_letter_groups()

        repo_data = {
            'title': self.opts.title,
            'letters': letters,
            'my_version': VERSION,
            'env': {},
        }

        try:
            assert self.opts.env is not None
            repo_data['env'] = {
                e.split('=', 1)[0]: e.split('=', 1)[1] for e in self.opts.env
            }
        except AssertionError:
            pass
        except IndexError:
            raise RepoviewError('invalid environment arguments.')

        group_template = self.j2env.get_template(GRPKID)
        package_template = self.j2env.get_template(PKGKID)

        group_count = 0
        for group_values in list(self.groups + self.letter_groups):
            (grp_name, grp_filename, grp_description, pkgnames) = group_values
            group_data = {
                'name': grp_name,
                'description': grp_description,
                'filename': grp_filename,
            }
            packages = self.do_packages(
                repo_data,
                group_data,
                sorted(pkgnames),
                package_template,
            )
            if not packages:
                if group_count < len(self.groups):
                    del self.groups[group_count]
                continue

            group_count += 1
            group_data['packages'] = packages

            checksum = self.mk_checksum(repo_data, group_data)
            if self.has_changed(grp_filename, checksum):
                self.say('Writing group %s\n' % grp_filename)
                self.render_template_to_path(
                    group_template,
                    self._output_path_for(grp_filename),
                    repo_data=repo_data,
                    group_data=group_data,
                )

        latest = self.get_latest_packages()
        repo_data['latest'] = latest
        repo_data['groups'] = self.groups

        checksum = self.mk_checksum(repo_data)
        if self.has_changed(IDXFILE, checksum):
            self.say('Writing index.html...')
            self.render_template_to_path(
                self.j2env.get_template(IDXKID),
                self._output_path_for(IDXFILE),
                repo_data=repo_data,
                url=self.opts.url,
                latest=latest,
                groups=self.groups,
                time=time.strftime('%Y-%m-%d'),
            )
            self.say('done\n')

            if self.opts.url:
                self.do_rss(repo_data, latest)

        self.remove_stale()
        self.sconn.commit()

    def say(self, text):
        """
        Unless in quiet mode, output the text passed.

        @param text: something to say
        @type  text: str

        @rtype: void
        """
        if not self.opts.quiet:
            sys.stdout.write(text)

    def _output_path_for(self, filename):
        candidate = (self.outdir / filename).resolve()
        outdir = self.outdir.resolve()
        if candidate != outdir and outdir not in candidate.parents:
            raise RepoviewError(
                'Refusing to access output path outside %s: %s'
                % (outdir, filename)
            )
        return candidate

    def render_template_to_path(self, template, destination, **context):
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open('w', encoding='utf-8') as fh:
            fh.write(template.render(**context))

    def setup_state_db(self):
        """
        Sets up the state-tracking database.

        @rtype: void
        """
        self.say('Examining state db...')
        if self.opts.statedir:
            statedir = Path(self.opts.statedir).expanduser()
            statedir.mkdir(parents=True, exist_ok=True)
            unique = '%s.state.sqlite' % md5.md5(
                str(self.outdir).encode('utf-8')
            ).hexdigest()
            statedb = statedir / unique
        else:
            statedb = self.outdir / 'state.sqlite'

        statedb.parent.mkdir(parents=True, exist_ok=True)
        if statedb.exists():
            if self.opts.force:
                statedb.unlink()
        else:
            self.opts.force = True

        self.sconn = sqlite.connect(str(statedb))
        scursor = self.sconn.cursor()
        scursor.execute(
            """CREATE TABLE IF NOT EXISTS state (
                   filename TEXT UNIQUE,
                   checksum TEXT)"""
        )
        scursor.execute("SELECT filename, checksum FROM state")
        for filename, checksum in scursor.fetchall():
            self.state_data[filename] = checksum
        self.say('done\n')

    def setup_repo(self):
        """
        Examines the repository, makes sure that it's valid and supported,
        and then opens the necessary databases.

        @rtype: void
        """
        self.say('Examining repository...')
        metadata = read_repomd_metadata(self.repodir)
        if metadata.dbversion > SUPPORTED_DB_VERSION:
            raise RepoviewError(
                'Sorry, the db_version in the repository is %s, but repoview '
                'only supports versions up to %s. Please check for a newer '
                'repoview version.' % (metadata.dbversion, SUPPORTED_DB_VERSION)
            )
        self.say('done\n')

        self.say('Opening primary database...')
        self.pconn = self._open_metadata_database(metadata.primary)
        self.say('done\n')

        self.say('Opening changelogs database...')
        self.oconn = self._open_metadata_database(metadata.other)
        self.say('done\n')

        self.say('Opening filelists database...')
        self.fconn = self._open_metadata_database(metadata.filelists)
        self.say('done\n')

        comps = metadata.comps
        if self.opts.comps:
            comps = Path(self.opts.comps).expanduser().resolve()
        if comps:
            self.setup_comps_groups(comps)

    def _open_metadata_database(self, dbfile):
        local_path = self.z_handler(Path(dbfile))
        return sqlite.connect(str(local_path))

    def setup_excludes(self):
        """
        Formulates exclusion clauses used by package queries.

        @rtype: void
        """
        self.exclude_clauses = []
        self.exclude_params = []
        for xarch in self.opts.xarch:
            self.exclude_clauses.append('arch != ?')
            self.exclude_params.append(xarch)
        for pkg in self.opts.ignore:
            self.exclude_clauses.append('name NOT LIKE ?')
            self.exclude_params.append(pkg.replace('*', '%'))

    def _build_where(self, clauses=None, params=None):
        all_clauses = list(clauses or [])
        all_params = list(params or [])
        all_clauses.extend(self.exclude_clauses)
        all_params.extend(self.exclude_params)
        if all_clauses:
            return ' WHERE ' + ' AND '.join(all_clauses), all_params
        return '', all_params

    def setup_outdir(self):
        """
        Sets up the output directory.

        @rtype: void
        """
        if self.opts.force and self.outdir.exists():
            shutil.rmtree(self.outdir)
        self.outdir.mkdir(parents=True, exist_ok=True)

        layoutsrc = self.templatedir / 'layout'
        layoutdst = self.outdir / 'layout'
        if layoutsrc.is_dir() and not layoutdst.exists():
            self.say('Copying layout...')
            shutil.copytree(str(layoutsrc), str(layoutdst))
            self.say('done\n')

    def get_package_data(self, pkgname):
        """
        Queries the packages and changelog databases and returns package data.
        """
        where_sql, params = self._build_where(['name = ?'], [pkgname])
        query = (
            'SELECT pkgKey, epoch, version, release, arch, summary, '
            'description, url, time_build, rpm_license, rpm_sourcerpm, '
            'size_package, location_href, rpm_vendor '
            'FROM packages%s ORDER BY arch ASC' % where_sql
        )
        pcursor = self.pconn.cursor()
        pcursor.execute(query, params)
        rows = pcursor.fetchall()
        if not rows:
            return None

        if len(rows) == 1:
            versions = [rows[0]]
        else:
            if rpm_label_compare is None:
                raise RepoviewError(
                    'python3-rpm is required to compare RPM versions.'
                )
            temp = {}
            for row in rows:
                temp[(row[1], row[2], row[3], row[4])] = row

            keys = list(temp.keys())
            keys.sort(
                key=cmp_to_key(lambda a, b: rpm_label_compare(a[:3], b[:3])),
                reverse=True,
            )
            versions = [temp[key] for key in keys]

        pkg_filename = _mkid(PKGFILE % pkgname)
        pkg_data = {
            'name': pkgname,
            'filename': pkg_filename,
            'summary': None,
            'description': None,
            'url': None,
            'rpm_license': None,
            'rpm_sourcerpm': None,
            'vendor': None,
            'rpms': [],
        }

        ocursor = self.oconn.cursor()
        fcursor = self.fconn.cursor()
        for row in versions:
            (pkg_key, epoch, version, release, arch, summary,
             description, url, time_build, rpm_license, rpm_sourcerpm,
             size_package, location_href, vendor) = row
            if pkg_data['summary'] is None:
                pkg_data['summary'] = summary
                pkg_data['description'] = description
                pkg_data['url'] = url
                pkg_data['rpm_license'] = rpm_license
                pkg_data['rpm_sourcerpm'] = rpm_sourcerpm
                pkg_data['vendor'] = vendor

            size = _humansize(size_package)
            ocursor.execute(
                'SELECT author, date, changelog FROM changelog '
                'WHERE pkgKey = ? ORDER BY date DESC LIMIT 1',
                (pkg_key,),
            )
            orow = ocursor.fetchone()
            if not orow:
                author = time_added = changelog = None
            else:
                (author, time_added, changelog) = orow
                try:
                    author = author[:author.index('<')].strip()
                except (TypeError, ValueError):
                    pass

            fcursor.execute(
                'SELECT dirname, filenames, filetypes FROM filelist '
                'WHERE pkgKey = ? ORDER BY dirname DESC',
                (pkg_key,),
            )
            filelist = []
            for dirname, filenames, filetypes in fcursor.fetchall():
                filelist.extend(
                    parse_filelist_entries(dirname, filenames, filetypes)
                )

            pkg_data['rpms'].append(
                (epoch, version, release, arch,
                 time_build, size, location_href,
                 author, changelog, time_added, filelist)
            )
        return pkg_data

    def do_packages(self, repo_data, group_data, pkgnames, package_template):
        """
        Iterate through package names and write the ones that changed.
        """
        pkg_tuples = []
        for pkgname in pkgnames:
            pkg_filename = _mkid(PKGFILE % pkgname)
            if pkgname in self.written:
                pkg_tuples.append(self.written[pkgname])
                continue

            pkg_data = self.get_package_data(pkgname)
            if pkg_data is None:
                continue

            pkg_tuple = (pkgname, pkg_filename, pkg_data['summary'])
            pkg_tuples.append(pkg_tuple)
            checksum = self.mk_checksum(repo_data, group_data, pkg_data)
            if self.has_changed(pkg_filename, checksum):
                self.say('Writing package %s\n' % pkg_filename)
                self.render_template_to_path(
                    package_template,
                    self._output_path_for(pkg_filename),
                    repo_data=repo_data,
                    group_data=group_data,
                    pkg_data=pkg_data,
                )
            self.written[pkgname] = pkg_tuple
        return pkg_tuples

    def mk_checksum(self, *args):
        """
        A fairly dirty function used for state tracking.
        """
        mangle = []
        for data in args:
            for key in sorted(data.keys()):
                mangle.append(data[key])
        return md5.md5(str(mangle).encode()).hexdigest()

    def has_changed(self, filename, checksum):
        """
        Figure out if the contents of the filename have changed.
        """
        scursor = self.sconn.cursor()
        if filename not in self.state_data:
            scursor.execute(
                'INSERT INTO state (filename, checksum) VALUES (?, ?)',
                (filename, checksum),
            )
            return True
        if self.state_data[filename] != checksum:
            scursor.execute(
                'UPDATE state SET checksum = ? WHERE filename = ?',
                (checksum, filename),
            )
            del self.state_data[filename]
            return True
        del self.state_data[filename]
        return False

    def remove_stale(self):
        """
        Remove errant stale files from the output directory.
        """
        scursor = self.sconn.cursor()
        for filename in list(self.state_data.keys()):
            self.say('Removing stale file %s\n' % filename)
            fullpath = self._output_path_for(filename)
            if fullpath.exists():
                fullpath.unlink()
            scursor.execute('DELETE FROM state WHERE filename = ?', (filename,))

    def _decompress_to_tempfile(self, dbfile, copy_func):
        fd, unzname = tempfile.mkstemp(suffix='.repoview')
        os.close(fd)
        temp_path = Path(unzname)
        try:
            copy_func(temp_path)
        except Exception:
            with suppress(FileNotFoundError):
                temp_path.unlink()
            raise
        self.cleanup.append(temp_path)
        return temp_path

    def _decompress_zstd(self, dbfile):
        if zstandard is not None:
            def copy_func(temp_path):
                with dbfile.open('rb') as source, temp_path.open('wb') as dest:
                    dctx = zstandard.ZstdDecompressor()
                    with dctx.stream_reader(source) as reader:
                        shutil.copyfileobj(reader, dest)
            return self._decompress_to_tempfile(dbfile, copy_func)

        if shutil.which('zstd') is None:
            raise RepoviewError(
                'Repository metadata %s is Zstandard-compressed, but neither '
                'the Python zstandard module nor the external "zstd" command '
                'is available.' % dbfile
            )

        def copy_func(temp_path):
            with temp_path.open('wb') as unzfd:
                subprocess.run(
                    ['zstd', '--quiet', '--decompress', '--stdout', str(dbfile)],
                    check=True,
                    stdout=unzfd,
                )

        try:
            return self._decompress_to_tempfile(dbfile, copy_func)
        except subprocess.CalledProcessError as exc:
            raise RepoviewError(
                'Failed to decompress Zstandard metadata %s: %s' % (dbfile, exc)
            )

    def z_handler(self, dbfile):
        """
        Return an uncompressed metadata file.

        Supports gzip, bzip2, xz, and Zstandard-compressed files.
        """
        suffix = dbfile.suffix.lower()
        if suffix == '.zst':
            return self._decompress_zstd(dbfile)

        openers = {
            '.bz2': bz2.open,
            '.gz': gzip.open,
            '.xz': lzma.open,
        }
        opener = openers.get(suffix)
        if opener is None:
            return dbfile

        def copy_func(temp_path):
            with opener(str(dbfile), 'rb') as zfd, temp_path.open('wb') as unzfd:
                shutil.copyfileobj(zfd, unzfd)

        return self._decompress_to_tempfile(dbfile, copy_func)

    def setup_comps_groups(self, compsxml):
        """
        Parse Comps XML without the removed EL7 yum Python API.

        @param compsxml: location of comps.xml
        @type compsxml: str
        """
        self.say('Parsing comps.xml...')
        compsxml = self.z_handler(Path(compsxml))
        root = ElementTree(file=str(compsxml)).getroot()
        for group in root.findall('group'):
            groupid = group.findtext('id')
            name = group.findtext('name')
            description = group.findtext('description')
            user_visible = group.findtext('uservisible', default='true').lower()
            if not groupid or not name or user_visible != 'true':
                continue

            packages = [
                package.text
                for package in group.findall('./packagelist/packagereq')
                if package.text
            ]
            if not packages:
                continue

            group_filename = _mkid(GRPFILE % groupid)
            self.groups.append([name, group_filename, description, packages])
        self.say('done\n')

    def setup_rpm_groups(self):
        """
        When comps is not around, we use the (useless) RPM groups.

        @rtype: void
        """
        self.say('Collecting group information...')
        pcursor = self.pconn.cursor()
        pcursor.execute(
            'SELECT DISTINCT lower(rpm_group) AS rpm_group '
            'FROM packages ORDER BY rpm_group ASC'
        )
        for (rpmgroup,) in pcursor.fetchall():
            if rpmgroup is None:
                continue
            where_sql, params = self._build_where(
                ['lower(rpm_group) = ?'],
                [rpmgroup],
            )
            pcursor.execute(
                'SELECT DISTINCT name FROM packages%s ORDER BY name' % where_sql,
                params,
            )
            pkgnames = [pkgname for (pkgname,) in pcursor.fetchall()]
            group_filename = _mkid(GRPFILE % rpmgroup)
            self.groups.append([rpmgroup, group_filename, None, pkgnames])
        self.say('done\n')

    def get_latest_packages(self, limit=30):
        """
        Return necessary data for the latest NN packages.
        """
        self.say('Collecting latest packages...')
        where_sql, params = self._build_where()
        query = (
            'SELECT name FROM packages%s GROUP BY name '
            'ORDER BY MAX(time_build) DESC LIMIT ?' % where_sql
        )
        pcursor = self.pconn.cursor()
        pcursor.execute(query, params + [limit])

        latest = []
        for (pkgname,) in pcursor.fetchall():
            filename = _mkid(PKGFILE % pkgname)
            pcursor.execute(
                'SELECT version, release, time_build FROM packages '
                'WHERE name = ? ORDER BY time_build DESC LIMIT 1',
                (pkgname,),
            )
            (version, release, built) = pcursor.fetchone()
            latest.append((pkgname, filename, version, release, built))

        self.say('done\n')
        return latest

    def setup_letter_groups(self):
        """
        Figure out which letters we have and set up the necessary groups.
        """
        self.say('Collecting letters...')
        where_sql, params = self._build_where()
        pcursor = self.pconn.cursor()
        pcursor.execute(
            'SELECT DISTINCT substr(upper(name), 1, 1) AS letter '
            'FROM packages%s ORDER BY letter' % where_sql,
            params,
        )
        letters = ''
        for (letter,) in pcursor.fetchall():
            letters += letter
            rpmgroup = 'Letter %s' % letter
            description = 'Packages beginning with letter "%s".' % letter
            letter_where_sql, letter_params = self._build_where(
                ['name LIKE ?'],
                ['%s%%' % letter],
            )
            pcursor.execute(
                'SELECT DISTINCT name FROM packages%s' % letter_where_sql,
                letter_params,
            )
            pkgnames = [pkgname for (pkgname,) in pcursor.fetchall()]
            group_filename = _mkid(GRPFILE % rpmgroup).lower()
            self.letter_groups.append(
                (rpmgroup, group_filename, description, pkgnames)
            )
        self.say('done\n')
        return letters

    def do_rss(self, repo_data, latest):
        """
        Write the RSS feed.
        """
        self.say('Generating rss feed...')
        etb = TreeBuilder()
        etb.start('rss', {'version': '2.0'})
        etb.start('channel', {})
        etb.start('title', {})
        etb.data(repo_data['title'])
        etb.end('title')
        etb.start('link', {})
        etb.data('%s/repoview/%s' % (self.opts.url, RSSFILE))
        etb.end('link')
        etb.start('description', {})
        etb.data('Latest packages for %s' % repo_data['title'])
        etb.end('description')
        etb.start('lastBuildDate', {})
        etb.data(time.strftime(ISOFORMAT))
        etb.end('lastBuildDate')
        etb.start('generator', {})
        etb.data('Repoview-%s' % repo_data['my_version'])
        etb.end('generator')

        rss_template = self.j2env.get_template(RSSKID)
        for row in latest:
            pkg_data = self.get_package_data(row[0])
            rpm = pkg_data['rpms'][0]
            (epoch, version, release, arch, built) = rpm[:5]
            etb.start('item', {})
            etb.start('guid', {})
            etb.data(
                '%s/repoview/%s+%s:%s-%s.%s'
                % (self.opts.url, pkg_data['filename'], epoch, version, release, arch)
            )
            etb.end('guid')
            etb.start('link', {})
            etb.data('%s/repoview/%s' % (self.opts.url, pkg_data['filename']))
            etb.end('link')
            etb.start('pubDate', {})
            etb.data(time.strftime(ISOFORMAT, time.gmtime(int(built))))
            etb.end('pubDate')
            etb.start('title', {})
            etb.data('Update: %s-%s-%s' % (pkg_data['name'], version, release))
            etb.end('title')
            etb.start('description', {})
            etb.data(
                rss_template.render(
                    repo_data=repo_data,
                    url=self.opts.url,
                    pkg_data=pkg_data,
                )
            )
            etb.end('description')
            etb.end('item')

        etb.end('channel')
        etb.end('rss')
        etree = ElementTree(etb.close())
        etree.write(str(self._output_path_for(RSSFILE)), 'utf-8')
        self.say('done\n')


def build_parser():
    """Create the CLI argument parser."""
    parser = ArgumentParser()
    parser.add_argument('repodir', help='path to the repository')
    parser.add_argument('--version', action='version', version='%(prog)s ' + VERSION)
    parser.add_argument(
        '-q', '--quiet',
        dest='quiet',
        action='store_true',
        help='Do not output anything except fatal errors.',
    )
    parser.add_argument(
        '-f', '--force',
        dest='force',
        action='store_true',
        help='Regenerate the pages even if the repomd checksum has not changed',
    )
    parser.add_argument(
        '-s', '--state-dir',
        dest='statedir',
        help='Create the state-tracking db in this directory '
             '(default: store in output directory)',
    )

    repo_opts = parser.add_argument_group('repository specific options')
    repo_opts.add_argument(
        '-i', '--ignore-package',
        dest='ignore',
        action='append',
        default=[],
        help='Optionally ignore package names using shell-style globs. '
             'This is useful for excluding debuginfo packages, e.g.: '
             '"-i *debuginfo* -i *doc*".',
    )
    repo_opts.add_argument(
        '-x', '--exclude-arch',
        dest='xarch',
        action='append',
        default=[],
        help='Optionally exclude this arch. E.g.: "-x src -x ia64"',
    )
    repo_opts.add_argument(
        '-c', '--comps',
        dest='comps',
        help='Use an alternative comps.xml file (default: off)',
    )

    tpl_opts = parser.add_argument_group('template specific options')
    tpl_opts.add_argument(
        '-k', '--template-dir',
        dest='templatedir',
        default=DEFAULT_TEMPLATEDIR,
        help='Use an alternative directory with Jinja2 templates instead of '
             'the default: %(default)s. The template directory must contain '
             'index.j2, group.j2, package.j2, rss.j2, and the "layout" '
             'directory that will be copied into the output directory.',
    )
    tpl_opts.add_argument(
        '-o', '--output-dir',
        dest='outdir',
        default='repoview',
        help='Write the generated output to this directory. Relative paths '
             'are created inside the repository root (default: "%(default)s").',
    )
    tpl_opts.add_argument(
        '-t', '--title',
        dest='title',
        default='Repoview',
        help='Describe the repository in a few words. By default, '
             '"%(default)s" is used. E.g.: -t "Extras for Fedora Core 4 x86"',
    )
    tpl_opts.add_argument(
        '-E', '--environment',
        dest='env',
        action='append',
        help='Add environment variables for usage in templates. '
             'E.g.: -E "foo=bar" -E "baz=yatta"',
    )

    rss_opts = parser.add_argument_group('RSS specific options')
    rss_opts.add_argument(
        '-u', '--url',
        dest='url',
        help='Repository URL to use when generating the RSS feed. E.g.: '
             '-u "http://fedoraproject.org/extras/4/i386". Leaving it off '
             'will skip RSS feed generation',
    )
    return parser


def main(argv=None):
    """Parse options and invoke Repoview."""
    parser = build_parser()
    opts = parser.parse_args(argv)
    try:
        with Repoview(opts) as app:
            app.run()
    except RepoviewError as exc:
        sys.stderr.write('%s\n' % exc)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
