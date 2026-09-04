Name:           repoview
Version:        0.7.2
Release:        1%{?dist}
Summary:        Generate static HTML pages for RPM repositories

License:        GPL-2.0-or-later
URL:            https://github.com/homi-mui/repoview
Source0:        %{name}-%{version}.tar.gz

BuildArch:      noarch

BuildRequires:  python3

Requires:       python3-rpm
Requires:       python3dist(jinja2)
Requires:       python3dist(zstandard) >= 0.19

%description
Repoview parses RPM repository metadata and generates a set of static HTML
pages that make the repository easier to browse over HTTP.

%prep
%autosetup

%build
:

%install
rm -rf %{buildroot}

install -d %{buildroot}%{_bindir} \
    %{buildroot}%{_datadir}/%{name} \
    %{buildroot}%{_mandir}/man8

install -pm 0755 repoview.py %{buildroot}%{_bindir}/%{name}
cp -a templates %{buildroot}%{_datadir}/%{name}/
install -pm 0644 repoview.8 %{buildroot}%{_mandir}/man8/%{name}.8

%files
%license COPYING
%doc README.md ChangeLog
%{_bindir}/repoview
%dir %{_datadir}/repoview
%dir %{_datadir}/repoview/templates
%{_datadir}/repoview/templates/group.j2
%{_datadir}/repoview/templates/index.j2
%dir %{_datadir}/repoview/templates/layout
%{_datadir}/repoview/templates/layout/repostyle.css
%{_datadir}/repoview/templates/package.j2
%{_datadir}/repoview/templates/rss.j2
%{_mandir}/man8/repoview.8*

%changelog
* Fri Sep 04 2026 GitHub Copilot <github-copilot[bot]@users.noreply.github.com> - 0.7.1-1
- Add EL9-oriented RPM packaging
