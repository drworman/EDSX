# EDSX

Convert [Odyssey Materials Helper](https://edomh.nl) ship share links into
[EDSY](https://edsy.org) SLEF JSON.

In the EDOMH Ship Editor tab, Options > Copy ship to Clipboard (Ctrl-c)

This gives you a URL, you can either use as the parameter or paste a
series of URLs into a file and pass the file as the parameter.

```
EDSX example-urls.txt
EDSX https://link.edomh.nl/f5aDKAAq
EDSX raw/*.edomh.json      # re-convert preserved payloads
```

Ships are written to `slef/`, and the decoded EDOMH payload for every link
is preserved under `raw/` before any conversion is attempted — a link that
fails to convert is never a link you have lost.

## Install

Download a binary from the
[releases page](https://github.com/drworman/EDSX/releases), or run the
script directly. It needs only the Python standard library:

```
python3 edsx.py --help
```

## How it works

Module and ship identifiers are resolved against EDSY's `eddb.js` and
EDCD's FDevIDs, downloaded on first run and cached. Nothing is hardcoded
per module: resolution matches on name, class, rating and mount, and
engineering blueprints are matched within the module type EDSY says they
are legal for. A module that cannot be identified confidently is reported
rather than guessed at.

Run `EDSX --selftest` to check the parser and resolver offline.

## Where EDSX keeps things

Data is stored in your Documents folder, under `EDSX/`:

| Platform | Location |
|---|---|
| Windows | `%USERPROFILE%\Documents\EDSX\` (follows a redirected or OneDrive Documents folder) |
| macOS | `~/Documents/EDSX/` |
| Linux | `$XDG_DOCUMENTS_DIR/EDSX/`, else `~/Documents/EDSX/` |

Delete the cache folder to force a fresh download, or pass
`--refresh-data`. Set `EDSX_HOME` to put it somewhere else — useful for
keeping the cache beside a portable binary on a memory stick.

Converted ships and preserved payloads are written to `slef/` and `raw/`

## Licence

MIT — see `LICENSE`. Released binaries embed the CPython runtime and the
PyInstaller bootloader; see `THIRD-PARTY-NOTICES.md` and `licenses/`.

Reference data is downloaded at runtime, not redistributed here. Elite
Dangerous is © Frontier Developments plc; this project is neither endorsed
by nor affiliated with them.
