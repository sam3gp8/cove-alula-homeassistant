# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). The
`version` in `custom_components/cove_alula/manifest.json` and the git tag (`vX.Y.Z`) are
kept in sync with the headings below — HACS installs the latest GitHub release and shows
its notes, so each release's section is what users read before updating.

## [Unreleased]

_Nothing yet._

---

## [0.11.1] - 2026-08-08

### Fixed
- **Arm/disarm did nothing on some ConnectFlex panels** (rejected as "Unsupported command",
  issue #3). 0.11.0 detected ConnectFlex only from the `connectedPanel` id, which not every
  ConnectFlex variant reports. Panel-family detection is now robust:
  - It also recognizes a partition panel by its read capabilities (answers `partitionStatus`
    but NAKs `panelStatus`), independent of the `connectedPanel` string.
  - The arming command is now **self-correcting**: if the panel rejects a command as
    unsupported, the integration automatically retries with the other family's command and
    remembers which one that panel accepts, so subsequent commands go straight to it.

### Changed
- Arm/disarm now waits for the panel's acknowledgement (so a failure surfaces to the
  service call instead of silently doing nothing), which is also what enables the fallback.
- Diagnostics now include each panel's `connected_panel` family id and
  `supports_partition_arming`, so panel-family issues are diagnosable from the report.

---

## [0.11.0] - 2026-08-03

### Added
- **ConnectFlex panel support.** Alula panels come in different families that use different
  arming commands. The integration now detects the panel family (from the `connectedPanel`
  attribute) and, for ConnectFlex-family panels, arms via `partitionArmingLevelChange`
  (string level + partitions, armed by user number, PIN only for disarm) instead of the
  Helix `changeArmingLevelUsingCode`. Helix panels are unchanged. Thanks to @rsseligman1962
  and @conorsham for the panel data and initial implementation (PR #4).

### Fixed
- **Device discovery read the wrong attribute name.** The panel filter checked snake_case
  `is_panel`, but the cloud sends camelCase `isPanel`; panels were previously included only
  by the "not a camera" fallback. It now reads `isPanel`/`isCamera` correctly, trusts an
  explicit panel flag when present, and keeps the lenient fallback for accounts that omit
  the flag — so both Helix and ConnectFlex panels are found.

---

## [0.10.1] - 2026-07-28

### Fixed
- **Setup failed with `invalid_grant: refresh token is invalid` and did not recover on its
  own.** When the stored refresh token is rejected (expired, revoked, or already rotated),
  the integration now automatically falls back to a full login using the stored
  credentials instead of failing setup. A full re-authentication prompt is only raised if
  that login *also* fails (genuinely wrong credentials).
- All token refreshes now run under the auth lock, so concurrent paths (setup, poll,
  reconnect, watchdog) can't burn a single-use refresh token by refreshing at the same
  time — a way the stored token could become invalid between restarts.

### Changed
- **Download diagnostics now works even when the entry failed to set up.** Previously the
  report assumed a live connection and would error; it now returns the config-entry state
  and the setup error reason (plus redacted entry data) when the integration isn't loaded,
  which is exactly when diagnostics are most useful. (The button itself only appears once
  the integration loads; with the auth fix above, a stuck entry should now load.)

---

## [0.10.0] - 2026-07-22

### Added
- **Download diagnostics.** A "Download diagnostics" button now appears on the
  integration's config-entry page and on each device page. The report is designed to be
  safe to attach to a bug report: credentials (account number, email, password, PIN,
  token), the panel's device id, and all panel and zone **names** are redacted. What it
  keeps is what's useful for debugging — integration version, websocket/token connection
  state, which panel-data reads the panel answered vs NAKed vs never returned, arming
  level and troubles, and per-zone type and state keyed by zone index.

---

## [0.9.2] - 2026-07-22

### Fixed
- **Config entry setup could be cancelled by Home Assistant** (`asyncio.CancelledError`
  raised mid-read, typically on `panelStatus`). Setup blocked for up to ~45 seconds
  pulling the full panel/zone snapshot before returning; Home Assistant cancels
  config-entry setup that stalls that long. Setup now returns as soon as the quick work
  (authenticate, discover panels, connect the socket, subscribe) is done.
- A transient socket error during the initial refresh (for example `ConnectionResetError`)
  could abort setup — the error guard only caught `CoveAlulaError`.
- Zone loading ran outside that error guard, so a failure there escaped and killed setup.

### Changed
- The full snapshot (panel name, firmware, zone names/config/status) now runs in a
  background task tied to the config entry. Entities are created immediately and fill in
  as responses and live pushes arrive.
- `async_refresh_state()` takes an overall time budget (default 30s; 45s for the initial
  bootstrap) so a slow or unresponsive cloud can never park it indefinitely.
- The coordinator poll is capped at 45s and keeps the last known state on timeout instead
  of flapping entities to *unavailable*.
- Reconnect reconcile waits reduced from 8s to 5s each, keeping the first refresh well
  inside Home Assistant's patience.
- Authentication failures now raise `ConfigEntryAuthFailed`, which starts Home Assistant's
  re-authentication flow; transient failures raise `ConfigEntryNotReady`, so Home
  Assistant retries setup with backoff instead of giving up.
- Cancellation is explicitly re-raised everywhere it is caught, so Home Assistant shutdown
  and config-entry reloads behave correctly.

---

## [0.9.1] - 2026-07-22

### Fixed
- **Arming Away and Night were reversed.** On-device testing confirmed this panel family
  uses `armingLevelValue` **3 = Night** and **4 = Away** — the reverse of the decompiled
  enum's nominal ordering, which the original mapping had followed. Both the arming
  commands and the state read-back are corrected, so the button you press and the state
  Home Assistant displays now match the keypad.

### Changed
- The `force_arm` service's mode mapping is now derived from the client's level constants
  instead of hardcoded numbers, so the command path and read-back path can never drift
  apart again.
- Corrected the arming-level table in `cove_alula_protocol.md`.

---

## [0.9.0] - 2026-07-22

### Fixed
- **Zones could stay stuck "Open" in Home Assistant after being closed physically.** The
  websocket was being dropped roughly every 13 minutes (access-token expiry). Reconnects
  re-subscribed but never re-read state, and subscribing only registers for *future*
  pushes — so a close event that occurred during the ~15–20s gap was lost permanently
  until the zone was toggled again.

### Added
- **Reconcile on every reconnect.** After the connect handshake the integration
  re-subscribes *and* re-reads panel status plus all zone statuses, so state matches the
  panel within a second or two of any reconnect.
- **Token watchdog.** Refreshes the access token and proactively recycles the socket about
  75 seconds before expiry, so reconnects happen on our own schedule (~1–2s) rather than
  waiting for the ~30s heartbeat to notice a server-forced drop.

### Changed
- Poll interval reduced from 60s to 30s; the poll now reconciles over the websocket and
  keeps the last known state through transient errors instead of marking every entity
  unavailable.
- Reconnect backoff after a clean close reduced from 2s to 1s.

---

## [0.8.0] - 2026-07-22

### Added
- `watch` CLI command — subscribes and prints arming/zone changes live as pushes arrive,
  for verifying real-time updates without Home Assistant.
- **HACS packaging**: `hacs.json`, a GitHub Actions workflow running HACS validation and
  Home Assistant `hassfest` on every push, and a HACS-first README with a one-click
  *My Home Assistant* repository link.
- **Brand icons** at `custom_components/cove_alula/brand/` (`icon.png` 256×256 and
  `icon@2x.png` 512×512), using the Alula logo as transparent PNGs.
- `.github/FUNDING.yml` (Buy Me a Coffee) plus a README badge and support section.
- A step-by-step guide in the README for submitting to the HACS default store.

### Changed
- `manifest.json` `documentation`, `issue_tracker`, and `codeowners` point at
  `sam3gp8/cove-alula-homeassistant`; manifest key order matches hassfest's requirement
  (`domain`, `name`, then alphabetical).
- Minimum Home Assistant version raised to **2024.11.0**, which is when
  `AlarmControlPanelState` — used by the alarm entity — was introduced.

### Removed
- Personal account number and panel name scrubbed from all code, examples, and docs
  (neutral placeholders used instead). No password, PIN, or device ID was ever stored in
  the code; those are runtime arguments only.
- Unused `LEVEL_ANY` constant.

---

## [0.7.0] - 2026-07-22

### Fixed
- `ack` — the panel's success reply to commands and writes (arm, bypass, force-arm) — was
  not recognized as success, so those operations appeared unanswered even when they had
  worked.
- **Rapid-fire reads dropped responses.** Firing ~10 reads back-to-back made the cloud
  drop some, leaving `panel_name` and `panelStatus` null and creating phantom zones
  20–63. State refresh now issues the critical reads sequentially with waits and spacing,
  and loads zones only after the panel's real zone count (`highestUsedIndexes`) is known.

---

## Earlier

Versions before 0.7.0 were development iterations produced while reverse-engineering the
Cove Connect app and building the client and integration. They were never published as
releases and are not itemized here.

> **A note on dates:** 0.7.0 through 0.9.2 were developed and tested iteratively against
> real hardware before the repository was published, so they share a publication date.
> Every release from here on gets its own dated entry as it ships.

[Unreleased]: https://github.com/sam3gp8/cove-alula-homeassistant/compare/v0.11.1...HEAD
[0.11.1]: https://github.com/sam3gp8/cove-alula-homeassistant/compare/v0.11.0...v0.11.1
[0.11.0]: https://github.com/sam3gp8/cove-alula-homeassistant/compare/v0.10.1...v0.11.0
[0.10.1]: https://github.com/sam3gp8/cove-alula-homeassistant/compare/v0.10.0...v0.10.1
[0.10.0]: https://github.com/sam3gp8/cove-alula-homeassistant/compare/v0.9.2...v0.10.0
[0.9.2]: https://github.com/sam3gp8/cove-alula-homeassistant/compare/v0.9.1...v0.9.2
[0.9.1]: https://github.com/sam3gp8/cove-alula-homeassistant/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/sam3gp8/cove-alula-homeassistant/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/sam3gp8/cove-alula-homeassistant/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/sam3gp8/cove-alula-homeassistant/releases/tag/v0.7.0
