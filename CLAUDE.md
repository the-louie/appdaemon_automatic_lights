# Project: Appdaemon Automatic Lights

This is a Python project for AppDaemon in Home Assistant.

## Overview

Single-file AppDaemon app (`i1_automatic_lights.py`) that automates lighting based on six time-of-day states: **night**, **morning**, **late_morning**, **day**, **evening**, **early_night**. Transitions are driven by scheduled times and sun position sensor events. An optional solar radiation sensor adds light-level awareness to transitions.

## Architecture

- **Single module**: `i1_automatic_lights.py` contains the `AutomaticLights` class (extends `hass.Hass`)
- **`notification_policy.py`** — a byte-identical copy of the canonical module in
  `appdaemon_watchdog`. Do not edit it here; re-copy. `test_policy_copies_match.py` turns
  drift into a test failure.
- **Configuration**: `config.yaml` (live, gitignored) / `config.yaml.example` (committed reference)
- **No tests or separate packages** — flat structure, deployed directly to AppDaemon's `apps` directory

### Key data structures

- `SolarConfig` / `StaggerConfig` / `EntityControl` — dataclasses for configuration and scene entity state
- `self.groups` — all HA groups, keyed by `group.<name>`
- `self.group_area_entities` — `{group_id: {area: [entity_ids]}}` — only groups referenced by scenes
- `self.scenes` — raw config: `{scene_name: {group_name: bool}}`
- `self._pending_timers` — tracked `run_in` handles, cancelled on scene change to prevent interleaving

### State machine

States cycle: `night` -> `morning` -> `late_morning` -> `day` -> `evening` -> `early_night` -> `night`

**Scheduled transitions:**
- **night -> morning**: scheduled at `morning_start` (with random offset -45 to -30 min)
- **morning -> late_morning**: scheduled at `late_morning_start` (optional, no randomization)
- **evening -> early_night**: scheduled at `early_night_start` (optional, with random offset -15 to -10 min)
- **early_night/evening -> night**: scheduled at `night_start` (with random offset -15 to -10 min)

**Sun-driven transitions:**
- **morning/late_morning -> day**: sun elevation rises above threshold (+ solar radiation if enabled)
- **day -> evening**: sun elevation drops below threshold (or solar radiation drops)

**Guards and protections:**
- Evening transition is blocked if already in early_night or night
- Same-scene re-entry is blocked (unless `immediate=True` for manual triggers)
- `late_morning` callback only fires from morning or night states
- `early_night` callback only fires from evening state
- Morning callback skips if already in day state
- Pending stagger timers are cancelled before any new scene activation

### Staggered control

Scene activation staggers entity changes across areas with random delays to simulate natural behaviour. Lights within an area get cumulative small delays (`light_delay`), areas get larger delays (`room_delay`). Timer handles are tracked in `_pending_timers` and cancelled on scene change to prevent old callbacks from interleaving with new ones.

### Midnight-wrapping time logic

`_calculate_state` handles `night_start` past midnight (e.g., "00:00" or "01:00"). Times between midnight and a post-midnight `night_start` are classified as evening/early_night, not night. The `early_night_start` comparison accounts for the case where `early_night_start` is before midnight but `now` is after midnight.

## Log codes

All log messages use bracketed codes for traceability:
- `A0xx` — Initialization and config loading (A001-A012)
- `B0xx` — Group and area setup
- `D0xx` — Manual scene handling (D001-D006)
- `E0xx` — State transitions / scene start (E001-E003)
- `F0xx` — Scene activation
- `G0xx` — Staggered control scheduling
- `H0xx` — Entity on/off control
- `S0xx` — Sensor reads and transition checks
- `U0xx` — Reachability audit: entities that cannot respond to a command (U001-U013)
- `V0xx` — Verification: whether a scene's commands actually took effect (V001-V008)
- `N0xx` — Notification of divergences (N001-N006)

When adding new log lines, follow this convention and use the next available code in the appropriate range.

### The `U0xx` range, and why it exists

Added 2026-09-03 (S4-04). `group.bedroom_lightning` had one member, that member was
`unavailable`, and the `late_morning`, `early_night` and `evening` scenes had all been
commanding it for an unknown length of time. The app logged `[H001] Turned ON` every time.
Nothing anywhere said otherwise, and the group's own state read `unknown` rather than `off`.

| code | level | meaning |
|---|---|---|
| `U001` | INFO | audit starting, with its context (`startup`, or a scene name) |
| `U002` | WARNING | one member of a group is unreachable; names the scenes that want it |
| `U003` | **ERROR** | *every* member of a group is unreachable — the scene controls nothing |
| `U004` | WARNING | a scene names a group that has no members at all |
| `U005` | INFO | audit clean |
| `U006` | **ERROR** | a scene activation where every entity is unreachable |
| `U007` | WARNING | a scene activation where some entities are unreachable |
| `U008` | WARNING | a command was issued to an entity that is unreachable |
| `U009` | INFO | unreachable, but on the expected-absent list and still in date |
| `U010` | WARNING | an expected-absent entry is **past its review date** |
| `U011` | INFO | a listed entity is reachable again — remove the entry |
| `U012` | **ERROR** | a malformed `expected_absent` entry, rejected |
| `U013` | INFO | summary of the expected-absent entries loaded |

**`U003`/`U006` are ERROR on purpose.** A group with one dead bulb is a maintenance note; a
group where nothing responds means a scene silently does nothing, which is the failure that
went unnoticed. They are different events and should not share a severity.

**The `expected_absent` list (S4-05).** `switch.v2_kok_girlang` is a Christmas ornament,
unplugged eleven months a year. An audit that warns about it from December to November is
worse than no audit, because people stop reading it — including in the month it is right.
Entries require both a `reason` and a `review` date; a malformed entry is rejected and does
**not** suppress, which is the safe direction. Past its review date an entry stops
suppressing and starts warning about itself, so the list cannot silently become permanent.

### The `V0xx` range — did it actually happen

Added 2026-09-03 (S4-06). `U0xx` answers *could this command land*. `V0xx` answers the
different and harder question: **did it**. After each scene activation the app re-reads
every entity it commanded and compares against what it asked for.

| code | level | meaning |
|---|---|---|
| `V001` | INFO | scene verified, everything in the commanded state |
| `V002` | WARNING | scene diverged — reachable entities not in the commanded state |
| `V003` | INFO | verification scheduled, with its delay |
| `V004` | WARNING | a day with **no scene transitions at all** |
| `V005` | INFO | daily report, all clean |
| `V006` | WARNING | daily report, with the worst offenders |
| `V007` | INFO | daily report scheduled |
| `V008` | **ERROR** | the daily report could not be scheduled |

**Unreachable entities are skipped here on purpose.** `U0xx` owns them; repeating them
would bury the finding this exists for — the entity that *is* reachable, *did* accept the
command, and is still in the wrong state. That is the case nothing has ever caught, and it
is exactly what a second app writing over the same light looks like (T-40).

**Verification waits for the stagger.** `_execute_staggered_control` now returns the largest
delay it scheduled, and verification runs `verify_margin_seconds` after that. Checking
sooner would report every scene as diverged, which is the fastest way to make a signal
worthless.

**`V004` — a silent day is a warning, not a pass.** No transitions recorded means the
scheduler never fired, which is a bigger fault than a single light being off, and it is the
failure mode that would otherwise look identical to success.

### The `N0xx` range — telling someone

Added 2026-09-03 (S4-07). A divergence in the log is a divergence nobody sees.

| code | level | meaning |
|---|---|---|
| `N001` | WARNING/ERROR | a bad `notify_targets` / channel / quiet-hour value |
| `N002` | WARNING | **no targets configured — nobody will be told** |
| `N003` | INFO | notification sent, and to whom |
| `N004` | **ERROR** | one target failed; the others were still tried |
| `N005` | INFO | divergences held by the repeat/quiet-hours policy |
| `N006` | INFO | a light recovered, so its next failure counts as news again |

Uses the shared `notification_policy.py`, byte-identical to the canonical copy in
`appdaemon_watchdog` (`test_policy_copies_match.py` fails on drift). Quiet hours 22–07,
re-alert after 6h, and the **first occurrence always sends** even inside quiet hours.

**Keyed per entity, not per scene.** One stuck light must not produce a message at every
transition all day, and a second light failing must still be news while the first is held.

**`notify_targets` is optional here, unlike the watchdog.** The watchdog raises without it,
because a watchdog that cannot speak is pointless. This app controlled lights correctly long
before it could notify anything, so making the key mandatory would stop every light in the
house on the next pull. It warns once at startup instead — silence about being unable to
speak is the failure this range exists to remove.

**`N006` matters more than it looks.** When a light recovers, its entry must be cleared from
the re-alert state, or its *next* failure is treated as a repeat and held. That bug was
written and caught by its own test.

**`U008` replaces a success line, it does not add to one.** `_turn_onoff` still issues the
command to an unreachable entity — a device that comes back should find the right state
waiting for it — but it no longer logs `H001`/`H002` when the command cannot have landed.

## HA entities used

- `sun.sun` (attribute: `elevation`) — sun elevation in degrees, read via `listen_state` with `attribute="elevation"`
- `sun.sun` (attribute: `rising`) — boolean, whether sun is rising
- `irisone.time_state` — custom entity set by the app to expose current state

## Configuration keys

Required: `morning_start`, `night_start`, `scenes`
Optional: `late_morning_start`, `early_night_start`, `solar_radiation` (with `sensor`, `threshold`, `elevation_threshold`), `staggering` (with `light_delay_min/max`, `room_delay_min/max`), `expected_absent` (entity_id -> `reason` + `review`), `audit` (with `verify_margin_seconds`, `history`), `audit_daily_report_time`, `notify_targets`, `notification_channel`, `notification_priority`, `quiet_start`, `quiet_end`, `repeat_after`

All time config values are validated at load time via `_parse_time_config` with fallback to defaults.

### Scene configuration (cumulative/delta model)

Scenes are **cumulative deltas**: each scene only defines the groups it changes. Groups not listed retain their state from earlier transitions. This allows groups like `specific_lightning` to be set once (in `night`) and left untouched until the next `night`, so manually activated lights persist across scene changes.

On init/restart, the app replays all predecessor scenes in state-machine order up to the current state to reconstruct the correct cumulative lighting state.

## Guidelines

- Review the [AppDaemon documentation](https://appdaemon.readthedocs.io/en/latest/)
- Robust error handling and logging, including context capture
- Code style consistency using Ruff
- Avoid duplicate code
- Pay extra attention to logical errors
- Configuration reference is in `config.yaml.example`

## Coding Practices

- Descriptive variable and function names
- Type hints on all function signatures
- Detailed comments for complex logic, especially midnight-wrapping time comparisons
- Rich error context for debugging
- Use `str.format()` for log messages (not f-strings) — consistent with existing codebase
- Constants at module level with descriptive names
- `HA_UNAVAILABLE_STATES` frozenset for sensor validation
- Exception handling: re-raise programming errors (`TypeError`, `AttributeError`, `NameError`), only catch runtime/API failures
- Precompute sets for O(1) membership tests in loops (see `group_sets` in `_build_area_mappings`)
