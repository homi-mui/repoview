# repoview

### ABOUT

This is a small software written to parse yum repositories and present them in a
format that's easily browsable via http by creating a set of static HTML pages.

### REQUIREMENTS

  * python3 (CPython 3.9-3.11)
  * python3-jinja2
  * python3-rpm
  * python3-zstandard (preferred for .zst metadata; otherwise an external `zstd` command is required)
  * sqlite3

### AUTHORS

  * Konstantin Ryabitsev
  * Philippe Kueck

### THANKS

  * Ville Skyttä
  * Michael Schwendt

### URL

https://github.com/philfry/repoview

### COPYRIGHT AND LICENSE

  * Copyright (C) 2005 by Duke University
  * Copyright (C) 2006-2007 by Konstantin Ryabitsev and contributors

For licensing and copying information see COPYING.

### USAGE

See **repoview(8)** or `repoview --help`.
