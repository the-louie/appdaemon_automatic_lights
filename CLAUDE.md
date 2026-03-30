# Project: Appdaemon Automatic Lights

This is a Python project for AppDaemon in Home Assistant.

## Overview

Single-file AppDaemon app (`i1_automatic_lights.py`) that automates lighting based on six time-of-day states: **night**, **morning**, **late_morning**, **day**, **evening**, **early_night**. Transitions are driven by scheduled times and sun position sensor events. An optional solar radiation sensor adds light-level awareness to transitions.

## Architecture

- **Single module**: `i1_automatic_lights.py` contains the `AutomaticLights` class (extends `hass.Hass`)
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

When adding new log lines, follow this convention and use the next available code in the appropriate range.

## HA entities used

- `sun.sun` (attribute: `elevation`) — sun elevation in degrees, read via `listen_state` with `attribute="elevation"`
- `sun.sun` (attribute: `rising`) — boolean, whether sun is rising
- `irisone.time_state` — custom entity set by the app to expose current state

## Configuration keys

Required: `morning_start`, `night_start`, `scenes`
Optional: `late_morning_start`, `early_night_start`, `solar_radiation` (with `sensor`, `threshold`, `elevation_threshold`), `staggering` (with `light_delay_min/max`, `room_delay_min/max`)

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
