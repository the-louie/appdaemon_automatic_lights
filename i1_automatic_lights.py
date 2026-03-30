"""
Automatic Lights - Home Assistant AppDaemon App
Copyright (c) 2025 the_louie
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass

import appdaemon.plugins.hass.hassapi as hass

# Configuration defaults
DEFAULT_ELEVATION_THRESHOLD = 3.0
DEFAULT_MORNING_START = "05:30"
DEFAULT_LATE_MORNING_START = None
DEFAULT_EARLY_NIGHT_START = None
DEFAULT_NIGHT_START = "23:30"
DEFAULT_LIGHT_DELAY_MIN = 2
DEFAULT_LIGHT_DELAY_MAX = 5
DEFAULT_ROOM_DELAY_MIN = 30
DEFAULT_ROOM_DELAY_MAX = 120

# Entity constants
SUN_ENTITY = "sun.sun"
TIME_STATE_ENTITY = "irisone.time_state"

# State machine order for cumulative scene merging on init
STATE_ORDER = ("night", "morning", "late_morning", "day", "evening", "early_night")

# Throttle / logging
SUN_HANDLER_THROTTLE_SECONDS = 60
NO_TRANSITION_LOG_INTERVAL = 15  # Log every Nth no-transition check (~15 min)

# HA states that indicate a sensor is not reporting valid data
HA_UNAVAILABLE_STATES = frozenset({"unavailable", "unknown", ""})


@dataclass
class SolarConfig:
    """Solar radiation sensor configuration."""

    sensor: str | None = None
    threshold: float | None = None
    elevation_threshold: float = DEFAULT_ELEVATION_THRESHOLD

    @property
    def is_enabled(self) -> bool:
        return self.sensor is not None and self.threshold is not None


@dataclass
class StaggerConfig:
    """Staggered light control timing configuration."""

    light_delay_min: float = DEFAULT_LIGHT_DELAY_MIN
    light_delay_max: float = DEFAULT_LIGHT_DELAY_MAX
    room_delay_min: float = DEFAULT_ROOM_DELAY_MIN
    room_delay_max: float = DEFAULT_ROOM_DELAY_MAX


@dataclass
class EntityControl:
    """A single entity to be controlled during a scene activation."""

    entity_id: str
    target_state: bool
    area: str
    group: str


class AutomaticLights(hass.Hass):
    """Automatic lighting control based on time and sun position."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.current_state: str = "night"
        self.groups: dict[str, list[str]] = {}
        self._last_throttle_time: float = 0.0
        self._no_transition_log_counter: int = 0
        self.area_list: list[str] = []
        self.area_entity_map: dict[str, list[str]] = {}
        self.entity_to_area: dict[str, str] = {}
        self.group_area_entities: dict[str, dict[str, list[str]]] = {}
        self.solar: SolarConfig = SolarConfig()
        self.stagger: StaggerConfig = StaggerConfig()
        self.morning_start: str = DEFAULT_MORNING_START
        self.late_morning_start: str | None = DEFAULT_LATE_MORNING_START
        self.early_night_start: str | None = DEFAULT_EARLY_NIGHT_START
        self.night_start: str = DEFAULT_NIGHT_START
        self.scenes: dict = {}
        self._pending_timers: list = []

    def initialize(self):
        """Initialize the app."""
        self.log("[A001] Starting initialization")

        self._load_config()
        self._setup_groups_and_areas()
        self.current_state = self._calculate_state()

        self._register_listeners()
        self._schedule_daily_events()

        self._activate_cumulative_state(self.current_state)

        self.log("[A003] Initialization complete: state={}".format(self.current_state))

    # ── Configuration ──────────────────────────────────────────────

    def _parse_time_config(self, key: str, default: str | None) -> str | None:
        """Read and validate a time configuration value."""
        raw = self.args.get(key, default)
        if raw is None:
            return None
        try:
            self.parse_time(raw)
            return raw
        except (ValueError, TypeError):
            self.log(
                "[A011] WARNING: Invalid time '{}' for '{}', "
                "using default '{}'".format(raw, key, default)
            )
            return default

    def _load_config(self):
        """Load and validate all configuration from apps.yaml."""
        self.morning_start = self._parse_time_config(
            "morning_start", DEFAULT_MORNING_START
        )
        self.late_morning_start = self._parse_time_config(
            "late_morning_start", DEFAULT_LATE_MORNING_START
        )
        self.early_night_start = self._parse_time_config(
            "early_night_start", DEFAULT_EARLY_NIGHT_START
        )
        self.night_start = self._parse_time_config(
            "night_start", DEFAULT_NIGHT_START
        )
        self.scenes = self.args.get("scenes", {})

        # Solar radiation
        solar_raw = self.args.get("solar_radiation", {})
        sensor = solar_raw.get("sensor")
        threshold = solar_raw.get("threshold")
        elevation_threshold = solar_raw.get(
            "elevation_threshold", DEFAULT_ELEVATION_THRESHOLD
        )

        if threshold is not None:
            try:
                threshold = float(threshold)
            except (ValueError, TypeError):
                self.log(
                    "[A004] WARNING: Invalid solar threshold '{}', "
                    "disabling solar radiation".format(threshold)
                )
                sensor = None
                threshold = None

        try:
            elev_thresh = float(elevation_threshold)
        except (ValueError, TypeError):
            self.log(
                "[A012] WARNING: Invalid elevation_threshold '{}', "
                "using default {}".format(
                    elevation_threshold, DEFAULT_ELEVATION_THRESHOLD
                )
            )
            elev_thresh = DEFAULT_ELEVATION_THRESHOLD

        self.solar = SolarConfig(
            sensor=sensor,
            threshold=threshold,
            elevation_threshold=elev_thresh,
        )

        # Staggering
        stagger_raw = self.args.get("staggering", {})
        self.stagger = StaggerConfig(
            light_delay_min=stagger_raw.get("light_delay_min", DEFAULT_LIGHT_DELAY_MIN),
            light_delay_max=stagger_raw.get("light_delay_max", DEFAULT_LIGHT_DELAY_MAX),
            room_delay_min=stagger_raw.get("room_delay_min", DEFAULT_ROOM_DELAY_MIN),
            room_delay_max=stagger_raw.get("room_delay_max", DEFAULT_ROOM_DELAY_MAX),
        )

        self.log(
            "[A002] Configuration loaded: morning={}, late_morning={}, "
            "early_night={}, night={}, solar={}, "
            "stagger=light {}-{}s, area {}-{}s".format(
                self.morning_start,
                self.late_morning_start or "disabled",
                self.early_night_start or "disabled",
                self.night_start,
                "enabled" if self.solar.is_enabled else "disabled",
                self.stagger.light_delay_min,
                self.stagger.light_delay_max,
                self.stagger.room_delay_min,
                self.stagger.room_delay_max,
            )
        )

    def _register_listeners(self):
        """Register state and event listeners."""
        # Validate sun entity is available before registering
        elevation = self.get_state(SUN_ENTITY, attribute="elevation")
        rising = self.get_state(SUN_ENTITY, attribute="rising")
        if elevation is not None and str(elevation) not in HA_UNAVAILABLE_STATES:
            self.log(
                "[A007] Sun entity '{}' elevation={}, rising={}".format(
                    SUN_ENTITY, elevation, rising
                )
            )
        else:
            self.log(
                "[A008] WARNING: Sun entity '{}' unavailable (elevation='{}') "
                "— day/evening auto-transitions will not work".format(
                    SUN_ENTITY, elevation
                ),
                level="WARNING",
            )

        self.listen_state(
            self._handle_sun_pos, SUN_ENTITY, attribute="elevation"
        )
        self.listen_event(
            self._handle_manual_scene, event="call_service", domain="scene"
        )

    def _schedule_daily_events(self):
        """Schedule daily time-based transitions."""
        self.run_daily(
            self._on_morning_schedule,
            self.morning_start,
            random_start=-45 * 60,
            random_end=-30 * 60,
        )
        self.log(
            "[A005] Scheduled morning at {} (random window -45 to -30 min)".format(
                self.morning_start
            )
        )

        if self.late_morning_start:
            self.run_daily(
                self._on_late_morning_schedule,
                self.late_morning_start,
            )
            self.log(
                "[A009] Scheduled late_morning at {}".format(
                    self.late_morning_start
                )
            )

        if self.early_night_start:
            self.run_daily(
                self._on_early_night_schedule,
                self.early_night_start,
                random_start=-15 * 60,
                random_end=-10 * 60,
            )
            self.log(
                "[A010] Scheduled early_night at {} (random window -15 to -10 min)".format(
                    self.early_night_start
                )
            )

        self.run_daily(
            self._on_night_schedule,
            self.night_start,
            random_start=-15 * 60,
            random_end=-10 * 60,
        )
        self.log(
            "[A006] Scheduled night at {} (random window -15 to -10 min)".format(
                self.night_start
            )
        )

    # ── Group and area setup ───────────────────────────────────────

    def _setup_groups_and_areas(self):
        """Setup groups and area mapping."""
        self.log("[B001] Starting groups and areas setup")

        self._load_groups()
        configured_entities = self._collect_configured_entities()

        self.log(
            "[B004] Found {} entities in configured groups".format(
                len(configured_entities)
            )
        )

        self._build_area_mappings(configured_entities)
        self._log_group_area_entity_mapping()

    def _load_groups(self):
        """Load all HA groups into self.groups."""
        state_groups = self.get_state("group")
        if not isinstance(state_groups, dict):
            self.log("[B003] WARNING: No groups found or invalid format")
            return

        for group_id, group_data in state_groups.items():
            entities = group_data.get("attributes", {}).get("entity_id", [])
            if isinstance(entities, list):
                self.groups[group_id] = entities
                self.log(
                    "[B002] Group {}: {} entities".format(group_id, len(entities))
                )

    def _collect_configured_entities(self) -> set[str]:
        """Collect all entity IDs referenced by configured scenes."""
        configured: set[str] = set()
        for scene_config in self.scenes.values():
            for group_name in scene_config:
                group_entity_id = f"group.{group_name}"
                if group_entity_id in self.groups:
                    configured.update(self.groups[group_entity_id])
        return configured

    def _build_area_mappings(self, configured_entities: set[str]):
        """Build area-to-entity and group-area-entity lookup tables."""
        self.log("[B005] Fetching areas from Home Assistant")
        self.area_list = self.areas()
        self.log("[B006] Found {} areas: {}".format(len(self.area_list), self.area_list))

        # Initialise group-area-entities lookup for configured groups
        for scene_config in self.scenes.values():
            for group_name in scene_config:
                group_entity_id = f"group.{group_name}"
                if group_entity_id in self.groups:
                    self.group_area_entities.setdefault(group_entity_id, {})

        # Precompute group entity sets for O(1) membership tests
        group_sets: dict[str, set[str]] = {
            gid: set(entities)
            for gid, entities in self.groups.items()
            if gid in self.group_area_entities
        }

        for area in self.area_list:
            all_area_entities = self.area_entities(area)
            if not all_area_entities:
                self.log("[B008] Area '{}': No entities found".format(area))
                continue

            filtered = [e for e in all_area_entities if e in configured_entities]
            self.area_entity_map[area] = filtered

            for entity in filtered:
                self.entity_to_area[entity] = area

            for group_id, group_set in group_sets.items():
                in_area = [e for e in filtered if e in group_set]
                if in_area:
                    self.group_area_entities[group_id][area] = in_area

            self.log(
                "[B007] Area '{}': {} configured of {} total entities".format(
                    area, len(filtered), len(all_area_entities)
                )
            )

        for group_id, area_entities in self.group_area_entities.items():
            total = sum(len(e) for e in area_entities.values())
            self.log(
                "[B010] Group {}: {} entities across {} areas".format(
                    group_id, total, len(area_entities)
                )
            )

        self.log(
            "[B009] Areas setup complete: {} areas with entities, {} entities cached".format(
                len(self.area_entity_map), len(self.entity_to_area)
            )
        )

    def _log_group_area_entity_mapping(self):
        """Log all entities in configured groups, grouped by area."""
        self.log("[B011] === GROUP-AREA-ENTITY MAPPING ===")

        configured_groups: set[str] = set()
        for scene_config in self.scenes.values():
            for group_name in scene_config:
                configured_groups.add(f"group.{group_name}")

        for group_entity_id in sorted(configured_groups):
            group_name = group_entity_id.removeprefix("group.")

            if group_entity_id not in self.group_area_entities:
                self.log(
                    "[B016] {}: Group not found or has no entities".format(group_name)
                )
                continue

            self.log("[B012] {}:".format(group_name))
            area_entities = self.group_area_entities[group_entity_id]

            if not area_entities:
                self.log("[B015]   No entities found in any area")
                continue

            for area_id in sorted(area_entities):
                area_name = self.area_name(area_id)
                self.log("[B013]   {}:".format(area_name))
                for entity in sorted(area_entities[area_id]):
                    self.log("[B014]     - {}".format(entity))

        self.log("[B017] === END GROUP-AREA-ENTITY MAPPING ===")

    # ── Event handlers ─────────────────────────────────────────────

    def _handle_manual_scene(self, event_name, data, **kwargs):
        """Handle manual scene activation via HA service call."""
        self.log("[D001] Manual scene activation triggered")

        if not isinstance(data, dict):
            self.log("[D006] Invalid event data: {}".format(type(data).__name__))
            return

        service_data = data.get("service_data", {})
        scene_entity = service_data.get("entity_id")

        if not scene_entity:
            self.log("[D002] No scene entity found in service data")
            return

        scene_entities = (
            scene_entity if isinstance(scene_entity, list) else [scene_entity]
        )
        self.log("[D003] Processing {} scene entities".format(len(scene_entities)))

        for entity in scene_entities:
            if not entity.startswith("scene."):
                continue
            scene_name = entity.removeprefix("scene.")
            if scene_name in self.scenes:
                self.log("[D004] Manually activating scene '{}'".format(scene_name))
                self._start_scene(scene_name, immediate=True)
            else:
                self.log(
                    "[D005] Scene '{}' not found in configuration".format(scene_name)
                )

    def _handle_sun_pos(self, entity, attribute, old, new, **kwargs):
        """Handle sun position changes with throttling."""
        now = time.monotonic()
        if now - self._last_throttle_time < SUN_HANDLER_THROTTLE_SECONDS:
            return
        self._last_throttle_time = now

        elevation = self._get_sun_elevation()
        is_rising = self._get_sun_rising()

        if elevation is None or is_rising is None:
            return

        if self.solar.is_enabled:
            self._process_solar_transitions(elevation, is_rising)
        else:
            self._process_elevation_transitions(elevation, is_rising)

    # ── Sensor helpers ─────────────────────────────────────────────

    def _get_sun_elevation(self) -> float | None:
        """Read current sun elevation from sun.sun attribute, returning None on failure."""
        raw = self.get_state(SUN_ENTITY, attribute="elevation")
        if raw is None or str(raw) in HA_UNAVAILABLE_STATES:
            self.log("[S005] Sun elevation unavailable: '{}'".format(raw))
            return None
        try:
            return float(raw)
        except (ValueError, TypeError):
            self.log("[S006] Unparseable sun elevation: '{}'".format(raw))
            return None

    def _get_sun_rising(self) -> bool | None:
        """Read whether sun is currently rising from sun.sun attribute, returning None on failure."""
        raw = self.get_state(SUN_ENTITY, attribute="rising")
        if raw is None or str(raw) in HA_UNAVAILABLE_STATES:
            self.log("[S007] Sun rising sensor unavailable: '{}'".format(raw))
            return None
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            return raw.lower() in ("true", "1", "yes", "on")
        return bool(raw)

    def _get_solar_radiation(self) -> float | None:
        """Read solar radiation sensor, returning None on failure."""
        raw = self.get_state(self.solar.sensor)
        if raw is None or str(raw) in HA_UNAVAILABLE_STATES:
            self.log(
                "[S001] Solar sensor '{}' unavailable: '{}'".format(
                    self.solar.sensor, raw
                )
            )
            return None
        try:
            return float(raw)
        except (ValueError, TypeError):
            self.log(
                "[S002] Solar sensor '{}' unparseable: '{}'".format(
                    self.solar.sensor, raw
                )
            )
            return None

    # ── State transitions ──────────────────────────────────────────

    def _process_solar_transitions(self, elevation: float, is_rising: bool):
        """Process state transitions using solar radiation sensor."""
        light_level = self._get_solar_radiation()
        if light_level is None:
            return

        elev_threshold = self.solar.elevation_threshold
        light_threshold = self.solar.threshold

        if (
            self.current_state in ("morning", "late_morning")
            and is_rising
            and elevation > elev_threshold
            and light_level > light_threshold
        ):
            self._start_scene("day")
        elif (
            self.current_state == "day"
            and not is_rising
            and (light_level < light_threshold or elevation < elev_threshold)
        ):
            self._start_scene("evening")
        else:
            self._log_no_transition(elevation, is_rising, light_level)

    def _process_elevation_transitions(self, elevation: float, is_rising: bool):
        """Process state transitions using elevation only."""
        elev_threshold = self.solar.elevation_threshold

        if (
            self.current_state in ("morning", "late_morning")
            and is_rising
            and elevation > elev_threshold
        ):
            self._start_scene("day")
        elif (
            self.current_state == "day"
            and not is_rising
            and elevation < elev_threshold
        ):
            self._start_scene("evening")
        else:
            self._log_no_transition(elevation, is_rising)

    def _log_no_transition(
        self,
        elevation: float,
        is_rising: bool,
        light_level: float | None = None,
    ):
        """Log when no transition occurs, throttled to reduce noise."""
        self._no_transition_log_counter += 1
        if self._no_transition_log_counter % NO_TRANSITION_LOG_INTERVAL != 1:
            return

        if light_level is not None:
            self.log(
                "[S003] No transition: state={}, elev={:.1f}, rising={}, light={:.1f}".format(
                    self.current_state, elevation, is_rising, light_level
                )
            )
        else:
            self.log(
                "[S004] No transition: state={}, elev={:.1f}, rising={}".format(
                    self.current_state, elevation, is_rising
                )
            )

    def _calculate_state(self) -> str:
        """Calculate initial state based on current time and sun position.

        State order: night → morning → late_morning → day → evening → early_night → night
        """
        now = self.time()
        sunrise = self.sunrise().time()
        sunset = self.sunset().time()
        morning_start = self.parse_time(self.morning_start)
        night_start = self.parse_time(self.night_start)
        late_morning_start = (
            self.parse_time(self.late_morning_start)
            if self.late_morning_start
            else None
        )
        early_night_start = (
            self.parse_time(self.early_night_start)
            if self.early_night_start
            else None
        )

        if now <= morning_start:
            # Check if night_start is past midnight and we haven't reached it yet
            if night_start < morning_start and now < night_start:
                if early_night_start:
                    # If early_night_start is before midnight (> night_start in time
                    # ordering), it already passed yesterday, so state is early_night.
                    # If early_night_start is also past midnight, compare directly.
                    if early_night_start > night_start or now >= early_night_start:
                        return "early_night"
                return "evening"
            return "night"
        if now < sunrise:
            # After morning_start but before sunrise: morning or late_morning
            if late_morning_start and now >= late_morning_start:
                return "late_morning"
            return "morning"
        # When morning_start >= sunrise (summer), morning/late_morning states are
        # skipped in _calculate_state because the sun is already up. The scheduled
        # callbacks handle the morning scene activation at runtime.
        if now >= sunrise and now < sunset:
            return "day"
        if now >= sunset:
            # Handle night_start at or past midnight (e.g., 00:00):
            # when night_start <= sunset, evening/early_night runs from sunset until midnight
            if night_start <= sunset or now < night_start:
                if early_night_start and now >= early_night_start:
                    return "early_night"
                return "evening"
            return "night"
        return "night"

    # ── Scene activation ───────────────────────────────────────────

    def _on_morning_schedule(self, **kwargs):
        """Scheduled morning callback."""
        if self.current_state == "day":
            return  # Already in day, no need to regress
        self._start_scene("morning")

    def _on_late_morning_schedule(self, **kwargs):
        """Scheduled late morning callback."""
        if self.current_state not in ("morning", "night"):
            return  # State has already advanced past late_morning
        self._start_scene("late_morning")

    def _on_early_night_schedule(self, **kwargs):
        """Scheduled early night callback."""
        if self.current_state != "evening":
            return  # Only transition to early_night from evening
        self._start_scene("early_night")

    def _on_night_schedule(self, **kwargs):
        """Scheduled night callback."""
        self._start_scene("night")

    def _start_scene(self, scene_name: str, *, immediate: bool = False):
        """Transition to a new scene and activate it.

        Args:
            scene_name: Name of the scene to activate.
            immediate: If True, control entities immediately (no stagger).
                       Used for manual triggers and initialization.
        """
        if scene_name == "evening" and self.current_state in ("early_night", "night"):
            self.log(
                "[E002] Blocked evening transition: already in {}".format(
                    self.current_state
                )
            )
            return

        if scene_name == self.current_state and not immediate:
            self.log(
                "[E003] Skipped transition: already in '{}'".format(scene_name)
            )
            return

        # Cancel any pending staggered timers from the previous scene
        for handle in self._pending_timers:
            self.cancel_timer(handle)
        self._pending_timers.clear()

        self.log("[E001] Transitioning to scene '{}'".format(scene_name))
        self.current_state = scene_name
        self.set_state(TIME_STATE_ENTITY, state=scene_name)
        self._no_transition_log_counter = 0
        self._activate_scene(scene_name, immediate=immediate)

    def _activate_cumulative_state(self, target_state: str):
        """Replay all scenes from night through target_state to build cumulative state.

        Scenes are deltas: each only defines the groups it changes. On init, we
        must merge all predecessor scenes to reconstruct the full lighting state.
        For example, early_night only sets bedroom=off, but general/night lighting
        should be on (set by the evening scene earlier in the chain).
        """
        if target_state not in STATE_ORDER:
            self.log(
                "[F006] State '{}' not in state order, "
                "activating directly".format(target_state)
            )
            if target_state in self.scenes:
                self._activate_scene(target_state, immediate=True)
            return

        target_idx = STATE_ORDER.index(target_state)
        # Walk night -> ... -> target_state, merging group states
        merged: dict[str, bool] = {}
        chain = []
        for i in range(target_idx + 1):
            state_name = STATE_ORDER[i]
            if state_name in self.scenes:
                for group_name, target in self.scenes[state_name].items():
                    merged[group_name] = target
                chain.append(state_name)

        self.log(
            "[F007] Cumulative init for '{}': replayed {} scenes ({}), "
            "{} groups to control".format(
                target_state, len(chain), " -> ".join(chain), len(merged)
            )
        )

        # Build EntityControl list from the merged state
        entities: list[EntityControl] = []
        for group_name, target in merged.items():
            group_id = "group.{}".format(group_name)

            if group_id in self.group_area_entities:
                for area, area_entities in self.group_area_entities[group_id].items():
                    for entity_id in area_entities:
                        entities.append(
                            EntityControl(
                                entity_id=entity_id,
                                target_state=target,
                                area=area,
                                group=group_name,
                            )
                        )
            else:
                for entity_id in self.groups.get(group_id, []):
                    entities.append(
                        EntityControl(
                            entity_id=entity_id,
                            target_state=target,
                            area=self.entity_to_area.get(entity_id, "unknown"),
                            group=group_name,
                        )
                    )

        if not entities:
            self.log("[F008] No entities to control after cumulative merge")
            return

        self.log(
            "[F009] Immediate control for {} entities (cumulative)".format(
                len(entities)
            )
        )
        for ec in entities:
            self._turn_onoff(entity=ec.entity_id, state=ec.target_state)

    def _activate_scene(self, scene_name: str, *, immediate: bool = False):
        """Activate a scene by controlling its group entities."""
        self.log(
            "[F001] Activating scene '{}' (immediate={})".format(
                scene_name, immediate
            )
        )

        if scene_name not in self.scenes:
            self.log("[F002] Scene '{}' not in configuration".format(scene_name))
            return

        entities = self._collect_scene_entities(scene_name)
        self.log(
            "[F003] Scene '{}': {} entities to control".format(
                scene_name, len(entities)
            )
        )

        if not entities:
            return

        if immediate:
            self.log(
                "[F004] Immediate control for {} entities".format(len(entities))
            )
            for ec in entities:
                self._turn_onoff(entity=ec.entity_id, state=ec.target_state)
        else:
            self.log(
                "[F005] Staggered control for {} entities".format(len(entities))
            )
            self._execute_staggered_control(entities)

    def _collect_scene_entities(self, scene_name: str) -> list[EntityControl]:
        """Collect all entities for a scene with their target states."""
        entities: list[EntityControl] = []
        scene_config = self.scenes[scene_name]

        for group_name, target_state in scene_config.items():
            group_id = f"group.{group_name}"

            if group_id in self.group_area_entities:
                for area, area_entities in self.group_area_entities[group_id].items():
                    for entity_id in area_entities:
                        entities.append(
                            EntityControl(
                                entity_id=entity_id,
                                target_state=target_state,
                                area=area,
                                group=group_name,
                            )
                        )
            else:
                for entity_id in self.groups.get(group_id, []):
                    entities.append(
                        EntityControl(
                            entity_id=entity_id,
                            target_state=target_state,
                            area=self.entity_to_area.get(entity_id, "unknown"),
                            group=group_name,
                        )
                    )

        return entities

    def _execute_staggered_control(self, entities: list[EntityControl]):
        """Schedule entity control with randomised area-based staggering."""
        self.log("[G001] Starting staggered control")

        if not entities:
            self.log("[G002] No entities to control")
            return

        # Group by area
        area_groups: dict[str, list[EntityControl]] = {}
        for ec in entities:
            area_groups.setdefault(ec.area, []).append(ec)

        areas = list(area_groups)
        random.shuffle(areas)
        self.log("[G007] Randomised area order: {}".format(areas))

        current_delay = 0.0

        for area in areas:
            area_entities = area_groups[area]
            self.log(
                "[G008] Area '{}': {} entities".format(area, len(area_entities))
            )

            entity_delay = current_delay
            for i, ec in enumerate(area_entities):
                if i > 0:
                    entity_delay += random.uniform(
                        self.stagger.light_delay_min,
                        self.stagger.light_delay_max,
                    )

                self.log(
                    "[G010] {} (state={}, group={}) scheduled in {:.1f}s".format(
                        ec.entity_id, ec.target_state, ec.group, entity_delay
                    )
                )

                handle = self.run_in(
                    self._turn_onoff,
                    entity_delay,
                    entity=ec.entity_id,
                    state=ec.target_state,
                )
                self._pending_timers.append(handle)

            if len(areas) > 1:
                current_delay += random.uniform(
                    self.stagger.room_delay_min,
                    self.stagger.room_delay_max,
                )

        self.log("[G013] Staggered control scheduled")

    def _turn_onoff(self, **kwargs):
        """Turn an entity on or off."""
        entity = kwargs.get("entity")
        state = kwargs.get("state")

        if not entity or state is None:
            self.log(
                "[H004] Invalid call: entity={}, state={}".format(entity, state)
            )
            return

        try:
            if state:
                self.turn_on(entity)
                self.log("[H001] Turned ON: {}".format(entity))
            else:
                self.turn_off(entity)
                self.log("[H002] Turned OFF: {}".format(entity))
        except Exception as exc:
            if isinstance(exc, (TypeError, AttributeError, NameError)):
                raise  # Programming error, do not swallow
            self.log(
                "[H003] Failed to control {}: {} ({})".format(
                    entity, exc, type(exc).__name__
                ),
                level="ERROR",
            )
