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

        # Add solar radiation configuration
        self.solar_radiation = self.args.get("solar_radiation")
        if not self.solar_radiation:
            self.log("solar_radiation is required")
            exit(1)

        self.current_state = self.calculate_state()

        # Enumerate groups
        self.groups = {}
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

        self.activate_scene(scene.replace("scene.", ""))

    def get_groups(self):
        """
        Retrieve and cache all Home Assistant groups.

        Populates self.groups with group names mapped to their entity lists.
        """
        self.log("get_groups()")
        state_groups = self.get_state("group")
        self.log(" * state_groups = {}".format(state_groups))
        self.groups = {}
        for key, val in state_groups.items():
            self.groups[key] = val.get("attributes", {}).get("entity_id", [])
            self.log(" - * group '{}' = '{}'".format(key, self.groups[key]))

    def sun_pos(self, entity, attribute, old, new, kwargs):
        """
        Handle sun position state changes.

        Monitors sun elevation and rising status to trigger day/evening transitions
        based on solar radiation levels.

        Args:
            entity: The sun entity
            attribute: Attribute that changed
            old: Previous value
            new: New value
            kwargs: Additional keyword arguments
        """
        self.log("sun_pos(self, {}, {}, {} -> {}, kwargs)".format(entity, json.dumps(attribute), old, new))
        current_elevation = float(self.get_state("sensor.sun_solar_elevation"))
        is_rising = self.get_state("sun.sun", attribute="rising")
        light_level = float(self.get_state(self.solar_radiation.get("sensor"), attribute="state"))
        self.log("sun_pos() current_elevation={} is_rising={} light_level={}".format(current_elevation, is_rising, light_level))

        # if it's morning and the has risen fully, it's day
        if self.current_state == "morning" and is_rising and current_elevation > 3:
            if light_level > self.solar_radiation.get("threshold"):
                self.start_day(None)

        elif self.current_state == "day" and not is_rising:
            if light_level < self.solar_radiation.get("threshold") or current_elevation < 3:
                self.start_evening(None)

    def calculate_state(self):
        """
        Calculate the current time-based state.

        Determines if it's night, morning, day, or evening based on current time
        relative to sunrise, sunset, and configured start times.

        Returns:
            str: Current state ('night', 'morning', 'day', or 'evening')
        """
        now = self.time()
        sunrise = self.sunrise().time()
        morning_start = self.parse_time(self.morning_start)
        sunset = self.sunset().time()
        night_start = self.parse_time(self.night_start)

        # if we're before sunrise and before morning it's night
        if now < sunrise and now < morning_start:
            return "night"
        # if we're after morning but before sunrise it's morning
        if now > morning_start and now < sunrise:
            return "morning"
        # if we're after sunrise but before sunset it's day
        if now > sunrise and now < sunset:
            return "day"
        # if it's after sunset but before night it's evening
        if now > sunset and now < night_start:
            return "evening"
        # if it's after night, it's night *doh*
        if now > night_start:
            return "night"

    def start_morning(self, kwargs):
        """
        Start the morning scene.

        Activates morning lighting if the sun is not already up and current
        state is not already day.

        Args:
            kwargs: Additional keyword arguments
        """
        if self.sun_up() or self.current_state == "day":
            self.log("Sun is already up, no morning needed")
            return
        self.log("morning_start()")
        self.activate_scene("morning")

    def start_day(self, kwargs):
        """
        Start the day scene.

        Activates day lighting configuration.

        Args:
            kwargs: Additional keyword arguments
        """
        self.log("day_start()")
        self.activate_scene("day")

    def start_evening(self, kwargs):
        """
        Start the evening scene.

        Activates evening lighting if not already in night state.

        Args:
            kwargs: Additional keyword arguments
        """
        if self.current_state == "night":
            self.log("last state was night, no evening needed")
            return
        self.log("evening_start()")
        self.activate_scene("evening")

    def start_night(self, kwargs):
        """
        Start the night scene.

        Activates night lighting configuration.

        Args:
            kwargs: Additional keyword arguments
        """
        self.log("night_start()")
        self.activate_scene("night")

    def activate_scene(self, scene_name):
        """
        Activate a specific scene by controlling group entities.

        Updates the current state and controls all entities in groups
        associated with the scene based on their configured states.

        Args:
            scene_name: Name of the scene to activate
        """
        self.log("activate_scene({})".format(scene_name))
        self.current_state = scene_name
        self.set_state("irisone.time_state", state=scene_name)

        self.get_groups()
        for group_name in self.scenes.get(scene_name, {}):
            group_state = self.scenes.get(scene_name, {}).get(group_name)
            self.log("name: {} state: {}".format(group_name, group_state))
            entities = self.groups.get("group.{}".format(group_name))
            self.log("entities: {}".format(json.dumps(entities)))
            if entities is None:
                self.log("ERROR: No entities for group {} and scene {}".format(group_name, scene_name))
                return
            for entity in entities:
                self.log("Turning entity {}".format(entity))
                self.run_in(self._turn_onoff, 0, random_start=0, random_end=600, entity=entity, state=group_state)

    def _turn_onoff(self, kwargs):
        """
        Turn an entity on or off based on the specified state.

        Args:
            kwargs: Dictionary containing 'entity' and 'state' keys
        """
        entity = kwargs.get("entity")
        state = kwargs.get("state")
        self.log("_turn_onoff(self, kwargs): entity={} state={}".format(entity, state))
        if entity is None or state is None:
            return
        if state:
            self.turn_on(entity)
        elif not state:
            self.turn_off(entity)

