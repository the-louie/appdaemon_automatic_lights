"""
Automatic Lights - Home Assistant AppDaemon App
Copyright (c) 2025 the_louie
"""

import random
import time
from typing import Dict, List

import appdaemon.plugins.hass.hassapi as hass

# Configuration defaults
DEFAULT_ELEVATION_THRESHOLD = 3.0
DEFAULT_MORNING_START = "05:30"
DEFAULT_NIGHT_START = "23:30"
LIGHT_DELAY_MAX = 5
LIGHT_DELAY_MIN = 2
ROOM_DELAY_MAX = 120
ROOM_DELAY_MIN = 30

# Entity constants
SUN_ELEVATION_SENSOR = "sensor.sun_solar_elevation"
SUN_RISING_SENSOR = "sensor.sun_solar_rising"
TIME_STATE_ENTITY = "irisone.time_state"


class AutomaticLights(hass.Hass):
    """Automatic lighting control based on time and sun position."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.current_state = "night"
        self.groups = {}
        self.last_state_change = 0
        self.area_list = []
        self.area_entity_map = {}
        self.entity_to_area = {}  # Cache for O(1) entity lookup
        self.group_area_entities = {}  # group_id -> {area: [entities]}

    def initialize(self):
        """Initialize the app."""
        self.log("[A001] Starting initialization")

        # Configuration
        self.morning_start = self.args.get("morning_start", DEFAULT_MORNING_START)
        self.night_start = self.args.get("night_start", DEFAULT_NIGHT_START)
        self.scenes = self.args.get("scenes", {})

        # Solar radiation config
        solar_config = self.args.get("solar_radiation", {})
        self.solar_sensor = solar_config.get("sensor")
        self.solar_threshold = solar_config.get("threshold")
        self.elevation_threshold = solar_config.get("elevation_threshold", DEFAULT_ELEVATION_THRESHOLD)

        # Validate solar threshold
        if self.solar_threshold is not None:
            try:
                self.solar_threshold = float(self.solar_threshold)
            except (ValueError, TypeError):
                self.log("[A004] WARNING: Invalid solar threshold, disabling solar radiation")
                self.solar_sensor = None
                self.solar_threshold = None

        # Staggering config
        stagger_config = self.args.get("staggering", {})
        self.light_delay_min = stagger_config.get("light_delay_min", LIGHT_DELAY_MIN)
        self.light_delay_max = stagger_config.get("light_delay_max", LIGHT_DELAY_MAX)
        self.room_delay_min = stagger_config.get("room_delay_min", ROOM_DELAY_MIN)
        self.room_delay_max = stagger_config.get("room_delay_max", ROOM_DELAY_MAX)

        self.log("[A002] Configuration loaded: morning={}, night={}, "
                "light_delay={}-{}s, area_delay={}-{}s".format(
                    self.morning_start, self.night_start,
                    self.light_delay_min, self.light_delay_max,
                    self.room_delay_min, self.room_delay_max))

        # Setup
        self._setup_groups_and_areas()
        self.current_state = self._calculate_state()

        # Register listeners
        self.listen_state(self._handle_sun_pos, SUN_ELEVATION_SENSOR)
        self.listen_event(self._handle_manual_scene, event='call_service', domain='scene')

        # Schedule daily events
        self.run_daily(self._start_morning, self.morning_start, random_start=-45*60, random_end=-30*60)
        self.run_daily(self._start_night, self.night_start, random_start=-15*60, random_end=10*60)

        # Activate initial scene
        if self.current_state in self.scenes:
            self._activate_scene(self.current_state, run_now=True)

        self.log("[A003] Initialization complete: state={}".format(self.current_state))

    def _setup_groups_and_areas(self):
        """Setup groups and area mapping."""
        self.log("[B001] Starting groups and areas setup")

        # Get groups and collect all configured entities
        configured_entities = set()
        state_groups = self.get_state("group")
        if isinstance(state_groups, dict):
            for group_id, group_data in state_groups.items():
                entities = group_data.get("attributes", {}).get("entity_id", [])
                if isinstance(entities, list):
                    self.groups[group_id] = entities
                    self.log("[B002] Group {}: {} entities".format(group_id, len(entities)))
        else:
            self.log("[B003] WARNING: No groups found or invalid format")

        # Collect all entities from configured groups
        for scene_config in self.scenes.values():
            for group_name in scene_config.keys():
                group_entity_id = f"group.{group_name}"
                if group_entity_id in self.groups:
                    configured_entities.update(self.groups[group_entity_id])

        self.log("[B004] Found {} entities in configured groups".format(len(configured_entities)))

        # Get areas and their entities
        self.log("[B005] Fetching areas from Home Assistant")
        self.area_list = self.areas()
        self.log("[B006] Found {} areas: {}".format(len(self.area_list), self.area_list))

        # Initialize group-area-entities lookup
        for scene_config in self.scenes.values():
            for group_name in scene_config.keys():
                group_entity_id = f"group.{group_name}"
                if group_entity_id in self.groups:
                    self.group_area_entities[group_entity_id] = {}

        for area in self.area_list:
            # Get entities from the area and filter them to only include entities in configured groups
            all_area_entities = self.area_entities(area)
            if all_area_entities:
                # Filter to only include configured entities
                filtered_area_entities = [entity for entity in all_area_entities if entity in configured_entities]
                self.area_entity_map[area] = filtered_area_entities

                # Cache entity-to-area mapping for filtered entities
                for entity in filtered_area_entities:
                    self.entity_to_area[entity] = area

                # Build group-area-entities lookup
                for group_entity_id, group_entities in self.groups.items():
                    if group_entity_id in self.group_area_entities:
                        # Find entities from this group that are in this area
                        group_entities_in_area = [entity for entity in filtered_area_entities if entity in group_entities]
                        if group_entities_in_area:
                            self.group_area_entities[group_entity_id][area] = group_entities_in_area

                self.log("[B007] Area '{}': {} entities ({} configured, {} total)".format(
                    area, len(filtered_area_entities), len(filtered_area_entities), len(all_area_entities)))
            else:
                self.log("[B008] Area '{}': No entities found".format(area))

        # Log group-area-entities summary
        for group_entity_id, area_entities in self.group_area_entities.items():
            total_entities = sum(len(entities) for entities in area_entities.values())
            self.log("[B010] Group {}: {} entities across {} areas".format(
                group_entity_id, total_entities, len(area_entities)))

        self.log("[B009] Areas setup complete: {} areas with entities, {} entities cached".format(
            len(self.area_entity_map), len(self.entity_to_area)))

        # Log detailed group-area-entity mapping
        self._log_group_area_entity_mapping()

    def _log_group_area_entity_mapping(self):
        """Log all entities in groups from config, grouped by area."""
        self.log("[B011] === GROUP-AREA-ENTITY MAPPING ===")

        # Get all groups that are actually used in the scenes configuration
        configured_groups = set()
        for scene_config in self.scenes.values():
            for group_name in scene_config.keys():
                group_entity_id = f"group.{group_name}"
                configured_groups.add(group_entity_id)

        for group_entity_id in sorted(configured_groups):
            if group_entity_id in self.group_area_entities:
                group_name = group_entity_id.replace("group.", "")
                self.log("[B012] {}:".format(group_name))

                area_entities = self.group_area_entities[group_entity_id]
                if area_entities:
                    for area_id in sorted(area_entities.keys()):
                        entities = area_entities[area_id]
                        area_name = self.area_name(area_id)
                        self.log("[B013]   {}:".format(area_name))
                        for entity in sorted(entities):
                            self.log("[B014]     - {}".format(entity))
                else:
                    self.log("[B015]   No entities found in any area")
            else:
                group_name = group_entity_id.replace("group.", "")
                self.log("[B016] {}: Group not found or has no entities".format(group_name))

        self.log("[B017] === END GROUP-AREA-ENTITY MAPPING ===")

    def _get_entities_by_area(self, target_entities: List[str]) -> Dict[str, List[str]]:
        """Group target entities by their area using O(1) lookup."""
        self.log("[C001] Grouping {} entities by area".format(len(target_entities)))
        area_groups = {}

        for entity_id in target_entities:
            # Use cached lookup for O(1) performance
            area = self.entity_to_area.get(entity_id, "unknown")
            area_groups.setdefault(area, []).append(entity_id)
            self.log("[C002] Entity {} -> Area '{}'".format(entity_id, area))

        # Log summary
        for area, entities in area_groups.items():
            self.log("[C004] Area '{}': {} entities to control".format(area, len(entities)))

        return area_groups

    def _get_group_entities_by_area(self, group_entity_id: str) -> Dict[str, List[str]]:
        """Get entities from a specific group grouped by area."""
        if group_entity_id not in self.group_area_entities:
            self.log("[C005] Group {} not found in area-entities lookup".format(group_entity_id))
            return {}

        area_entities = self.group_area_entities[group_entity_id]
        self.log("[C006] Group {}: {} entities across {} areas".format(
            group_entity_id, sum(len(entities) for entities in area_entities.values()), len(area_entities)))

        return area_entities

    def _handle_manual_scene(self, event_name, data, kwargs):
        """Handle manual scene activation."""
        self.log("[D001] Manual scene activation triggered")

        service_data = data.get("service_data", {})
        scene_entity = service_data.get("entity_id")

        if not scene_entity:
            self.log("[D002] No scene entity found in service data")
            return

        scene_entities = scene_entity if isinstance(scene_entity, list) else [scene_entity]
        self.log("[D003] Processing {} scene entities".format(len(scene_entities)))

        for scene_entity in scene_entities:
            if scene_entity.startswith("scene."):
                scene_name = scene_entity.replace("scene.", "")
                if scene_name in self.scenes:
                    self.log("[D004] Activating scene '{}'".format(scene_name))
                    self._activate_scene(scene_name, run_now=True)
                else:
                    self.log("[D005] Scene '{}' not found in configuration".format(scene_name))

    def _handle_sun_pos(self, entity, attribute, old, new, kwargs):
        """Handle sun position changes."""
        current_time = time.time()
        if current_time - self.last_state_change < 60:
            return
        self.last_state_change = current_time

        elevation_state = self.get_state(SUN_ELEVATION_SENSOR)
        rising_state = self.get_state(SUN_RISING_SENSOR)

        if not elevation_state or not rising_state:
            return

        try:
            current_elevation = float(elevation_state)
            is_rising = rising_state.lower() in ('true', '1', 'yes', 'on') if isinstance(rising_state, str) else rising_state
        except (ValueError, TypeError):
            return

        # Process transitions
        if self.solar_sensor and self.solar_threshold is not None:
            self._process_solar_transitions(current_elevation, is_rising)
        else:
            self._process_elevation_transitions(current_elevation, is_rising)

    def _process_solar_transitions(self, current_elevation, is_rising):
        """Process transitions with solar radiation."""
        light_state = self.get_state(self.solar_sensor, attribute="state")
        if not light_state:
            return

        try:
            light_level = float(light_state)
        except (ValueError, TypeError):
            return

        if (self.current_state == "morning" and is_rising and
                current_elevation > self.elevation_threshold and light_level > self.solar_threshold):
            self._start_day()
        elif (self.current_state == "day" and not is_rising and
                (light_level < self.solar_threshold or current_elevation < self.elevation_threshold)):
            self._start_evening()

    def _process_elevation_transitions(self, current_elevation, is_rising):
        """Process transitions with elevation only."""
        if (self.current_state == "morning" and is_rising and
                current_elevation > self.elevation_threshold):
            self._start_day()
        elif (self.current_state == "day" and not is_rising and
                current_elevation < self.elevation_threshold):
            self._start_evening()

    def _calculate_state(self):
        """Calculate initial state based on time and sun."""
        now = self.time()
        sunrise = self.sunrise().time()
        sunset = self.sunset().time()

        morning_start = self.parse_time(self.morning_start)
        night_start = self.parse_time(self.night_start)

        if now <= sunrise and now <= morning_start:
            return "night"
        elif now > morning_start and now < sunrise:
            return "morning"
        elif now >= sunrise and now < sunset:
            return "day"
        elif now >= sunset and now < night_start:
            return "evening"
        else:
            return "night"

    def _start_morning(self, kwargs):
        """Start morning scene."""
        if self.sun_up() or self.current_state == "day":
            self._start_day()
        else:
            self._start_scene("morning")

    def _start_day(self, kwargs=None):
        """Start day scene."""
        self._start_scene("day")

    def _start_evening(self, kwargs=None):
        """Start evening scene."""
        if self.current_state != "night":
            self._start_scene("evening")

    def _start_night(self, kwargs):
        """Start night scene."""
        self._start_scene("night")

    def _start_scene(self, scene_name):
        """Start a scene."""
        self.log("[E001] Starting scene '{}'".format(scene_name))

        self.current_state = scene_name
        self.set_state(TIME_STATE_ENTITY, state=scene_name)
        self._activate_scene(scene_name)

    def _activate_scene(self, scene_name, run_now=False):
        """Activate scene by controlling group entities."""
        self.log("[F001] Activating scene '{}' (run_now={})".format(scene_name, run_now))

        if scene_name not in self.scenes:
            self.log("[F002] Scene '{}' not found in configuration".format(scene_name))
            return

        scene_config = self.scenes[scene_name]
        all_entities = []

        # Collect all entities to control using group-area lookup
        for group_name, group_state in scene_config.items():
            group_entity_id = f"group.{group_name}"
            if group_entity_id in self.group_area_entities:
                # Use group-area lookup for efficient entity collection
                group_area_entities = self.group_area_entities[group_entity_id]
                for area, entities in group_area_entities.items():
                    for entity in entities:
                        all_entities.append({"entity": entity, "state": group_state, "area": area, "group": group_name})
            else:
                # Fallback to old method if group not in lookup
                entities = self.groups.get(group_entity_id, [])
                for entity in entities:
                    all_entities.append({"entity": entity, "state": group_state, "area": self.entity_to_area.get(entity, "unknown"), "group": group_name})

        self.log("[F003] Scene '{}': {} entities to control".format(scene_name, len(all_entities)))

        if run_now:
            # Immediate execution
            self.log("[F004] Executing immediate control for {} entities".format(len(all_entities)))
            for entity_info in all_entities:
                self._turn_onoff({"entity": entity_info["entity"], "state": entity_info["state"]})
        else:
            # Staggered execution with random area selection
            self.log("[F005] Starting staggered control for {} entities".format(len(all_entities)))
            self._execute_staggered_control(all_entities)

    def _execute_staggered_control(self, entities_to_control):
        """Execute light control with random area-based staggering."""
        self.log("[G001] Starting staggered control execution")

        if not entities_to_control:
            self.log("[G002] No entities to control, exiting")
            return

        # Group entities by area using the area information already in entity_info
        area_groups = {}
        for entity_info in entities_to_control:
            area = entity_info.get("area", "unknown")
            area_groups.setdefault(area, []).append(entity_info)

        self.log("[G003] Grouped entities by area: {}".format({area: len(entities) for area, entities in area_groups.items()}))

        if not area_groups:
            self.log("[G004] No area groups found, exiting")
            return

        # Create a list of areas with entities to control
        areas_with_entities = [area for area, entities in area_groups.items() if entities]

        if not areas_with_entities:
            self.log("[G005] No areas with entities to control, exiting")
            return

        self.log("[G006] Areas with entities: {}".format(areas_with_entities))

        # Randomize the order of areas
        random.shuffle(areas_with_entities)
        self.log("[G007] Randomized area order: {}".format(areas_with_entities))

        current_delay = 0

        for area in areas_with_entities:
            area_entities = area_groups[area]
            if not area_entities:
                continue

            self.log("[G008] Processing area '{}' with {} entities".format(area, len(area_entities)))

            # Process each entity in the area with random delays
            for i, entity_info in enumerate(area_entities):
                entity_delay = current_delay
                if i > 0:  # Add light delay for subsequent lights
                    light_delay = random.uniform(self.light_delay_min, self.light_delay_max)
                    entity_delay += light_delay
                    self.log("[G010] Entity {} (state={}, group={}) scheduled in {:.1f}s (added {:.1f}s delay)".format(
                        entity_info["entity"], entity_info["state"], entity_info["group"], entity_delay, light_delay))
                else:
                    self.log("[G011] Entity {} (state={}, group={}) scheduled in {:.1f}s (first entity in area)".format(
                        entity_info["entity"], entity_info["state"], entity_info["group"], entity_delay))

                self.run_in(
                    self._turn_onoff,
                    entity_delay,
                    entity=entity_info["entity"],
                    state=entity_info["state"]
                )

            # Add area delay for next area
            if len(areas_with_entities) > 1:
                area_delay = random.uniform(self.room_delay_min, self.room_delay_max)
                current_delay += area_delay
                self.log("[G012] Added {:.1f}s delay before next area (total delay now: {:.1f}s)".format(
                    area_delay, current_delay))

        self.log("[G013] Staggered control execution complete")

    def _turn_onoff(self, kwargs):
        """Turn entity on or off."""
        entity = kwargs.get("entity")
        state = kwargs.get("state")

        if entity and state is not None:
            try:
                if state:
                    self.turn_on(entity)
                    self.log("[H001] Turned ON entity: {}".format(entity))
                else:
                    self.turn_off(entity)
                    self.log("[H002] Turned OFF entity: {}".format(entity))
            except Exception as e:
                self.log("[H003] Failed to control entity {}: {}".format(entity, e), level="ERROR")
        else:
            self.log("[H004] Invalid entity or state: entity={}, state={}".format(entity, state))

