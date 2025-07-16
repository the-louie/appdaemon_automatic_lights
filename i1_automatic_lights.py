"""
Automatic Lights - Home Assistant AppDaemon App

This module provides intelligent lighting control based on time, sun position,
and optional solar radiation sensors. It manages four distinct lighting modes
throughout the day with seamless transitions based on environmental conditions.

Copyright (c) 2025 the_louie
Licensed under BSD 2-Clause License
"""

import time
import traceback
from datetime import datetime
from typing import Any, Dict, List, Optional, Union

import appdaemon.plugins.hass.hassapi as hass

# Constants
DEFAULT_ELEVATION_THRESHOLD = 3.0
CACHE_EXPIRY_HOURS = 6
MAX_RETRIES = 1
RETRY_DELAY = 0.1
DEFAULT_MORNING_START = "05:30"
DEFAULT_NIGHT_START = "23:30"
RANDOM_DELAY_SECONDS = 600
SCENE_PREFIX = "scene."
GROUP_PREFIX = "group."
TIME_STATE_ENTITY = "irisone.time_state"
SUN_ELEVATION_SENSOR = "sensor.sun_solar_elevation"
SUN_RISING_SENSOR = "sensor.sun_solar_rising"
MORNING_RANDOM_START = -45 * 60
MORNING_RANDOM_END = -30 * 60
NIGHT_RANDOM_START = -15 * 60
NIGHT_RANDOM_END = 10 * 60


class AutomaticLights(hass.Hass):
    """
    Home Assistant AppDaemon app for automatic lighting control.

    This app manages lighting scenes throughout the day based on:
    - Time-based triggers (morning and night)
    - Sun position and solar radiation levels
    - Manual scene activation

    The app provides four lighting modes:
    - Night: Low ambient lighting for late night hours
    - Morning: Gentle wake-up lighting before sunrise
    - Day: Full lighting during daylight hours
    - Evening: Transitional lighting as daylight fades

    Configuration Parameters:
        morning_start (str): Time to start morning scene (format: HH:MM)
        night_start (str): Time to start night scene (format: HH:MM)
        solar_radiation (dict, optional): Solar radiation configuration
        scenes (dict): Scene configurations mapping groups to states

    Solar Radiation Configuration:
        sensor (str): Entity ID of the light level sensor
        threshold (float): Light level threshold for transitions
        elevation_threshold (float, optional): Solar elevation threshold (default: 3)
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.last_state_change_time = 0
        self.state_change_debounce_seconds = 60
        self.sensor_cache = {}
        self.sensor_cache_time = {}
        self.sensor_cache_duration = 60

    def initialize(self) -> None:
        """Initialize the automatic lights app."""
        self._initialize_time_config()
        self._initialize_solar_radiation_config()
        self._initialize_state_and_cache()
        self._register_event_listeners()
        self._setup_scheduled_events()

        solar_status = "enabled" if self.solar_radiation else "disabled"
        self.log(
            "AutomaticLights initialized: morning={}, night={}, solar_radiation={}, scenes={}".format(
                self.morning_start, self.night_start, solar_status, len(self.scenes)
            )
        )

    def _initialize_time_config(self) -> None:
        """Initialize and validate time-based configuration."""
        try:
            self.morning_start = str(self.args.get("morning_start", DEFAULT_MORNING_START))
            self.night_start = str(self.args.get("night_start", DEFAULT_NIGHT_START))

            if not self.morning_start or self.morning_start == "None":
                self.log("ERROR: morning_start is required, format: HH:MM - using default {}".format(DEFAULT_MORNING_START))
                self.morning_start = DEFAULT_MORNING_START

            if not self.night_start or self.night_start == "None":
                self.log("ERROR: night_start is required, format: HH:MM - using default {}".format(DEFAULT_NIGHT_START))
                self.night_start = DEFAULT_NIGHT_START

            self.parse_time(self.morning_start)
            self.parse_time(self.night_start)

        except Exception as e:
            line_num = traceback.extract_stack()[-1].lineno
            self.log("ERROR: Failed to initialize time configuration at line {}: {}".format(line_num, e))
            raise ValueError("Invalid time configuration") from e

    def _initialize_solar_radiation_config(self) -> None:
        """Initialize and validate solar radiation configuration."""
        self.solar_radiation = self.args.get("solar_radiation")

        if not self.solar_radiation:
            return

        try:
            if not isinstance(self.solar_radiation, dict):
                self.log("ERROR: solar_radiation must be a dictionary - disabling solar radiation monitoring")
                self.solar_radiation = None
                return

            required_keys = ["sensor", "threshold"]
            if not all(key in self.solar_radiation for key in required_keys):
                self.log("ERROR: solar_radiation must contain 'sensor' and 'threshold' keys - disabling monitoring")
                self.solar_radiation = None
                return

            threshold = self.solar_radiation.get("threshold")
            if threshold is None:
                self.log("ERROR: solar_radiation threshold cannot be None - disabling solar radiation monitoring")
                self.solar_radiation = None
                return

            try:
                float(threshold)
            except (ValueError, TypeError):
                self.log("ERROR: solar_radiation threshold must be a numeric value - disabling monitoring")
                self.solar_radiation = None
                return

        except Exception as e:
            line_num = traceback.extract_stack()[-1].lineno
            self.log("ERROR: Failed to initialize solar radiation configuration at line {}: {}".format(line_num, e))
            self.solar_radiation = None

    def _initialize_state_and_cache(self) -> None:
        """Initialize state tracking and group caching."""
        try:
            self.current_state = self.calculate_state()
            self.groups: Dict[str, List[str]] = {}
            self.groups_cache_time: Optional[datetime] = None
            self.get_groups()
            self.scenes = self.args.get("scenes", {})

            if self.current_state in self.scenes:
                self.activate_scene(self.current_state, run_now=True)
            else:
                self.log("WARNING: No scene configuration found for initial state: {}".format(self.current_state))

        except Exception as e:
            line_num = traceback.extract_stack()[-1].lineno
            self.log("ERROR: Failed to initialize state and cache at line {}: {}".format(line_num, e))
            raise

    def _register_event_listeners(self) -> None:
        """Register event listeners for sensor changes and manual scene activation."""
        try:
            self.listen_state(self.sun_pos, SUN_ELEVATION_SENSOR)
            self.listen_event(self.manual_scene, event='call_service', domain='scene')
        except Exception as e:
            line_num = traceback.extract_stack()[-1].lineno
            self.log("ERROR: Failed to register event listeners at line {}: {}".format(line_num, e))
            raise

    def _setup_scheduled_events(self) -> None:
        """Set up scheduled daily events for time-based transitions."""
        try:
            self.run_daily(
                self.start_morning, self.morning_start,
                random_start=MORNING_RANDOM_START, random_end=MORNING_RANDOM_END
            )
            self.run_daily(
                self.start_night, self.night_start,
                random_start=NIGHT_RANDOM_START, random_end=NIGHT_RANDOM_END
            )
        except Exception as e:
            line_num = traceback.extract_stack()[-1].lineno
            self.log("ERROR: Failed to setup scheduled events at line {}: {}".format(line_num, e))
            raise

    def manual_scene(self, event_name: str, data: Dict[str, Any], kwargs: Dict[str, Any]) -> None:
        """Handle manual scene activation events from Home Assistant."""
        try:
            service_data = data.get("service_data", {})
            scene_entity = service_data.get("entity_id")

            if scene_entity is None:
                self.log("ERROR: No entity_id found in manual scene event data")
                return

            if isinstance(scene_entity, list):
                scene_entities = scene_entity
            else:
                scene_entities = [scene_entity]

            for scene_entity in scene_entities:
                try:
                    if not scene_entity.startswith(SCENE_PREFIX):
                        self.log("WARNING: Invalid scene entity_id format: {}".format(scene_entity))
                        continue

                    scene_name = scene_entity.replace(SCENE_PREFIX, "")

                    if not scene_name:
                        self.log("ERROR: Invalid scene entity_id: {}".format(scene_entity))
                        continue

                    if scene_name not in self.scenes:
                        available_scenes = list(self.scenes.keys())
                        self.log("ERROR: Scene '{}' not found in configuration. Available scenes: {}".format(
                            scene_name, available_scenes))
                        continue

                    self.activate_scene(scene_name, run_now=True)

                except Exception as e:
                    line_num = traceback.extract_stack()[-1].lineno
                    self.log("ERROR: Failed to process scene entity '{}' at line {}: {}".format(
                        scene_entity, line_num, e))
                    continue

        except Exception as e:
            line_num = traceback.extract_stack()[-1].lineno
            self.log("ERROR: Failed to handle manual scene event at line {}: {}".format(line_num, e))

    def get_groups(self) -> None:
        """Retrieve and cache all Home Assistant groups."""
        try:
            state_groups = self._get_safe_state("group", log_name="Groups")
            if state_groups is None or not isinstance(state_groups, dict):
                self.log("ERROR: Failed to retrieve groups from Home Assistant or invalid format")
                return

            self.groups = {}

            for group_id, group_data in state_groups.items():
                try:
                    entities = group_data.get("attributes", {}).get("entity_id", [])

                    if not isinstance(entities, list):
                        self.log("WARNING: Invalid entity list for group {}: {}".format(group_id, entities))
                        continue

                    self.groups[group_id] = entities

                except Exception as e:
                    line_num = traceback.extract_stack()[-1].lineno
                    self.log("ERROR: Failed to process group {} at line {}: {}".format(group_id, line_num, e))
                    continue

            self.groups_cache_time = datetime.now()

        except Exception as e:
            line_num = traceback.extract_stack()[-1].lineno
            self.log("ERROR: Failed to get groups at line {}: {}".format(line_num, e))

    def sun_pos(self, entity: str, attribute: str, old: str, new: str, kwargs: Dict[str, Any]) -> None:
        """Handle sun position state changes and manage runtime state transitions."""
        try:
            current_time = time.time()
            if current_time - self.last_state_change_time < self.state_change_debounce_seconds:
                return
            self.last_state_change_time = current_time

            sensor_data = self._get_sensor_data()
            if sensor_data is None:
                return

            current_elevation, is_rising = sensor_data

            if self.solar_radiation:
                self._process_solar_radiation_transitions(current_elevation, is_rising)
            else:
                self._process_elevation_only_transitions(current_elevation, is_rising)

        except Exception as e:
            line_num = traceback.extract_stack()[-1].lineno
            self.log("ERROR: Failed to process sun position change at line {}: {}".format(line_num, e))

    def _get_sensor_data(self) -> Optional[tuple[float, bool]]:
        """Retrieve and validate sensor data for sun position calculations."""
        try:
            elevation_state = self._get_safe_state(SUN_ELEVATION_SENSOR, log_name="Solar elevation sensor")
            if elevation_state is None or not isinstance(elevation_state, str):
                return None

            rising_state = self._get_safe_state(SUN_RISING_SENSOR, log_name="Sun rising sensor")
            if rising_state is None:
                return None

            try:
                current_elevation = float(elevation_state)
            except (ValueError, TypeError) as e:
                line_num = traceback.extract_stack()[-1].lineno
                self.log("ERROR: Failed to convert elevation value '{}' to float at line {}: {}".format(
                    elevation_state, line_num, e))
                return None

            if isinstance(rising_state, bool):
                is_rising = rising_state
            elif isinstance(rising_state, str):
                is_rising = rising_state.lower() in ('true', '1', 'yes', 'on')
            else:
                line_num = traceback.extract_stack()[-1].lineno
                self.log("ERROR: Invalid rising state type '{}' at line {}: expected bool or string".format(
                    type(rising_state), line_num))
                return None

            return current_elevation, is_rising

        except Exception as e:
            line_num = traceback.extract_stack()[-1].lineno
            self.log("ERROR: Failed to retrieve sensor data at line {}: {}".format(line_num, e))
            return None

    def _process_solar_radiation_transitions(self, current_elevation: float, is_rising: bool) -> None:
        """Process state transitions when solar radiation monitoring is enabled."""
        try:
            if not self.solar_radiation or not isinstance(self.solar_radiation, dict):
                return

            sensor_id = self.solar_radiation.get("sensor")
            if not sensor_id:
                return

            light_state = self._get_safe_state(sensor_id, attribute="state", log_name="Light level sensor")
            if light_state is None or not isinstance(light_state, str):
                return

            try:
                light_level = float(light_state)
            except (ValueError, TypeError) as e:
                line_num = traceback.extract_stack()[-1].lineno
                self.log("ERROR: Failed to convert light level '{}' to float at line {}: {}".format(
                    light_state, line_num, e))
                return

            threshold = self.solar_radiation.get("threshold")
            elevation_threshold = self.solar_radiation.get("elevation_threshold", DEFAULT_ELEVATION_THRESHOLD)

            if threshold is None:
                return

            try:
                threshold = float(threshold)
            except (ValueError, TypeError) as e:
                line_num = traceback.extract_stack()[-1].lineno
                self.log("ERROR: Failed to convert threshold '{}' to float at line {}: {}".format(
                    self.solar_radiation.get("threshold"), line_num, e))
                return

            if (self.current_state == "morning" and is_rising and
                    current_elevation > elevation_threshold and light_level > threshold):
                self.start_day(None)

            elif self.current_state == "day" and not is_rising:
                if light_level < threshold or current_elevation < elevation_threshold:
                    self.start_evening(None)

        except Exception as e:
            line_num = traceback.extract_stack()[-1].lineno
            self.log("ERROR: Failed to process solar radiation transitions at line {}: {}".format(line_num, e))

    def _process_elevation_only_transitions(self, current_elevation: float, is_rising: bool) -> None:
        """Process state transitions when solar radiation monitoring is disabled."""
        try:
            elevation_threshold = DEFAULT_ELEVATION_THRESHOLD

            if (self.current_state == "morning" and is_rising and current_elevation > elevation_threshold):
                self.start_day(None)

            elif (self.current_state == "day" and not is_rising and current_elevation < elevation_threshold):
                self.start_evening(None)

        except Exception as e:
            line_num = traceback.extract_stack()[-1].lineno
            self.log("ERROR: Failed to process elevation-only transitions at line {}: {}".format(line_num, e))

    def calculate_state(self) -> str:
        """Calculate the initial state based on time, sun position, and solar radiation."""
        try:
            now = self.time()
            sunrise = self.sunrise().time()
            sunset = self.sunset().time()

            try:
                morning_start = self.parse_time(self.morning_start)
            except Exception as e:
                self.log("ERROR: Invalid morning_start time format '{}': {} - using default {}".format(
                    self.morning_start, e, DEFAULT_MORNING_START))
                morning_start = self.parse_time(DEFAULT_MORNING_START)

            try:
                night_start = self.parse_time(self.night_start)
            except Exception as e:
                self.log("ERROR: Invalid night_start time format '{}': {} - using default {}".format(
                    self.night_start, e, DEFAULT_NIGHT_START))
                night_start = self.parse_time(DEFAULT_NIGHT_START)

            sensor_data = self._get_sensor_data()
            current_elevation = None
            is_rising = None

            if sensor_data is not None:
                current_elevation, is_rising = sensor_data

            if now <= sunrise and now <= morning_start:
                initial_state = "night"
            elif now > morning_start and now < sunrise:
                initial_state = "morning"
            elif now >= sunrise and now < sunset:
                initial_state = "day"
            elif now >= sunset and now < night_start:
                initial_state = "evening"
            else:
                initial_state = "night"

            if sensor_data is not None and self.solar_radiation and current_elevation is not None and is_rising is not None:
                initial_state = self._enhance_state_with_solar_radiation(initial_state, current_elevation, is_rising)
            elif sensor_data is not None and current_elevation is not None and is_rising is not None:
                initial_state = self._enhance_state_with_elevation_only(initial_state, current_elevation, is_rising)

            return initial_state

        except Exception as e:
            line_num = traceback.extract_stack()[-1].lineno
            self.log("ERROR: Failed to calculate initial state at line {}: {}".format(line_num, e))
            return "night"

    def _enhance_state_with_solar_radiation(self, initial_state: str, current_elevation: float, is_rising: bool) -> str:
        """Enhance state calculation with solar radiation data."""
        try:
            if not self.solar_radiation:
                return initial_state

            light_state = self._get_safe_state(
                self.solar_radiation.get("sensor"),
                attribute="state",
                log_name="Light level sensor"
            )
            if light_state is None or not isinstance(light_state, str):
                return initial_state

            light_level = float(light_state)
            threshold = self.solar_radiation.get("threshold")
            elevation_threshold = self.solar_radiation.get("elevation_threshold", DEFAULT_ELEVATION_THRESHOLD)

            if initial_state == "day":
                if light_level < threshold or (not is_rising and current_elevation < elevation_threshold):
                    return "evening"

            elif initial_state == "evening":
                if light_level > threshold and is_rising and current_elevation > elevation_threshold:
                    return "day"

            return initial_state

        except Exception as e:
            line_num = traceback.extract_stack()[-1].lineno
            self.log("ERROR: Failed to enhance state with solar radiation at line {}: {}".format(line_num, e))
            return initial_state

    def _enhance_state_with_elevation_only(self, initial_state: str, current_elevation: float, is_rising: bool) -> str:
        """Enhance state calculation with elevation data only."""
        try:
            elevation_threshold = DEFAULT_ELEVATION_THRESHOLD

            if initial_state == "day":
                if not is_rising and current_elevation < elevation_threshold:
                    return "evening"

            elif initial_state == "evening":
                if is_rising and current_elevation > elevation_threshold:
                    return "day"

            return initial_state

        except Exception as e:
            line_num = traceback.extract_stack()[-1].lineno
            self.log("ERROR: Failed to enhance state with elevation only at line {}: {}".format(line_num, e))
            return initial_state

    def start_morning(self, _kwargs: Optional[Dict[str, Any]]) -> None:
        """Start the morning scene with validation."""
        try:
            try:
                sun_state = self.sun_up()

                if sun_state is None:
                    self.log("Morning scene skipped: sun state unavailable, no state change")
                    return

                should_skip = sun_state or self.current_state == "day"
                if should_skip:
                    self.log("Morning scene skipped: sun_state={}, current_state={}, activating day scene".format(
                        sun_state, self.current_state))
                    self._start_scene("day")
                    return
            except Exception as e:
                line_num = traceback.extract_stack()[-1].lineno
                self.log("ERROR: Failed to check sun state at line {}: {}".format(line_num, e))

            self._start_scene("morning")

        except Exception as e:
            line_num = traceback.extract_stack()[-1].lineno
            self.log("ERROR: Failed to start morning scene at line {}: {}".format(line_num, e))

    def start_day(self, _kwargs: Optional[Dict[str, Any]]) -> None:
        """Start the day scene."""
        try:
            self._start_scene("day")
        except Exception as e:
            line_num = traceback.extract_stack()[-1].lineno
            self.log("ERROR: Failed to start day scene at line {}: {}".format(line_num, e))

    def start_evening(self, _kwargs: Optional[Dict[str, Any]]) -> None:
        """Start the evening scene with validation."""
        try:
            should_skip = self.current_state == "night"
            if should_skip:
                self.log("Evening scene skipped: current_state={}".format(self.current_state))
                return

            self._start_scene("evening")
        except Exception as e:
            line_num = traceback.extract_stack()[-1].lineno
            self.log("ERROR: Failed to start evening scene at line {}: {}".format(line_num, e))

    def start_night(self, _kwargs: Optional[Dict[str, Any]]) -> None:
        """Start the night scene."""
        try:
            self._start_scene("night")
        except Exception as e:
            line_num = traceback.extract_stack()[-1].lineno
            self.log("ERROR: Failed to start night scene at line {}: {}".format(line_num, e))

    def activate_scene(self, scene_name: str, run_now: bool = False) -> None:
        """Activate a specific scene by controlling group entities."""
        try:
            if scene_name not in self.scenes:
                available_scenes = list(self.scenes.keys())
                self.log("ERROR: Scene '{}' not found in configuration. Available scenes: {}".format(
                    scene_name, available_scenes))
                return

            if self.current_state != scene_name:
                self.current_state = scene_name
                self.set_state(TIME_STATE_ENTITY, state=scene_name)

            self._refresh_group_cache_if_needed()

            scene_config = self.scenes.get(scene_name, {})
            if not scene_config:
                self.log("WARNING: Scene '{}' has no configuration".format(scene_name))
                return

            self._process_scene_configuration(scene_name, scene_config, run_now)

        except Exception as e:
            line_num = traceback.extract_stack()[-1].lineno
            self.log("ERROR: Failed to activate scene '{}' at line {}: {}".format(scene_name, line_num, e))

    def _refresh_group_cache_if_needed(self) -> None:
        """Refresh the group cache if it's empty or expired."""
        try:
            if not self.groups or not self.groups_cache_time:
                self.get_groups()
            else:
                now = datetime.now()
                cache_age = (now - self.groups_cache_time).total_seconds()
                if cache_age > CACHE_EXPIRY_HOURS * 3600:
                    self.get_groups()

        except Exception as e:
            line_num = traceback.extract_stack()[-1].lineno
            self.log("ERROR: Failed to refresh group cache at line {}: {}".format(line_num, e))

    def _process_scene_configuration(self, scene_name: str, scene_config: Dict[str, bool], run_now: bool) -> None:
        """Process scene configuration and control individual entities."""
        try:
            for group_name, group_state in scene_config.items():
                try:
                    if not isinstance(group_state, bool):
                        self.log("ERROR: Invalid group_state for group '{}' in scene '{}': {} (must be boolean)".format(
                            group_name, scene_name, group_state))
                        continue

                    group_entity_id = "{}{}".format(GROUP_PREFIX, group_name)
                    entities = self.groups.get(group_entity_id)

                    if entities is None:
                        self.log("ERROR: No entities found for group '{}' in scene '{}'".format(group_name, scene_name))
                        continue

                    if run_now:
                        for entity in entities:
                            try:
                                self._turn_onoff({"entity": entity, "state": group_state})
                            except Exception as e:
                                line_num = traceback.extract_stack()[-1].lineno
                                self.log("ERROR: Failed to control entity '{}' at line {}: {}".format(entity, line_num, e))
                    else:
                        for i, entity in enumerate(entities):
                            try:
                                delay = (i * 0.1) % (RANDOM_DELAY_SECONDS / 10)
                                self.run_in(self._turn_onoff, delay, entity=entity, state=group_state)
                            except Exception as e:
                                line_num = traceback.extract_stack()[-1].lineno
                                self.log("ERROR: Failed to schedule entity '{}' at line {}: {}".format(entity, line_num, e))

                except Exception as e:
                    line_num = traceback.extract_stack()[-1].lineno
                    self.log("ERROR: Failed to process group '{}' at line {}: {}".format(group_name, line_num, e))
                    continue

        except Exception as e:
            line_num = traceback.extract_stack()[-1].lineno
            self.log("ERROR: Failed to process scene configuration at line {}: {}".format(line_num, e))

    def _turn_onoff(self, kwargs: Dict[str, Any]) -> None:
        """Turn an entity on or off based on the specified state."""
        try:
            entity = kwargs.get("entity")
            state = kwargs.get("state")

            if entity is None or state is None:
                self.log("ERROR: Missing required parameters in _turn_onoff: entity={}, state={}".format(entity, state))
                return

            if not isinstance(state, bool):
                self.log("ERROR: Invalid state type in _turn_onoff: entity={}, state={} (must be boolean)".format(
                    entity, state))
                return

            for attempt in range(MAX_RETRIES):
                try:
                    if state:
                        self.turn_on(entity)
                    else:
                        self.turn_off(entity)
                    break

                except Exception as e:
                    if attempt < MAX_RETRIES - 1:
                        self.log("WARNING: Failed to control entity '{}' (attempt {}/{}), retrying...".format(
                            entity, attempt + 1, MAX_RETRIES))
                        time.sleep(RETRY_DELAY)
                    else:
                        raise

        except Exception as e:
            line_num = traceback.extract_stack()[-1].lineno
            self.log("ERROR: Failed to control entity '{}' after retries at line {}: {}".format(entity, line_num, e))

    def _get_safe_state(self, entity: str, attribute: Optional[str] = None, log_name: Optional[str] = None) -> Optional[Union[str, Dict[str, Any]]]:
        """Safely get state from Home Assistant entity with comprehensive error handling."""
        try:
            cache_key = f"{entity}:{attribute or 'state'}"
            current_time = time.time()

            if cache_key in self.sensor_cache:
                cache_age = current_time - self.sensor_cache_time.get(cache_key, 0)
                if cache_age < self.sensor_cache_duration:
                    return self.sensor_cache[cache_key]

            state = self.get_state(entity, attribute=attribute)

            if state is None or state == "unavailable":
                log_msg = log_name or entity
                self.log("Entity unavailable: {} = {}".format(log_msg, state))
                return None

            self.sensor_cache[cache_key] = state
            self.sensor_cache_time[cache_key] = current_time

            return state

        except Exception as e:
            log_msg = log_name or entity
            line_num = traceback.extract_stack()[-1].lineno
            self.log("ERROR: Failed to get state for '{}' at line {}: {}".format(log_msg, line_num, e))
            return None

    def _start_scene(self, scene_name: str) -> None:
        """Start a scene by updating state and activating it."""
        try:
            self.current_state = scene_name
            self.set_state(TIME_STATE_ENTITY, state=scene_name)
            self.activate_scene(scene_name)

        except Exception as e:
            line_num = traceback.extract_stack()[-1].lineno
            self.log("ERROR: Failed to start scene '{}' at line {}: {}".format(scene_name, line_num, e))
            raise ValueError(f"Scene activation failed for '{scene_name}'") from e

