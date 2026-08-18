# Changelog

All notable changes to this integration ([mhoogenbosch/ha-pipup](https://github.com/mhoogenbosch/ha-pipup))
are documented here. The format is loosely based on [Keep a Changelog](https://keepachangelog.com/).

Every version below has a [GitHub release](https://github.com/mhoogenbosch/ha-pipup/releases) with the
full story in English and Dutch. Features marked *(app ≥ x.y.z)* need a matching version of the
[PiPup app](https://github.com/mhoogenbosch/PiPup) on the TV.

## [v1.10.1] — 2026-08-17 (schedule the reload in the update listener)
### Fixed
- Home Assistant logged *"Detected that custom integration 'pipup' has an update listener and should use
  it for scheduling a reload. This will stop working in Home Assistant 2026.12.0"*. Awaiting
  `async_reload` from inside the entry's own update listener races with setup retry; it now calls
  `async_schedule_reload`, which cancels the retry first and runs the reload in a task. Only reached when
  you change the polling interval in the options, so nothing else behaves differently.

## [v1.10.0] — 2026-08-17 (fix a TV's permissions from Home Assistant)
Companion to [app v0.8.0](https://github.com/mhoogenbosch/PiPup/releases/tag/v0.8.0) — update the app on
your TVs too.
### Added
- **`pipup.fix_permission`** action — opens the app's own permission overview on the TV, the first missing
  permission, or a specific one. Errors are collected per device instead of aborting the whole call, so
  targeting several TVs still helps the ones that can.
- **Permission screen** button per TV, created only where the app reports the capability *(app ≥ 0.8.0)*.
- **`fixable_on_tv`** attribute on the permission sensor, listing what can be granted on screen on that
  particular device — so a dashboard only offers a button where it leads somewhere.
### Changed
- The overlay repair now fixes itself where the device allows it: press *Submit* and the system permission
  screen appears on the TV. Where it does not (Fire OS answers those intents with placeholders that do
  nothing) the flow aborts with the adb command instead of pretending.
- The coordinator asks the issue registry whether the repair issue still exists rather than comparing
  against the last value it saw. The repair flow closes its issue while the permission is still missing —
  without this, dismissing the repair would have buried the problem for good.

## [v1.9.1] — 2026-08-17 (verify a discovered address before moving an entry)
Recommended for everyone with more than one PiPup TV.
### Fixed
- **An entry could end up pointing at the wrong TV.** The app registers its service through `NsdManager`,
  which publishes under the *device hostname*, and Android TVs collide there — one stick claims
  `Android.local`, others `Android-4.local` and `Android-6.local`. Resolving a TV's service returned
  whichever device owned that generic hostname, and the entry was faithfully moved onto it.
  Now the device at a discovered address is asked whether it really reports that id before an existing
  entry is moved; a mismatch or unreachable host aborts with `address_mismatch`. The zeroconf confirm step
  checks the same before creating an entry. A genuine DHCP change still moves the entry, because the
  device at the new address confirms its own id.
- `model` and `manufacturer` are synced into the device registry alongside `sw_version`, so a device page
  that picked up the wrong hardware heals on the next poll.
### Notes
- Wondering whether an entry is wrong: compare the TV's `id` in `http://<tv-ip>:7979/state` with the entry.
  Repair by adding the integration again with the correct IP — the flow recognises the device id and moves
  the existing entry, keeping its entities, history and automations.

## [v1.9.0] — 2026-08-17 (screen switch, border styling, permission repair)
Companion to [app v0.7.0](https://github.com/mhoogenbosch/PiPup/releases/tag/v0.7.0).
### Added
- **Screen** switch per TV *(app ≥ 0.7.0)*: on = wake, off = standby, through the app's `POST /power`.
  Turning off needs a one-time adb grant on the TV; the capability shows up as the `can_sleep` /
  `sleep_method` attributes and `turn_off` raises an error naming the fix, rather than the entity silently
  not existing. The switch trusts its own last command for 20 seconds and schedules an extra refresh,
  because the app derives the screen state from `PowerManager.isInteractive`, which lags a poll behind a
  fresh wake.
- **`border_color`, `border_width`, `corner_radius`** on `pipup.show`, each overriding *its part* of the
  `urgency` preset. Also available as per-device defaults in the options flow. For the two numeric ones,
  **empty** means "no default" while **0** is a real value (no border / square corners).
- **Permission problem** binary sensor per TV, with each grant as an attribute, plus a **repair issue**
  when the overlay app-op is missing that carries the exact adb command and clears itself once the device
  reports the grant.

## [v1.8.2] — 2026-07-28 (the update entity polls again)
### Fixed
- The update entity actually polls again.

## [v1.8.1] — 2026-07-27 (version display is correct again)
### Fixed
- Device firmware stays in sync and a stale latest-release cache is refetched.

## [v1.8.0] — 2026-07-27 (Install button: the TV updates itself)
### Added
- An Install button on the update entity, so the TV pulls a new app version itself.

## [v1.7.1] — 2026-07-27 (last_popup attributes)
### Added
- `last_popup` attributes on the popup sensor.

## [v1.7.0] — 2026-07-19 (token-protected button callbacks)
### Added
- Token-protected callbacks for popup buttons, plus review fixes.

## [v1.6.1] — 2026-07-12 · [v1.6.0] — 2026-07-12
### Added
- Popup buttons, countdown bar and urgency presets; hassfest fix and a README refresh.

## [v1.5.1] · [v1.5.0] — 2026-07-11
### Added
- Discovery, connectivity sensor, notify platform and TTS; notify title support.

## [v1.0.0] – [v1.4.1] — 2026-07-03 … 2026-07-11
### Added
- First releases: sensors, button, select and update entity, name suffix, own brand images, `muted`
  (on by default), per-TV defaults for every field, and MJPEG camera mode as the safe default.

[v1.10.1]: https://github.com/mhoogenbosch/ha-pipup/releases/tag/v1.10.1
[v1.10.0]: https://github.com/mhoogenbosch/ha-pipup/releases/tag/v1.10.0
[v1.9.1]: https://github.com/mhoogenbosch/ha-pipup/releases/tag/v1.9.1
[v1.9.0]: https://github.com/mhoogenbosch/ha-pipup/releases/tag/v1.9.0
[v1.8.2]: https://github.com/mhoogenbosch/ha-pipup/releases/tag/v1.8.2
[v1.8.1]: https://github.com/mhoogenbosch/ha-pipup/releases/tag/v1.8.1
[v1.8.0]: https://github.com/mhoogenbosch/ha-pipup/releases/tag/v1.8.0
[v1.7.1]: https://github.com/mhoogenbosch/ha-pipup/releases/tag/v1.7.1
[v1.7.0]: https://github.com/mhoogenbosch/ha-pipup/releases/tag/v1.7.0
[v1.6.1]: https://github.com/mhoogenbosch/ha-pipup/releases/tag/v1.6.1
[v1.6.0]: https://github.com/mhoogenbosch/ha-pipup/releases/tag/v1.6.0
[v1.5.1]: https://github.com/mhoogenbosch/ha-pipup/releases/tag/v1.5.1
[v1.5.0]: https://github.com/mhoogenbosch/ha-pipup/releases/tag/v1.5.0
[v1.4.1]: https://github.com/mhoogenbosch/ha-pipup/releases/tag/v1.4.1
[v1.0.0]: https://github.com/mhoogenbosch/ha-pipup/releases/tag/v1.0.0
