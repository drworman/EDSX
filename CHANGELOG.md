# Changelog

## [20260903a]

### Fixed
- Slot names are taken from EDSY's `slotnames` overrides where a hull has
  them, instead of always being generated from the slot sizes. Hulls with
  bays named for their purpose rather than numbered — the Panther Mk II's
  `Cargo01`/`Cargo02`, the Lynx's `Passenger01`-`Passenger03`, the
  Type-11's `FighterBay01` and `LimpetController01` — were placing those
  modules in numbered slots and shifting every slot after them. Also
  affected hulls whose ordinary slots number with gaps (Type-10 Defender,
  Anaconda, Type-9, Type-7, Vulture, Asp Scout, Keelback, Federation
  Dropship) and hardpoint naming on the Type-8, Type-11 and Caspian.
- Two engineering modifiers were written under EDSY's display name or an
  inbound-only alias rather than the attribute's `fdattr`, so EDSY
  discarded them on import: damage falloff start is `FalloffRange`, not
  `DamageFalloffRange`, and typical emission range is `Range`, not
  `TypicalEmission`.

### Added
- `--selftest` covers slot-name overrides and the two corrected modifier
  labels.

## [20260903]

Initial public release.

### Added
- Convert EDOMH ship share links to EDSY SLEF JSON.
- `--version` and an offline `--selftest` covering reference parsing,
  module resolution, slot naming and engineering lookup.
- Previously decoded `.edomh.json` payloads are accepted as input, so a
  conversion can be retried without re-fetching.
- Single-file binaries for Linux, Windows and macOS.
