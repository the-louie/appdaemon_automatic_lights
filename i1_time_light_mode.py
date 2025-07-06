import time
import json
import appdaemon.plugins.hass.hassapi as hass

class TimeLightMode(hass.Hass):

  def initialize(self):
    self.morning_start = str(self.args.get("morning_start", "05:30:00"))
    self.late_morning_start = str(self.args.get("late_morning_start", "07:30:00"))
    self.early_night_start = str(self.args.get("early_night_start", "21:30:00"))
    self.night_start = str(self.args.get("night_start", "23:30:00"))
    if not self.morning_start:
        self.log("morning_start is required, format: %H:%M:%S")
    if not self.night_start:
        self.log("night_start is required, format: %H:%M:%S")

    self.solarradiation = self.args.get("solar_radiation")
    if self.solarradiation is not None:
      self.listen_state(self.lightlevel_change, self.solarradiation.get("sensor"))

    self.current_state = self.calculate_state()

    # Enumerate groups
    self.groups = {}
    self.get_groups()

    # Get Scenes
    self.scenes = self.args.get("scenes", {})

    self.run_daily(self.start_morning, self.morning_start, random_start=-45*60, random_end=-30*60)
    self.run_daily(self.start_late_morning, self.late_morning_start)
    # Instead we use sun2 integration with a 1 degree threshold
    self.listen_state(self.sun_pos, 'binary_sensor.sun_is_up_proper')
    self.listen_event(self.manual_scene, event='call_service', domain='scene')
    #self.run_at_sunrise(self.start_day, random_start=15*60, random_end=30*60)
    #self.run_at_sunset(self.start_evening, random_start=-45*60, random_end=-30*60)
    self.run_daily(self.start_early_night, self.early_night_start)
    self.run_daily(self.start_night, self.night_start, random_start=-15*60, random_end=15*60)

    current_elevation = self.get_state("sun.sun", attribute="elevation")
    #next_sunupdn = self.get_state("binary_sensor.sun_is_up_proper", attribute="next_change")
    next_sunupdn = ''
    self.log(" >> TimeMode day:{} night:{} sunup: {} sundn: {} state: {} / ele: {} nxt: {}".format(self.morning_start, self.night_start, self.sunrise().time(), self.sunset().time(), self.current_state, current_elevation, next_sunupdn))

  def lightlevel_change(self, entity, attribute, old, new, kwargs):
    #self.log("lightlevel_change(self, {}, {}, {}, {}, kwargs)".format(entity, attribute, old, new))
    if entity != self.solarradiation.get("sensor"):
        return
    if new == old or new is None or new is 'unknown':
        return

    if self.current_state == "night" and float(new) >= 10:
        self.log("current_state: '{}' and {} > 10, activating 'morning'".format(self.current_state, new))
        self.activate_scene("morning")
    elif self.current_state == "day" and float(new) < 10:
        self.log("current_state: '{}' and {} < 10, activating 'evening'".format(self.current_state, new))
        self.activate_scene("evening")

  def manual_scene(self,  event_name, data, kwargs):
    self.log("manual_scene(self, {}, {}, {})".format(event_name, json.dumps(data),json.dumps(kwargs)))
    scene = data.get("service_data", {}).get("entity_id")
    if scene is None:
        return

    self.activate_scene(scene.replace("scene.", ""))

  def get_groups(self):
    self.log("get_groups()")
    state_groups = self.get_state("group")
    self.log(" * state_groups = {}".format(state_groups))
    self.groups = {}
    for key, val in state_groups.items():
      self.groups[key] = val.get("attributes", {}).get("entity_id", [])
      self.log(" - * group '{}' = '{}'".format(key, self.groups[key]))


  def sun_pos(self, entity, attribute, old, new, kwargs):
    self.log("sun_pos(self, {}, {}, {} -> {}, kwargs)".format(entity, json.dumps(attribute), old, new))
    if new == 'on': # sun is rising
        self.start_day(None)
    elif new == 'off': # sun is setting
        self.start_evening(None)

  def calculate_state(self):
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
    if now > self.night_start:
      return "night"

  def start_morning(self, kwargs):
    if self.sun_up() or self.current_state == "day":
      self.log("Sun is already up, no morning needed")
      return
    self.log("morning_start()")
    self.activate_scene("morning")

  def start_late_morning(self, kwargs):
    if self.sun_up() or self.current_state == "day":
      self.log("Sun is already up, no morning needed")
      return
    self.log("start_late_morning()")
    self.activate_scene("laste_morning")

  def start_day(self, kwargs):
    self.log("day_start()")
    self.activate_scene("day")

  def start_evening(self, kwargs):
    if self.current_state == "night":
      self.log("last state was night, no evening needed")
      return
    self.log("evening_start()")
    self.activate_scene("evening")

  def start_early_night(self, kwargs):
    self.log("start_early_night()")
    self.activate_scene("early_night")

  def start_night(self, kwargs):
    self.log("night_start()")
    self.activate_scene("night")

  def activate_scene(self, scene_name):
    self.log("activate_scene({})".format(scene_name))
    self.get_groups()
    self.current_state = scene_name
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
      entity = kwargs.get("entity")
      state = kwargs.get("state")
      self.log("_turn_onoff(self, kwargs): entity={} state={}".format(entity, state))
      if entity is None or state is None:
          return
      if state == True:
          self.turn_on(entity)
      elif state == False:
          self.turn_off(entity)

