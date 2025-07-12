import json
import appdaemon.plugins.hass.hassapi as hass

class AutomaticLights(hass.Hass):
    """
    Home Assistant AppDaemon app for automatic lighting control based on time and sun position.

    This app manages lighting scenes throughout the day based on:
    - Time-based triggers (morning and night)
    - Sun position and solar radiation levels
    - Manual scene activation

    Configuration:
        morning_start: Time to start morning scene (format: HH:MM)
        night_start: Time to start night scene (format: HH:MM)
        solar_radiation: Dict with 'sensor' and 'threshold' keys
        scenes: Dict mapping scene names to group configurations
    """

    def initialize(self):
        """
        Initialize the automatic lights app.

        Sets up configuration, initializes state tracking, and registers
        event listeners for sun position changes and manual scene activation.
        """
        self.morning_start = str(self.args.get("morning_start", "05:30"))
        self.night_start = str(self.args.get("night_start", "23:30"))
        if not self.morning_start:
            self.log("morning_start is required, format: %H:%M:%S")
        if not self.night_start:
            self.log("night_start is required, format: %H:%M:%S")

        # Add solar radiation configuration (optional)
        self.solar_radiation = self.args.get("solar_radiation")
        if self.solar_radiation:
            self.log("Solar radiation monitoring enabled")
        else:
            self.log("Solar radiation monitoring disabled - using time and sun position only")

        # Initialize state tracking (only called on startup)
        self.current_state = self.calculate_state()

        # Initialize group cache with timeout
        self.groups = {}
        self.groups_cache_time = None
        self.get_groups()

        # Get Scenes
        self.scenes = self.args.get("scenes", {})

        # sun position events
        self.listen_state(self.sun_pos, "sensor.sun_solar_elevation")

        # manual scene events
        self.listen_event(self.manual_scene, event='call_service', domain='scene')

        # time based events
        self.run_daily(self.start_morning, self.morning_start, random_start=-45*60, random_end=-30*60)
        self.run_daily(self.start_night, self.night_start, random_start=-15*60, random_end=10*60)

        self.log(" >> AutomaticLights day:{} night:{} sunup: {} sundn: {} state: {}".format(self.morning_start, self.night_start, self.sunrise().time(), self.sunset().time(), self.current_state))

    def manual_scene(self, event_name, data, kwargs):
        """
        Handle manual scene activation events.

        Args:
            event_name: Name of the event
            data: Event data containing service information
            kwargs: Additional keyword arguments
        """
        self.log("manual_scene(self, {}, {}, {})".format(event_name, json.dumps(data), json.dumps(kwargs)))
        scene = data.get("service_data", {}).get("entity_id")
        if scene is None:
            return

        self.activate_scene(scene.replace("scene.", ""), True)

    def get_groups(self):
        """
        Retrieve and cache all Home Assistant groups.

        Populates self.groups with group names mapped to their entity lists.
        Cache expires after 6 hours.
        """
        self.log("get_groups()")
        state_groups = self._get_safe_state("group", log_name="Groups")
        if state_groups is None:
            return

        self.log(" * state_groups = {}".format(state_groups))
        self.groups = {}
        for key, val in state_groups.items():
            self.groups[key] = val.get("attributes", {}).get("entity_id", [])
            self.log(" - * group '{}' = '{}'".format(key, self.groups[key]))
        from datetime import datetime
        self.groups_cache_time = datetime.now()

    def sun_pos(self, entity, attribute, old, new, kwargs):
        """
        Handle sun position state changes and runtime state transitions.

        This method handles ALL runtime state transitions based on sensor data.
        The initial state is set by calculate_state() on startup, but all subsequent
        transitions (morning->day, day->evening, etc.) are controlled by this method.

        Monitors sun elevation and rising status to trigger transitions based on
        solar radiation levels and environmental conditions.

        Args:
            entity: The sun entity
            attribute: Attribute that changed
            old: Previous value
            new: New value
            kwargs: Additional keyword arguments
        """
        self.log("sun_pos(self, {}, {}, {} -> {}, kwargs)".format(entity, json.dumps(attribute), old, new))

        # Check solar elevation sensor
        elevation_state = self._get_safe_state("sensor.sun_solar_elevation", log_name="Solar elevation sensor")
        if elevation_state is None:
            return

        # Check sun rising attribute
        rising_state = self._get_safe_state("sun.sun", attribute="rising", log_name="Sun rising attribute")
        if rising_state is None:
            return

        try:
            current_elevation = float(elevation_state)
            is_rising = rising_state == "true"
            self.log("sun_pos() current_elevation={} is_rising={} current_state={}".format(current_elevation, is_rising, self.current_state))

            # Check if solar radiation monitoring is enabled
            if self.solar_radiation:
                light_state = self._get_safe_state(self.solar_radiation.get("sensor"), attribute="state", log_name="Light level sensor")
                if light_state is None:
                    return

                light_level = float(light_state)
                threshold = self.solar_radiation.get("threshold")
                self.log("sun_pos() light_level={} threshold={}".format(light_level, threshold))

                # if it's morning and the has risen fully, it's day
                if self.current_state == "morning" and is_rising and current_elevation > 3:
                    if light_level > threshold:
                        self.log("Transitioning morning -> day (light_level {} > threshold {})".format(light_level, threshold))
                        self.start_day(None)

                elif self.current_state == "day" and not is_rising:
                    if light_level < threshold:
                        self.log("Transitioning day -> evening (light_level {} < threshold {})".format(light_level, threshold))
                        self.start_evening(None)
                    elif current_elevation < 3:
                        self.log("Transitioning day -> evening (elevation {} < 3)".format(current_elevation))
                        self.start_evening(None)
            else:
                # Solar radiation disabled - use only elevation and rising status
                # if it's morning and the sun has risen above 3 degrees, it's day
                if self.current_state == "morning" and is_rising and current_elevation > 3:
                    self.log("Transitioning morning -> day (elevation {} > 3, rising)".format(current_elevation))
                    self.start_day(None)

                elif self.current_state == "day" and not is_rising:
                    self.log("Transitioning day -> evening (not rising, elevation {})".format(current_elevation))
                    self.start_evening(None)

        except (ValueError, TypeError) as e:
            import traceback
            self.log("Error converting sensor values to float at line {}: {}".format(traceback.extract_stack()[-1].lineno, e))

    def calculate_state(self):
        """
        Calculate the initial time-based state on startup only.

        Determines the initial state based on current time relative to sunrise,
        sunset, and configured start times. This is only called during initialization.
        All subsequent state transitions are handled by sensor-based logic.

        Returns:
            str: Initial state ('night', 'morning', 'day', or 'evening')
        """
        now = self.time()
        sunrise = self.sunrise().time()
        morning_start = self.parse_time(self.morning_start)
        sunset = self.sunset().time()
        night_start = self.parse_time(self.night_start)

        # if we're before sunrise and before morning it's night
        if now <= sunrise and now <= morning_start:
            return "night"
        # if we're after morning but before sunrise it's morning
        if now > morning_start and now < sunrise:
            return "morning"
        # if we're after sunrise but before sunset it's day
        if now >= sunrise and now < sunset:
            return "day"
        # if it's after sunset but before night it's evening
        if now >= sunset and now < night_start:
            return "evening"
        # if it's after night, it's night
        if now >= night_start:
            return "night"

        # Fallback - should never reach here
        return "night"

    def start_morning(self, _kwargs):
        """
        Start the morning scene.

        Activates morning lighting if the sun is not already up and current
        state is not already day.

        Args:
            kwargs: Additional keyword arguments
        """
        def should_skip():
            sun_state = self.sun_up()
            return sun_state is None or sun_state or self.current_state == "day"

        self._start_scene("morning", should_skip, "Sun is already up, no morning needed")

    def start_day(self, _kwargs):
        """
        Start the day scene.

        Activates day lighting configuration.

        Args:
            kwargs: Additional keyword arguments
        """
        self._start_scene("day")

    def start_evening(self, _kwargs):
        """
        Start the evening scene.

        Activates evening lighting if not already in night state.

        Args:
            kwargs: Additional keyword arguments
        """
        def should_skip():
            return self.current_state == "night"

        self._start_scene("evening", should_skip, "last state was night, no evening needed")

    def start_night(self, _kwargs):
        """
        Start the night scene.

        Activates night lighting configuration.

        Args:
            kwargs: Additional keyword arguments
        """
        self._start_scene("night")

    def activate_scene(self, scene_name, run_now=False):
        """
        Activate a specific scene by controlling group entities.

        Updates the current state and controls all entities in groups
        associated with the scene based on their configured states.

        Args:
            scene_name: Name of the scene to activate
            run_now: If True, execute immediately instead of with delay
        """
        try:
            self.log("activate_scene({})".format(scene_name))
            self.current_state = scene_name
            self.set_state("irisone.time_state", state=scene_name)

            # Refresh groups if cache is empty or expired (6 hours)
            if not self.groups or not self.groups_cache_time:
                self.get_groups()
            else:
                # Calculate cache age using datetime objects
                from datetime import datetime
                now = datetime.now()
                cache_age = (now - self.groups_cache_time).total_seconds()
                if cache_age > 6 * 3600:  # 6 hours in seconds
                    self.get_groups()
            scene_config = self.scenes.get(scene_name, {})
            for group_name in scene_config:
                group_state = scene_config.get(group_name)
                self.log("name: {} state: {}".format(group_name, group_state))

                entities = self.groups.get("group.{}".format(group_name))
                self.log("entities: {}".format(json.dumps(entities)))
                if entities is None:
                    self.log("ERROR: No entities for group {} and scene {}".format(group_name, scene_name))
                    continue

                for entity in entities:
                    self.log("Turning entity {}".format(entity))
                    if run_now:
                        self._turn_onoff({"entity": entity, "state": group_state})
                    else:
                        self.run_in(self._turn_onoff, 0, random_start=0, random_end=600, entity=entity, state=group_state)
        except Exception as e:
            import traceback
            self.log("Error in activate_scene at line {}: {}".format(traceback.extract_stack()[-1].lineno, e))

    def _turn_onoff(self, kwargs):
        """
        Turn an entity on or off based on the specified state.

        Args:
            kwargs: Dictionary containing 'entity' and 'state' keys
        """
        try:
            entity = kwargs.get("entity")
            state = kwargs.get("state")
            self.log("_turn_onoff(self, kwargs): entity={} state={}".format(entity, state))
            if entity is None or state is None:
                return
            if state:
                self.turn_on(entity)
            elif not state:
                self.turn_off(entity)
        except Exception as e:
            import traceback
            self.log("Error in _turn_onoff at line {}: {}".format(traceback.extract_stack()[-1].lineno, e))

    def _get_safe_state(self, entity, attribute=None, log_name=None):
        """
        Safely get state from Home Assistant entity.

        Args:
            entity: Entity ID to query
            attribute: Optional attribute to get
            log_name: Name for logging if unavailable

        Returns:
            State value or None if unavailable
        """
        try:
            state = self.get_state(entity, attribute=attribute)
            if state is None or state == "unavailable":
                log_msg = log_name or entity
                self.log("{} unavailable: {}".format(log_msg, state))
                return None
            return state
        except Exception as e:
            import traceback
            self.log("Error in _get_safe_state at line {}: {}".format(traceback.extract_stack()[-1].lineno, e))
            return None

    def _start_scene(self, scene_name, validation_func=None, skip_message=None):
        """
        Generic scene starter with optional validation.

        Args:
            scene_name: Name of the scene to activate
            validation_func: Optional function that returns True to skip
            skip_message: Message to log if validation fails
        """
        try:
            if validation_func and validation_func():
                if skip_message:
                    self.log(skip_message)
                return

            self.log("{}_start()".format(scene_name))
            self.activate_scene(scene_name)
        except Exception as e:
            import traceback
            self.log("Error in _start_scene at line {}: {}".format(traceback.extract_stack()[-1].lineno, e))

