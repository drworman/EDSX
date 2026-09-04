# Third-party notices

EDSX itself depends only on the Python standard library. It bundles
no third-party code at source level.

## Distributed binaries

The released binaries are produced with PyInstaller, which embeds a
compiled bootloader and a copy of the CPython runtime.

- **CPython** — Python Software Foundation License, version 2.
  See `licenses/PYTHON-LICENSE.txt`.
- **PyInstaller bootloader** — GPLv2-or-later with an explicit exception
  permitting distribution of non-GPL applications frozen with it.
  See `licenses/PYINSTALLER-LICENSE.txt`.

Running `edsx.py` directly with your own Python interpreter involves
neither of these.

## Reference data

EDSX downloads reference data at runtime and caches it locally. That
data is not redistributed as part of this project and is not covered by
this project's licence.

- **EDSY** (`eddb.js`) — © taleden, CC BY-NC 4.0 for the design, markup and
  script code. The Elite Dangerous game data within it remains the property
  of Frontier Developments plc and is used with permission.
- **FDevIDs** (`outfitting.csv`, `shipyard.csv`) — maintained by EDCD.

Elite Dangerous is © Frontier Developments plc. This project is neither
endorsed by nor affiliated with Frontier Developments.
