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
from typing import Any, Callable, Dict, List, Optional, Union

import appdaemon.plugins.hass.hassapi as hass

# Constants
DEFAULT_ELEVATION_THRESHOLD = 3.0
CACHE_EXPIRY_HOURS = 6
MAX_RETRIES = 2
RETRY_DELAY = 0.5
DEFAULT_MORNING_START = "05:30"
DEFAULT_NIGHT_START = "23:30"
RANDOM_DELAY_MINUTES = 10
RANDOM_DELAY_SECONDS = 600
SCENE_PREFIX = "scene."
GROUP_PREFIX = "group."
TIME_STATE_ENTITY = "irisone.time_state"
SUN_ELEVATION_SENSOR = "sensor.sun_solar_elevation"
SUN_RISING_SENSOR = "sensor.sun_solar_rising"


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

    def initialize(self) -> None:
        """
        Initialize the automatic lights app.

        Sets up configuration, initializes state tracking, and registers
        event listeners for sun position changes and manual scene activation.

        This method performs the following initialization steps:
        1. Validates and sets up time-based configuration
        2. Validates solar radiation configuration (if provided)
        3. Initializes state tracking and group caching
        4. Registers event listeners for sensor changes
        5. Sets up scheduled daily events

        Raises:
            ValueError: If critical configuration is invalid
        """
        self.log("Initializing AutomaticLights app...")

        # Initialize time-based configuration
        self._initialize_time_config()

        # Initialize solar radiation configuration
        self._initialize_solar_radiation_config()

        # Initialize state tracking and caching
        self._initialize_state_and_cache()

        # Register event listeners
        self._register_event_listeners()

        # Set up scheduled events
        self._setup_scheduled_events()

        self.log("AutomaticLights initialized: morning={}, night={}, solar_radiation={}, scenes={}".format(
            self.morning_start, self.night_start,
            "enabled" if self.solar_radiation else "disabled",
            len(self.scenes)
        ))

    def _initialize_time_config(self) -> None:
        """
        Initialize and validate time-based configuration.

        Sets up morning_start and night_start times with validation
        and fallback to default values if invalid.
        """
        try:
            self.morning_start = str(self.args.get("morning_start", DEFAULT_MORNING_START))
            self.night_start = str(self.args.get("night_start", DEFAULT_NIGHT_START))

            if not self.morning_start or self.morning_start == "None":
                self.log("ERROR: morning_start is required, format: HH:MM - using default {}".format(DEFAULT_MORNING_START))
                self.morning_start = DEFAULT_MORNING_START

            if not self.night_start or self.night_start == "None":
                self.log("ERROR: night_start is required, format: HH:MM - using default {}".format(DEFAULT_NIGHT_START))
                self.night_start = DEFAULT_NIGHT_START

            # Validate time format by attempting to parse
            self.parse_time(self.morning_start)
            self.parse_time(self.night_start)

            self.log("Time configuration validated: morning_start={}, night_start={}".format(
                self.morning_start, self.night_start
            ))

        except Exception as e:
            self.log("ERROR: Failed to initialize time configuration at line {}: {}".format(
                traceback.extract_stack()[-1].lineno, e
            ))
            raise ValueError("Invalid time configuration") from e

    def _initialize_solar_radiation_config(self) -> None:
        """
        Initialize and validate solar radiation configuration.

        Validates the solar_radiation configuration if provided,
        including sensor existence, threshold type, and required keys.
        """
        self.solar_radiation = self.args.get("solar_radiation")

        if not self.solar_radiation:
            self.log("Solar radiation monitoring disabled - using time and sun position only")
            return

        try:
            # Validate solar radiation configuration structure
            if not isinstance(self.solar_radiation, dict):
                self.log("ERROR: solar_radiation must be a dictionary - disabling solar radiation monitoring")
                self.solar_radiation = None
                return

            if "sensor" not in self.solar_radiation or "threshold" not in self.solar_radiation:
                self.log("ERROR: solar_radiation must contain 'sensor' and 'threshold' keys - disabling solar radiation monitoring")
                self.solar_radiation = None
                return

            # Validate threshold is numeric
            threshold = self.solar_radiation.get("threshold")
            if threshold is None:
                self.log("ERROR: solar_radiation threshold cannot be None - disabling solar radiation monitoring")
                self.solar_radiation = None
                return

            try:
                float(threshold)
            except (ValueError, TypeError):
                self.log("ERROR: solar_radiation threshold must be a numeric value - disabling solar radiation monitoring")
                self.solar_radiation = None
                return

            self.log("Solar radiation monitoring enabled: sensor={}, threshold={}, elevation_threshold={}".format(
                self.solar_radiation.get("sensor"),
                self.solar_radiation.get("threshold"),
                self.solar_radiation.get("elevation_threshold", DEFAULT_ELEVATION_THRESHOLD)
            ))

        except Exception as e:
            self.log("ERROR: Failed to initialize solar radiation configuration at line {}: {}".format(
                traceback.extract_stack()[-1].lineno, e
            ))
            self.solar_radiation = None

    def _initialize_state_and_cache(self) -> None:
        """
        Initialize state tracking and group caching.

        Sets up the current state based on time calculation and
        initializes the group cache for efficient entity management.
        """
        try:
            # Initialize state tracking (only called on startup)
            self.current_state = self.calculate_state()

            # Initialize group cache with timeout
            self.groups: Dict[str, List[str]] = {}
            self.groups_cache_time: Optional[datetime] = None
            self.get_groups()

            # Get Scenes
            self.scenes = self.args.get("scenes", {})

            self.log("State and cache initialized: current_state={}, groups={}, scenes={}".format(
                self.current_state, len(self.groups), len(self.scenes)
            ))

        except Exception as e:
            self.log("ERROR: Failed to initialize state and cache at line {}: {}".format(
                traceback.extract_stack()[-1].lineno, e
            ))
            raise

    def _register_event_listeners(self) -> None:
        """
        Register event listeners for sensor changes and manual scene activation.

        Sets up listeners for:
        - Sun position changes (solar elevation sensor)
        - Manual scene activation events
        """
        try:
            # Sun position events
            self.listen_state(self.sun_pos, SUN_ELEVATION_SENSOR)
            self.log("Registered listener for sun position changes: {}".format(SUN_ELEVATION_SENSOR))

            # Manual scene events
            self.listen_event(self.manual_scene, event='call_service', domain='scene')
            self.log("Registered listener for manual scene activation")

        except Exception as e:
            self.log("ERROR: Failed to register event listeners at line {}: {}".format(
                traceback.extract_stack()[-1].lineno, e
            ))
            raise

    def _setup_scheduled_events(self) -> None:
        """
        Set up scheduled daily events for time-based transitions.

        Configures daily scheduled events for morning and night scene activation
        with randomized delays to prevent simultaneous execution.
        """
        try:
            # Time based events with randomized delays
            self.run_daily(self.start_morning, self.morning_start, random_start=-45*60, random_end=-30*60)
            self.run_daily(self.start_night, self.night_start, random_start=-15*60, random_end=10*60)

            self.log("Scheduled events: morning={}, night={}, sunrise={}, sunset={}, state={}".format(
                self.morning_start, self.night_start,
                self.sunrise().time(), self.sunset().time(), self.current_state
            ))

        except Exception as e:
            self.log("ERROR: Failed to setup scheduled events at line {}: {}".format(
                traceback.extract_stack()[-1].lineno, e
            ))
            raise

    def manual_scene(self, event_name: str, data: Dict[str, Any], kwargs: Dict[str, Any]) -> None:
        """
        Handle manual scene activation events from Home Assistant.

        This method is triggered when a scene is manually activated through
        Home Assistant's scene.turn_on service. It extracts the scene name
        from the event data and activates the corresponding scene immediately.

        Args:
            event_name: Name of the event (typically 'call_service')
            data: Event data containing service information including entity_id
            kwargs: Additional keyword arguments (unused)

        Example:
            When scene.turn_on is called with entity_id: scene.morning,
            this method will activate the 'morning' scene configuration.
        """
        try:
            self.log("Manual scene activation event received: event_name={}".format(event_name))

            # Extract scene name from service data
            service_data = data.get("service_data", {})
            scene_entity = service_data.get("entity_id")

            if scene_entity is None:
                self.log("ERROR: No entity_id found in manual scene event data")
                return

            # Handle both single entity and list of entities
            if isinstance(scene_entity, list):
                scene_entities = scene_entity
            else:
                scene_entities = [scene_entity]

            # Process each scene entity
            for scene_entity in scene_entities:
                try:
                    # Remove 'scene.' prefix to get scene name
                    if not scene_entity.startswith(SCENE_PREFIX):
                        self.log("WARNING: Invalid scene entity_id format: {}".format(scene_entity))
                        continue

                    scene_name = scene_entity.replace(SCENE_PREFIX, "")

                    if not scene_name:
                        self.log("ERROR: Invalid scene entity_id: {}".format(scene_entity))
                        continue

                    # Validate scene exists in configuration
                    if scene_name not in self.scenes:
                        self.log("ERROR: Scene '{}' not found in configuration. Available: {}".format(
                            scene_name, list(self.scenes.keys())
                        ))
                        continue

                    self.log("Activating manual scene: {} (from entity: {})".format(scene_name, scene_entity))
                    self.activate_scene(scene_name, run_now=True)

                except Exception as e:
                    self.log("ERROR: Failed to process scene entity '{}' at line {}: {}".format(
                        scene_entity, traceback.extract_stack()[-1].lineno, e
                    ))
                    continue

        except Exception as e:
            self.log("ERROR: Failed to handle manual scene event at line {}: {}".format(
                traceback.extract_stack()[-1].lineno, e
            ))

    def get_groups(self) -> None:
        """
        Retrieve and cache all Home Assistant groups.

        This method fetches all available groups from Home Assistant and caches
        them locally for efficient access. The cache includes group names mapped
        to their entity lists and expires after 6 hours to ensure data freshness.

        The cached groups are used by activate_scene() to control individual
        entities within each group based on scene configurations.

        Cache Structure:
            self.groups: Dict[str, List[str]] - Maps group entity IDs to entity lists
            self.groups_cache_time: datetime - Timestamp of last cache update
        """
        try:
            self.log("Retrieving and caching Home Assistant groups...")

            # Get all groups from Home Assistant
            state_groups = self._get_safe_state("group", log_name="Groups")
            if state_groups is None or not isinstance(state_groups, dict):
                self.log("ERROR: Failed to retrieve groups from Home Assistant or invalid format")
                return

            # Process and cache group data
            self.groups = {}
            group_count = 0
            entity_count = 0

            for group_id, group_data in state_groups.items():
                try:
                    # Extract entity list from group attributes
                    entities = group_data.get("attributes", {}).get("entity_id", [])

                    if not isinstance(entities, list):
                        self.log("WARNING: Invalid entity list for group {}: {}".format(group_id, entities))
                        continue

                    self.groups[group_id] = entities
                    group_count += 1
                    entity_count += len(entities)

                except Exception as e:
                    self.log("ERROR: Failed to process group {} at line {}: {}".format(
                        group_id, traceback.extract_stack()[-1].lineno, e
                    ))
                    continue

            # Update cache timestamp
            self.groups_cache_time = datetime.now()

            self.log("Group cache updated: {} groups, {} total entities".format(group_count, entity_count))

        except Exception as e:
            self.log("ERROR: Failed to get groups at line {}: {}".format(
                traceback.extract_stack()[-1].lineno, e
            ))

    def sun_pos(self, entity: str, attribute: str, old: str, new: str, kwargs: Dict[str, Any]) -> None:
        """
        Handle sun position state changes and manage runtime state transitions.

        This method is the core logic for all sensor-based state transitions.
        It monitors solar elevation and rising status to trigger intelligent
        transitions between lighting modes based on environmental conditions.

        The method handles two modes of operation:
        1. Solar radiation enabled: Uses light level sensors for precise transitions
        2. Solar radiation disabled: Uses only elevation and rising status

        State Transition Logic:
        - Morning → Day: When sun is rising AND elevation > threshold AND light level > threshold
        - Day → Evening: When sun is not rising AND (light level < threshold OR elevation < threshold)

        Args:
            entity: The entity that triggered the state change (sensor.sun_solar_elevation)
            attribute: The attribute that changed (typically 'state')
            old: Previous value of the attribute
            new: New value of the attribute
            kwargs: Additional keyword arguments (unused)
        """
        try:
            self.log("Sun position change detected: entity={}, attribute={}, old={}, new={}".format(
                entity, attribute, old, new
            ))

            # Get current sensor values
            sensor_data = self._get_sensor_data()
            if sensor_data is None:
                return

            current_elevation, is_rising = sensor_data

            # Process state transitions based on solar radiation configuration
            if self.solar_radiation:
                self._process_solar_radiation_transitions(current_elevation, is_rising)
            else:
                self._process_elevation_only_transitions(current_elevation, is_rising)

        except Exception as e:
            self.log("ERROR: Failed to process sun position change at line {}: {}".format(
                traceback.extract_stack()[-1].lineno, e
            ))

    def _get_sensor_data(self) -> Optional[tuple[float, bool]]:
        """
        Retrieve and validate sensor data for sun position calculations.

        Returns:
            Tuple of (elevation, is_rising) or None if sensors are unavailable
        """
        try:
            # Check solar elevation sensor
            elevation_state = self._get_safe_state(SUN_ELEVATION_SENSOR, log_name="Solar elevation sensor")
            if elevation_state is None or not isinstance(elevation_state, str):
                return None

            # Check sun rising sensor
            rising_state = self._get_safe_state(SUN_RISING_SENSOR, log_name="Sun rising sensor")
            if rising_state is None:
                return None

            # Convert sensor values to appropriate types
            current_elevation = float(elevation_state)
            is_rising = rising_state is True

            self.log("Sensor data: elevation={}, rising={}, state={}".format(
                current_elevation, is_rising, self.current_state
            ))

            return current_elevation, is_rising

        except (ValueError, TypeError) as e:
            self.log("ERROR: Failed to convert sensor values to float at line {}: {}".format(
                traceback.extract_stack()[-1].lineno, e
            ))
            return None

    def _process_solar_radiation_transitions(self, current_elevation: float, is_rising: bool) -> None:
        """
        Process state transitions when solar radiation monitoring is enabled.

        Args:
            current_elevation: Current solar elevation in degrees
            is_rising: Whether the sun is currently rising
        """
        try:
            # Validate solar radiation configuration is available
            if not self.solar_radiation:
                self.log("ERROR: Solar radiation configuration is None")
                return

            # Get light level sensor data
            light_state = self._get_safe_state(
                self.solar_radiation.get("sensor"),
                attribute="state",
                log_name="Light level sensor"
            )
            if light_state is None or not isinstance(light_state, str):
                return

            light_level = float(light_state)
            threshold = self.solar_radiation.get("threshold")
            elevation_threshold = self.solar_radiation.get("elevation_threshold", DEFAULT_ELEVATION_THRESHOLD)

            self.log("Solar radiation: light={}, threshold={}, elevation_threshold={}".format(
                light_level, threshold, elevation_threshold
            ))

            # Morning to Day transition
            if (self.current_state == "morning" and
                is_rising and
                current_elevation > elevation_threshold and
                light_level > threshold):

                self.log("Transitioning morning -> day (light_level {} > threshold {}, elevation {} > {})".format(
                    light_level, threshold, current_elevation, elevation_threshold
                ))
                self.start_day(None)

            # Day to Evening transition
            elif self.current_state == "day" and not is_rising:
                if light_level < threshold:
                    self.log("Transitioning day -> evening (light_level {} < threshold {})".format(
                        light_level, threshold
                    ))
                    self.start_evening(None)
                elif current_elevation < elevation_threshold:
                    self.log("Transitioning day -> evening (elevation {} < {})".format(
                        current_elevation, elevation_threshold
                    ))
                    self.start_evening(None)
                else:
                    self.log("Day -> evening transition skipped: light_level {} >= threshold {}, elevation {} >= {}".format(
                        light_level, threshold, current_elevation, elevation_threshold
                    ))

        except Exception as e:
            self.log("ERROR: Failed to process solar radiation transitions at line {}: {}".format(
                traceback.extract_stack()[-1].lineno, e
            ))

    def _process_elevation_only_transitions(self, current_elevation: float, is_rising: bool) -> None:
        """
        Process state transitions when solar radiation monitoring is disabled.

        Args:
            current_elevation: Current solar elevation in degrees
            is_rising: Whether the sun is currently rising
        """
        try:
            elevation_threshold = DEFAULT_ELEVATION_THRESHOLD

            # Morning to Day transition
            if (self.current_state == "morning" and
                is_rising and
                current_elevation > elevation_threshold):

                self.log("Transitioning morning -> day (elevation {} > {}, rising)".format(
                    current_elevation, elevation_threshold
                ))
                self.start_day(None)

            # Day to Evening transition
            elif (self.current_state == "day" and
                  not is_rising and
                  current_elevation < elevation_threshold):

                self.log("Transitioning day -> evening (not rising, elevation {} < {})".format(
                    current_elevation, elevation_threshold
                ))
                self.start_evening(None)
            else:
                self.log("Day -> evening transition skipped: elevation {} >= {} or sun is rising".format(
                    current_elevation, elevation_threshold
                ))

        except Exception as e:
            self.log("ERROR: Failed to process elevation-only transitions at line {}: {}".format(
                traceback.extract_stack()[-1].lineno, e
            ))

    def calculate_state(self) -> str:
        """
        Calculate the initial time-based state on startup.

        This method determines the initial lighting state based on current time
        relative to sunrise, sunset, and configured start times. It is only
        called during initialization to set the starting state. All subsequent
        state transitions are handled by sensor-based logic in sun_pos().

        State Calculation Logic:
        1. Night: Before sunrise and before morning start time
        2. Morning: After morning start but before sunrise
        3. Day: After sunrise but before sunset
        4. Evening: After sunset but before night start time
        5. Night: After night start time

        Returns:
            str: Initial state ('night', 'morning', 'day', or 'evening')

        Raises:
            ValueError: If time parsing fails
        """
        try:
            self.log("Calculating initial state based on current time...")

            now = self.time()
            sunrise = self.sunrise().time()
            sunset = self.sunset().time()

            # Parse configured times with validation
            try:
                morning_start = self.parse_time(self.morning_start)
            except Exception as e:
                self.log("ERROR: Invalid morning_start time format '{}': {} - using default {}".format(
                    self.morning_start, e, DEFAULT_MORNING_START
                ))
                morning_start = self.parse_time(DEFAULT_MORNING_START)  # Default fallback

            try:
                night_start = self.parse_time(self.night_start)
            except Exception as e:
                self.log("ERROR: Invalid night_start time format '{}': {} - using default {}".format(
                    self.night_start, e, DEFAULT_NIGHT_START
                ))
                night_start = self.parse_time(DEFAULT_NIGHT_START)  # Default fallback

            # Determine state based on time relationships
            if now <= sunrise and now <= morning_start:
                initial_state = "night"
            elif now > morning_start and now < sunrise:
                initial_state = "morning"
            elif now >= sunrise and now < sunset:
                initial_state = "day"
            elif now >= sunset and now < night_start:
                initial_state = "evening"
            elif now >= night_start:
                initial_state = "night"
            else:
                # Fallback - should never reach here
                initial_state = "night"

            self.log("Initial state: {} (now={}, sunrise={}, sunset={}, morning={}, night={})".format(
                initial_state, now, sunrise, sunset, morning_start, night_start
            ))

            return initial_state

        except Exception as e:
            self.log("ERROR: Failed to calculate initial state at line {}: {}".format(
                traceback.extract_stack()[-1].lineno, e
            ))
            return "night"  # Safe fallback

    def start_morning(self, _kwargs: Optional[Dict[str, Any]]) -> None:
        """
        Start the morning scene with validation.

        Activates morning lighting if the sun is not already up and current
        state is not already day. This method includes validation to prevent
        unnecessary scene activation during daylight hours.

        Args:
            _kwargs: Additional keyword arguments (unused)
        """
        try:
            self.log("Morning scene activation requested")

            def should_skip() -> bool:
                """Check if morning scene should be skipped."""
                try:
                    sun_state = self.sun_up()
                    should_skip = sun_state is None or sun_state or self.current_state == "day"
                    if should_skip:
                        self.log("Morning scene skipped: sun_state={}, current_state={}".format(
                            sun_state, self.current_state
                        ))
                    return should_skip
                except Exception as e:
                    self.log("ERROR: Failed to check sun state at line {}: {}".format(
                        traceback.extract_stack()[-1].lineno, e
                    ))
                    return False

            self._start_scene("morning", should_skip, "Sun is already up or already in day state, no morning needed")

        except Exception as e:
            self.log("ERROR: Failed to start morning scene at line {}: {}".format(
                traceback.extract_stack()[-1].lineno, e
            ))

    def start_day(self, _kwargs: Optional[Dict[str, Any]]) -> None:
        """
        Start the day scene.

        Activates day lighting configuration without additional validation.
        This scene is typically triggered by sensor-based transitions when
        sufficient light levels are detected.

        Args:
            _kwargs: Additional keyword arguments (unused)
        """
        try:
            self.log("Day scene activation requested")
            self._start_scene("day")

        except Exception as e:
            self.log("ERROR: Failed to start day scene at line {}: {}".format(
                traceback.extract_stack()[-1].lineno, e
            ))

    def start_evening(self, _kwargs: Optional[Dict[str, Any]]) -> None:
        """
        Start the evening scene with validation.

        Activates evening lighting if not already in night state. This method
        includes validation to prevent evening scene activation during night hours.

        Args:
            _kwargs: Additional keyword arguments (unused)
        """
        try:
            self.log("Evening scene activation requested")

            def should_skip() -> bool:
                """Check if evening scene should be skipped."""
                should_skip = self.current_state == "night"
                if should_skip:
                    self.log("Evening scene skipped: current_state={}".format(self.current_state))
                return should_skip

            self._start_scene("evening", should_skip, "Already in night state, no evening needed")

        except Exception as e:
            self.log("ERROR: Failed to start evening scene at line {}: {}".format(
                traceback.extract_stack()[-1].lineno, e
            ))

    def start_night(self, _kwargs: Optional[Dict[str, Any]]) -> None:
        """
        Start the night scene.

        Activates night lighting configuration without additional validation.
        This scene is typically triggered by scheduled time-based events.

        Args:
            _kwargs: Additional keyword arguments (unused)
        """
        try:
            self.log("Night scene activation requested")
            self._start_scene("night")

        except Exception as e:
            self.log("ERROR: Failed to start night scene at line {}: {}".format(
                traceback.extract_stack()[-1].lineno, e
            ))

    def activate_scene(self, scene_name: str, run_now: bool = False) -> None:
        """
        Activate a specific scene by controlling group entities.

        This method is the core scene activation logic that updates the current
        state and controls all entities in groups associated with the scene
        based on their configured states. It includes comprehensive validation,
        caching, and error handling.

        The method performs the following steps:
        1. Validates the scene exists in configuration
        2. Updates current state and Home Assistant entity
        3. Refreshes group cache if expired (6 hours)
        4. Iterates through scene groups and controls individual entities
        5. Executes either immediately or with delay based on run_now parameter

        Args:
            scene_name: Name of the scene to activate
            run_now: If True, execute immediately instead of with delay

        Raises:
            ValueError: If scene configuration is invalid
        """
        try:
            self.log("Scene activation requested: scene={}, run_now={}".format(scene_name, run_now))

            # Validate scene exists in configuration
            if scene_name not in self.scenes:
                self.log("ERROR: Scene '{}' not found in configuration. Available scenes: {}".format(
                    scene_name, list(self.scenes.keys())
                ))
                return

            # Update current state and Home Assistant entity
            self.current_state = scene_name
            self.set_state(TIME_STATE_ENTITY, state=scene_name)
            self.log("State updated: current_state={}".format(scene_name))

            # Refresh groups if cache is empty or expired (6 hours)
            self._refresh_group_cache_if_needed()

            # Process scene configuration
            scene_config = self.scenes.get(scene_name, {})
            if not scene_config:
                self.log("WARNING: Scene '{}' has no configuration".format(scene_name))
                return

            self._process_scene_configuration(scene_name, scene_config, run_now)

        except Exception as e:
            self.log("ERROR: Failed to activate scene '{}' at line {}: {}".format(
                scene_name, traceback.extract_stack()[-1].lineno, e
            ))

    def _refresh_group_cache_if_needed(self) -> None:
        """
        Refresh the group cache if it's empty or expired.

        The cache expires after 6 hours to ensure data freshness while
        minimizing API calls to Home Assistant.
        """
        try:
            if not self.groups or not self.groups_cache_time:
                self.log("Group cache is empty or missing timestamp, refreshing...")
                self.get_groups()
            else:
                # Calculate cache age using datetime objects
                now = datetime.now()
                cache_age = (now - self.groups_cache_time).total_seconds()
                if cache_age > CACHE_EXPIRY_HOURS * 3600:  # Convert hours to seconds
                    self.log("Group cache expired (age: {:.1f} hours), refreshing...".format(cache_age / 3600))
                    self.get_groups()
                else:
                    self.log("Group cache is fresh (age: {:.1f} hours)".format(cache_age / 3600))

        except Exception as e:
            self.log("ERROR: Failed to refresh group cache at line {}: {}".format(
                traceback.extract_stack()[-1].lineno, e
            ))

    def _process_scene_configuration(self, scene_name: str, scene_config: Dict[str, bool], run_now: bool) -> None:
        """
        Process scene configuration and control individual entities.

        Args:
            scene_name: Name of the scene being activated
            scene_config: Dictionary mapping group names to boolean states
            run_now: Whether to execute immediately or with delay
        """
        try:
            entity_count = 0
            success_count = 0

            for group_name, group_state in scene_config.items():
                try:
                    # Validate group_state is a boolean
                    if not isinstance(group_state, bool):
                        self.log("ERROR: Invalid group_state for group '{}' in scene '{}': {} (must be boolean)".format(
                            group_name, scene_name, group_state
                        ))
                        continue

                    # Get entities for this group
                    group_entity_id = "{}{}".format(GROUP_PREFIX, group_name)
                    entities = self.groups.get(group_entity_id)

                    if entities is None:
                        self.log("ERROR: No entities found for group '{}' in scene '{}'".format(
                            group_name, scene_name
                        ))
                        continue

                    # Control each entity in the group
                    for entity in entities:
                        try:
                            entity_count += 1

                            if run_now:
                                self._turn_onoff({"entity": entity, "state": group_state})
                            else:
                                self.run_in(
                                    self._turn_onoff,
                                    0,
                                    random_start=0,
                                    random_end=RANDOM_DELAY_SECONDS,
                                    entity=entity,
                                    state=group_state
                                )
                            success_count += 1

                        except Exception as e:
                            self.log("ERROR: Failed to control entity '{}' at line {}: {}".format(
                                entity, traceback.extract_stack()[-1].lineno, e
                            ))

                except Exception as e:
                    self.log("ERROR: Failed to process group '{}' at line {}: {}".format(
                        group_name, traceback.extract_stack()[-1].lineno, e
                    ))
                    continue

            self.log("Scene '{}' activation completed: {}/{} entities controlled successfully".format(
                scene_name, success_count, entity_count
            ))

        except Exception as e:
            self.log("ERROR: Failed to process scene configuration at line {}: {}".format(
                traceback.extract_stack()[-1].lineno, e
            ))

    def _turn_onoff(self, kwargs: Dict[str, Any]) -> None:
        """
        Turn an entity on or off based on the specified state.

        This method is called either immediately or with a delay to control
        individual Home Assistant entities. It includes validation, error
        handling, and retry mechanism for failed operations.

        Args:
            kwargs: Dictionary containing 'entity' and 'state' keys
                - entity: Entity ID to control
                - state: Boolean indicating on (True) or off (False)
        """
        try:
            entity = kwargs.get("entity")
            state = kwargs.get("state")

            if entity is None or state is None:
                self.log("ERROR: Missing required parameters in _turn_onoff: entity={}, state={}".format(
                    entity, state
                ))
                return

            if not isinstance(state, bool):
                self.log("ERROR: Invalid state type in _turn_onoff: entity={}, state={} (must be boolean)".format(
                    entity, state
                ))
                return

            # Control the entity with retry mechanism
            for attempt in range(MAX_RETRIES):
                try:
                    if state:
                        self.turn_on(entity)
                    else:
                        self.turn_off(entity)
                    break  # Success, exit retry loop

                except Exception as e:
                    if attempt < MAX_RETRIES - 1:
                        self.log("WARNING: Failed to control entity '{}' (attempt {}/{}), retrying...".format(
                            entity, attempt + 1, MAX_RETRIES
                        ))
                        # Small delay before retry
                        time.sleep(RETRY_DELAY)
                    else:
                        raise  # Re-raise on final attempt

        except Exception as e:
            self.log("ERROR: Failed to control entity '{}' after retries at line {}: {}".format(
                entity, traceback.extract_stack()[-1].lineno, e
            ))

    def _get_safe_state(self, entity: str, attribute: Optional[str] = None, log_name: Optional[str] = None) -> Optional[Union[str, Dict[str, Any]]]:
        """
        Safely get state from Home Assistant entity with comprehensive error handling.

        This method provides a safe wrapper around Home Assistant's get_state
        method, handling unavailable entities and network errors gracefully.

        Args:
            entity: Entity ID to query
            attribute: Optional attribute to get (defaults to 'state')
            log_name: Name for logging if entity is unavailable (defaults to entity ID)

        Returns:
            State value as string or dictionary, or None if entity is unavailable or error occurs
        """
        try:
            state = self.get_state(entity, attribute=attribute)

            if state is None or state == "unavailable":
                log_msg = log_name or entity
                self.log("Entity unavailable: {} = {}".format(log_msg, state))
                return None

            return state

        except Exception as e:
            log_msg = log_name or entity
            self.log("ERROR: Failed to get state for '{}' at line {}: {}".format(
                log_msg, traceback.extract_stack()[-1].lineno, e
            ))
            return None

    def _start_scene(self, scene_name: str, validation_func: Optional[Callable[[], bool]] = None, skip_message: Optional[str] = None) -> None:
        """
        Generic scene starter with optional validation.

        This method provides a common interface for starting scenes with
        optional validation logic. It handles the validation, logging, and
        scene activation in a consistent manner.

        Args:
            scene_name: Name of the scene to activate
            validation_func: Optional function that returns True to skip activation
            skip_message: Message to log if validation fails and scene is skipped
        """
        try:
            # Run validation if provided
            if validation_func is not None:
                try:
                    should_skip = validation_func()
                    if should_skip:
                        if skip_message:
                            self.log(skip_message)
                        else:
                            self.log("Scene '{}' activation skipped by validation function".format(scene_name))
                        return
                except Exception as e:
                    self.log("ERROR: Validation function failed for scene '{}' at line {}: {}".format(
                        scene_name, traceback.extract_stack()[-1].lineno, e
                    ))
                    # Continue with scene activation even if validation fails

            # Activate the scene
            self.log("Starting scene: {}".format(scene_name))
            self.activate_scene(scene_name)

        except Exception as e:
            self.log("ERROR: Failed to start scene '{}' at line {}: {}".format(
                scene_name, traceback.extract_stack()[-1].lineno, e
            ))

