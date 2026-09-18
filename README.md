# Cove (Alula) Alarm — Home Assistant integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![Validate](https://github.com/sam3gp8/cove-alula-homeassistant/actions/workflows/validate.yml/badge.svg)](https://github.com/sam3gp8/cove-alula-homeassistant/actions/workflows/validate.yml)
![version](https://img.shields.io/badge/version-0.11.1-blue.svg)
[![Buy Me A Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-support-FFDD00?logo=buymeacoffee&logoColor=black)](https://www.buymeacoffee.com/sam3gp8)

📋 **[Changelog](CHANGELOG.md)** — what changed in each release.

Control and monitor a **Cove** home-security system (built on the **Alula "Connect"**
cloud platform) from Home Assistant. Adds an `alarm_control_panel` entity that can arm
(home / away / night), disarm, and report live panel state.

This talks to the same `api.alula.net` cloud the official Cove Connect app uses, with
**your own** account credentials and PIN. It is cloud-based (`cloud_push` over a
WebSocket); there is no LAN/BLE local control yet.

> ⚠️ **This is a professionally-monitored alarm.** Arming, disarming, and alarm
> cancel/confirm generate **real central-station events**. Put your account on **test
> mode** with the monitoring provider while you set this up and validate it, and avoid
> rapid repeated login/PIN attempts (the service rate-limits with HTTP 429 and the panel
> has a keypad lockout).

---

## Install

### Option A — HACS (recommended)

This repo is a HACS **custom repository**. To add it:

1. In Home Assistant, open **HACS → ⋮ (top right) → Custom repositories**.
2. Repository: `https://github.com/sam3gp8/cove-alula-homeassistant` — Category: **Integration** — **Add**.
3. Search HACS for **Cove (Alula) Alarm**, open it, and click **Download**.
4. **Restart Home Assistant.**
5. Go to **Settings → Devices & Services → + Add Integration**, search for
   **“Cove (Alula) Alarm”**, and follow the prompts.

Or use the one-click link (opens the dialog pre-filled):

[![Open your Home Assistant instance and open a repository inside HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=sam3gp8&repository=cove-alula-homeassistant&category=integration)

### Option B — Manual

1. Copy the folder `custom_components/cove_alula` from this repo into your Home
   Assistant config directory, so you end up with:
   ```
   /config/custom_components/cove_alula/
   ```
2. Restart Home Assistant.
3. Add the integration from **Settings → Devices & Services** as above.

---

## Publishing this to GitHub (one-time)

The manifest, badges, and HACS links all point at
`https://github.com/sam3gp8/cove-alula-homeassistant`, so create the repo under that
exact name:

```bash
# from the extracted folder (contains custom_components/, hacs.json, README.md, .github/)
git init -b main
git add .
git commit -m "Cove (Alula) Alarm v0.11.1"
git remote add origin https://github.com/sam3gp8/cove-alula-homeassistant.git
git push -u origin main

# cut a release so HACS has a versioned download (tag must match manifest "version")
git tag v0.11.1
git push origin v0.11.1
```

Then on GitHub: **Releases → Draft a new release → choose tag `v0.11.1` → Publish**, and
paste that version's section from [CHANGELOG.md](CHANGELOG.md) into the release notes —
HACS shows those notes to users before they update.

HACS installs the latest release, so three things must stay in sync on every release: the
git tag (`v0.11.1`), the manifest `version` (`0.11.1`), and the `CHANGELOG.md` heading.

The included CI workflow (`.github/workflows/validate.yml`) runs **HACS validation** and
Home Assistant **hassfest** on every push, so you'll see a green check once it's pushed.
The HACS check runs with no ignores — the brand icons shipped in
`custom_components/cove_alula/brand/` satisfy the brands test.

> If you name the GitHub repo something other than `cove-alula-homeassistant`, update
> the URLs in `custom_components/cove_alula/manifest.json`, the badge/My-HA links in this
> README, and the `owner`/`repository` in the one-click link above.

---

## Getting into the HACS default store (optional)

Being in the default store means users can find this in HACS by searching, without
adding a custom repository. It's a one-time PR to [`hacs/default`](https://github.com/hacs/default)
and is reviewed by the HACS team (the backlog can take **months**). Everything below is
already set up in this repo except the GitHub-side bits only you can do.

**Eligibility:** integrations that override or alpha/beta-test a *core* integration are
not accepted as defaults. This is a standalone cloud integration, so it qualifies.

**Pre-submission checklist** (the PR runs automated checks; all must pass with **no
ignores**):

1. **Repo is public** on GitHub, **not archived**, with **Issues enabled**.
2. **Repo description** is set (a short sentence) and **topics** include
   `home-assistant`, `integration`, and `hacs`
   (GitHub → repo main page → ⚙ next to *About*).
3. **CI is green** — the included `.github/workflows/validate.yml` runs the
   [HACS action](https://github.com/hacs/action) and
   [hassfest](https://github.com/home-assistant/actions#hassfest). For the default-store
   PR the HACS action must pass **without `ignore:`** (this repo no longer uses one).
4. **Brand icon present.** This repo ships `custom_components/cove_alula/brand/icon.png`
   (256×256) and `icon@2x.png` (512×512). Since Home Assistant 2026.3 that local
   `brand/` folder is the supported way to provide an icon, and the HACS brand check
   accepts it. *The bundled icon is a placeholder — replace it with a real one before
   submitting (keep it square PNG, 256/512, and don't use Home Assistant's logo).* If
   your HACS-action brand check is on an older version that only reads
   [`home-assistant/brands`](https://github.com/home-assistant/brands), also drop the
   same `icon.png`/`icon@2x.png` into `custom_integrations/cove_alula/` there via a PR.
5. **At least one published GitHub release** (a full Release, not just a tag — see the
   section above).

**Submit the PR:**

1. Fork [`hacs/default`](https://github.com/hacs/default) **from your personal account**
   (not an org — the PR must be editable), and create a **new branch off `master`**.
2. Edit the [`integration`](https://github.com/hacs/default/blob/master/integration)
   file and add your repo **in alphabetical order** (not at the end), in
   `owner/repository` form:
   ```
   sam3gp8/cove-alula-homeassistant
   ```
3. Open the PR and **fill out the template completely** — incomplete or misrepresented
   PRs are closed without notice. If it's drafted for a minor issue, fix it and mark it
   ready for review.

After it merges, the repo is picked up on the next scheduled scan and becomes searchable
in HACS for everyone. Until then (and as a permanent alternative), the custom-repository
install above works for anyone you share the URL with.

> Note: the HACS default store is a separate thing from being a **core** Home Assistant
> integration (shipped with HA itself). That's a much larger review against the
> [integration quality scale](https://developers.home-assistant.io/docs/core/integration-quality-scale/)
> and isn't required for HACS.

---

## Configuration

The setup dialog asks for three things:

| Field | What to enter |
|---|---|
| **Email or account number** | Your Cove login — an email **or** the account number (e.g. `C123456`). |
| **Password** | Your Cove account password. |
| **Arm/disarm PIN** | The user PIN you use on the keypad. Stored in HA and applied automatically so automations can arm/disarm without re-entering it. |

The integration logs in once, then persists a refresh token in the config entry so it
keeps working across restarts without re-authenticating.

### What you get

* An **Alarm Control Panel** entity supporting **Arm Home**, **Arm Away**, **Arm Night**,
  and **Disarm**. The device is named after your system (e.g. "My Home").
* A **binary_sensor per zone** (door / window / motion / smoke / etc., auto-classified
  from the panel's zone type), named from the panel ("Kitchen Door") and showing
  open/closed live. Each carries `bypassed`, `alarm`, `tamper`, `low_battery`, and
  `signal_level` as attributes.
* **System status sensors**: Alarm, Ready to arm, Low battery, AC power, Tamper, and
  (disabled by default) central-station/server comms, siren, and fire troubles.
  *Ready to arm* is reported by the panel when available; if your panel only exposes it
  via a status code it doesn't support, it's derived automatically — ready when no zone is
  open-and-unbypassed. The sensor lists the blocking zones in its `open_zones` attribute.
* **Services**:
  * `cove_alula.cancel_alarm` — send an alarm cancel to monitoring.
  * `cove_alula.confirm_alarm` — send an alarm confirm to monitoring.
  * `cove_alula.bypass_zone` — bypass/unbypass a zone by index
    (`{ "zone": 3, "bypass": true }`).
  * `cove_alula.force_arm` — arm when zones are open. `{ "mode": "away" }` bypasses any
    open zones then arms with your PIN (default, most reliable); `{ "mode": "away",
    "method": "native" }` uses the panel's built-in force-arm command instead. Bypasses
    clear on the next disarm.

State derives from live panel pushes: `disarmed`, `armed_home`, `armed_away`,
`armed_night`, `arming` (exit delay), `pending` (entry delay), `triggered` (in alarm).

---

## Verify it before relying on it

Bundled with the component is a standalone CLI (`custom_components/cove_alula/covealula.py`)
you can run on any machine with `python3` + `aiohttp` — no Home Assistant required. Use it
to confirm login and the correct arming-level numbers for **your** panel:

```bash
pip install aiohttp
python custom_components/cove_alula/covealula.py status   <user> '<password>'
python custom_components/cove_alula/covealula.py zones    <user> '<password>'
python custom_components/cove_alula/covealula.py names    <user> '<password>'
python custom_components/cove_alula/covealula.py disarm   <user> '<password>' <pin>
python custom_components/cove_alula/covealula.py arm_away <user> '<password>' <pin>
```

* `status` subscribes, pulls a full snapshot (name, firmware, zones), and prints every
  raw frame plus the parsed state and zone list.
* `zones` reads zone names/config/status and prints the parsed sensor list — use it to
  confirm the zone fields parse correctly on your panel.
* `names` prints your panel's arming-level labels so you can confirm which number means
  Stay vs Away vs Night.

### Arming-level mapping (important)

`armingLevelValue` is sent to the panel as: **1 = disarm**, 2 = first armed level
(usually *stay/home*), 3 = second armed level (usually *away*), 4+ = further/custom levels
(e.g. *night*). Level **0 is “unknown” and is not a valid command.** The meaning of each
*armed* level is configured **per panel** — only disarm (1) is universal. If `Arm Away`
or `Arm Home` lands on the wrong mode, run `names`, then adjust `LEVEL_STAY` / `LEVEL_AWAY`
/ `LEVEL_NIGHT` near the top of `covealula.py`.

---

## Troubleshooting

* **Entity shows `unknown` / no state.** The panel only streams state after the
  integration subscribes and pulls a snapshot — both happen automatically on setup. If it
  stays unknown, run the `status` CLI and look at the raw `<<` frames; that's the actual
  wire data.
* **`invalid_auth` at setup.** Wrong username/password. Remember the username may be your
  account number, not an email.
* **Login starts failing later with `invalid_client`.** The mobile client credentials
  embedded in the Cove app were rotated in a newer app version. They live in `covealula.py`
  (`CLIENT_ID` / `CLIENT_SECRET`) and can be re-extracted from a current app build.
* **Rate limited (HTTP 429).** Back off; don't loop auth/PIN attempts.

**Download diagnostics** for a bug report: **Settings → Devices & Services → Cove (Alula)
Alarm → ⋮ → Download diagnostics** (or the same menu on a device page). The file is safe
to share — credentials, the panel device id, and all panel/zone names are redacted, while
connection state, per-read results, arming level, troubles, and per-zone state (by index)
are kept.

Enable debug logging to see frames in HA logs:

```yaml
logger:
  default: info
  logs:
    custom_components.cove_alula: debug
```

---

## How it works (short version)

* OAuth2 password grant against `https://api.alula.net/oauth/token` using the app's
  embedded mobile client id/secret + your username/password.
* Device discovery via the JSON:API REST endpoints (`/rest/v1/devices`).
* Live state + commands over `wss://api.alula.net/ws/v1`: subscribe to `device.status` +
  `device.helix`, pull a snapshot via `requestMfd` reads, and arm/disarm with
  `changeArmingLevelUsingCode` (PIN sent as a per-digit array).

Full protocol notes are in `cove_alula_protocol.md` (alongside this package).

---

## Support

This is a free, unofficial integration built in spare time. If it saved you some
work, you can say thanks with a coffee — entirely optional and always appreciated:

[![Buy Me A Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-support-FFDD00?logo=buymeacoffee&logoColor=black)](https://www.buymeacoffee.com/sam3gp8)

> [buymeacoffee.com/sam3gp8](https://www.buymeacoffee.com/sam3gp8)

---

## Disclaimer

Independent interoperability project. Not affiliated with, endorsed by, or supported by
Cove or Alula. Use at your own risk on your own account/equipment. A security system is
safety-critical — keep the official app installed and do not rely solely on this.
