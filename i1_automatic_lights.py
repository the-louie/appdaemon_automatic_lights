"""
Automatic Lights - Home Assistant AppDaemon App
Copyright (c) 2025 the_louie
"""

from __future__ import annotations

import datetime
import random
import time
from dataclasses import dataclass, field

import appdaemon.plugins.hass.hassapi as hass

import notification_policy as policy

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

# Verification: how long after the last staggered command to check the result,
# how many results to keep, and when to summarise the day.
DEFAULT_AUDIT_MARGIN_SECONDS = 30
DEFAULT_AUDIT_HISTORY = 200
DEFAULT_DAILY_REPORT_TIME = "23:55:00"

# Notification. The channel matters: a notification that names no channel goes
# to the companion app's default one, which on this household's phone is
# disabled -- Android discards it while Home Assistant reports success (T-52).
DEFAULT_NOTIFY_CHANNEL = "light_alerts"
DEFAULT_NOTIFY_PRIORITY = "high"

# Throttle / logging
SUN_HANDLER_THROTTLE_SECONDS = 60
NO_TRANSITION_LOG_INTERVAL = 15  # Log every Nth no-transition check (~15 min)

# HA states that indicate a sensor is not reporting valid data, and equally
# that a command sent to that entity will not turn anything on. `None` from
# get_state is handled separately: a missing entity is a config error, an
# unavailable one is usually a flat battery or a device off the network.
# The set itself is shared estate-wide since S7-07 (T-06): see ha_states.py.
from ha_states import HA_UNAVAILABLE_STATES  # noqa: E402


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
class ExpectedAbsence:
    """An entity that is *supposed* to be unavailable, for a stated period.

    `switch.v2_kok_girlang` is a Christmas ornament. It is unplugged for eleven
    months of the year, so an alarm on it would fire from December to November
    and teach everyone to ignore the alarm -- which is worse than not having
    one. Suppressing it needs two things and neither is optional:

    `reason`  -- a suppression nobody can explain is a suppression nobody dares
                 remove, so it becomes permanent by default.
    `review`  -- past this date the entry stops suppressing and starts warning
                 about itself. That is the difference between a decision with an
                 expiry and a silence that outlives the person who set it.
    """

    entity_id: str
    reason: str
    review: datetime.date


@dataclass
class Divergence:
    """One entity that did not end up where the scene put it."""

    entity_id: str
    group: str
    expected: str
    actual: str


@dataclass
class AuditResult:
    """What a scene asked for, against what the house actually did.

    The reachability audit (U0xx) answers "could this command land". This
    answers the different and harder question: "did it". An entity can be
    perfectly reachable, accept the command, and still not be in the commanded
    state a minute later -- someone used the wall switch, a bulb dropped off
    and rejoined, a competing app wrote over it. Nothing in this system has
    ever looked.
    """

    scene: str
    when: str
    checked: int
    skipped: int
    diverged: list[Divergence]
    checked_ids: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.diverged


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
        self.expected_absent: dict[str, ExpectedAbsence] = {}
        self.audit_history: list[AuditResult] = []
        self.audit_margin: float = DEFAULT_AUDIT_MARGIN_SECONDS
        self.audit_history_limit: int = DEFAULT_AUDIT_HISTORY
        self.daily_report_time: str = DEFAULT_DAILY_REPORT_TIME
        self.notify_targets: list[str] = []
        self.notification_channel: str | None = DEFAULT_NOTIFY_CHANNEL
        self.notification_priority: str | None = DEFAULT_NOTIFY_PRIORITY
        self.quiet_start: int = policy.DEFAULT_QUIET_START
        self.quiet_end: int = policy.DEFAULT_QUIET_END
        self.repeat_after: float = policy.DEFAULT_REPEAT_AFTER
        self._notify_state: dict[str, float] = {}
        self._pending_timers: list = []

    def initialize(self):
        """Initialize the app."""
        self.log("[A001] Starting initialization")

        self._load_config()
        self._setup_groups_and_areas()
        self.current_state = self._calculate_state()

        self._register_listeners()
        self._schedule_daily_events()
        self._schedule_daily_report()

        self._audit_lighting_groups("startup")
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
        self.expected_absent = self._load_expected_absent()

        audit_raw = self.args.get("audit") or {}
        self.audit_margin = self._positive_number(
            audit_raw.get("verify_margin_seconds"), DEFAULT_AUDIT_MARGIN_SECONDS,
            "audit.verify_margin_seconds",
        )
        self.audit_history_limit = int(
            self._positive_number(
                audit_raw.get("history"), DEFAULT_AUDIT_HISTORY, "audit.history"
            )
        )
        self.daily_report_time = self._parse_time_config(
            "audit_daily_report_time", DEFAULT_DAILY_REPORT_TIME
        )
        self._load_notify_config()

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

    def _schedule_daily_report(self):
        """One summary a day, so a quiet day is visibly quiet rather than absent."""
        if not self.daily_report_time:
            return
        try:
            self.run_daily(self._daily_light_report, self.daily_report_time)
            self.log(
                "[V007] Daily light report scheduled for {}".format(
                    self.daily_report_time
                )
            )
        except Exception as exc:
            if isinstance(exc, (TypeError, AttributeError, NameError)):
                raise
            self.log(
                "[V008] Could not schedule the daily light report: {}".format(exc),
                level="ERROR",
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

    def _today(self) -> datetime.date:
        """Today, on AppDaemon's clock rather than the process's.

        No fallback to datetime.now(): that is the container's timezone, not
        Home Assistant's, and if get_now() is unavailable the app has a bigger
        problem than a date. Tests override this method.
        """
        return self.get_now().date()

    def _positive_number(self, value, default: float, key: str) -> float:
        """A knob that must be a positive number, or the default, loudly."""
        if value is None:
            return default
        try:
            number = float(value)
        except (ValueError, TypeError):
            number = None
        if number is None or number <= 0:
            self.log(
                "[A013] {} must be a positive number, got {!r} -- using "
                "{}".format(key, value, default),
                level="WARNING",
            )
            return default
        return number

    def _load_expected_absent(self) -> dict[str, ExpectedAbsence]:
        """Parse `expected_absent`, rejecting entries loudly rather than silently.

        Per D1 (fail loud), a malformed entry is dropped and reported. Dropping
        it means the entity goes back to warning normally, which is the safe
        direction: a broken suppression should make noise, not silence.
        """
        raw = self.args.get("expected_absent") or {}
        if not isinstance(raw, dict):
            self.log(
                "[U012] expected_absent must be a mapping of entity_id to "
                "{{reason, review}}, got {} -- ignoring all of it".format(
                    type(raw).__name__
                ),
                level="ERROR",
            )
            return {}

        parsed: dict[str, ExpectedAbsence] = {}
        for entity_id, spec in raw.items():
            if not isinstance(spec, dict):
                self.log(
                    "[U012] expected_absent['{}'] must be a mapping with "
                    "reason and review, got {} -- entry ignored".format(
                        entity_id, type(spec).__name__
                    ),
                    level="ERROR",
                )
                continue

            reason = str(spec.get("reason") or "").strip()
            if not reason:
                self.log(
                    "[U012] expected_absent['{}'] has no reason -- entry "
                    "ignored. A suppression nobody can explain is one nobody "
                    "dares remove.".format(entity_id),
                    level="ERROR",
                )
                continue

            review_raw = spec.get("review")
            if review_raw is None:
                self.log(
                    "[U012] expected_absent['{}'] has no review date -- entry "
                    "ignored. Without one the suppression is permanent.".format(
                        entity_id
                    ),
                    level="ERROR",
                )
                continue

            review = self._parse_review_date(entity_id, review_raw)
            if review is None:
                continue

            parsed[entity_id] = ExpectedAbsence(entity_id, reason, review)

        if parsed:
            self.log(
                "[U013] {} expected-absent entrie(s): {}".format(
                    len(parsed),
                    ", ".join(
                        "{} until {}".format(e.entity_id, e.review)
                        for e in parsed.values()
                    ),
                )
            )
        return parsed

    def _parse_review_date(self, entity_id: str, value) -> datetime.date | None:
        """Accept a real date from YAML, or an ISO string. Reject anything else."""
        if isinstance(value, datetime.datetime):
            return value.date()
        if isinstance(value, datetime.date):
            return value
        try:
            return datetime.date.fromisoformat(str(value).strip())
        except (ValueError, TypeError):
            self.log(
                "[U012] expected_absent['{}'] review '{}' is not a YYYY-MM-DD "
                "date -- entry ignored".format(entity_id, value),
                level="ERROR",
            )
            return None

    def _absence_verdict(self, entity_id: str) -> tuple[str, ExpectedAbsence | None]:
        """Classify an unreachable entity: 'unexpected', 'expected', 'expired'."""
        entry = self.expected_absent.get(entity_id)
        if entry is None:
            return "unexpected", None
        if self._today() > entry.review:
            return "expired", entry
        return "expected", entry

    # ── Telling someone ────────────────────────────────────────────

    def _load_notify_config(self):
        """Notification is opt-in, and its absence is stated rather than assumed.

        The watchdog *raises* without `notify_targets`, because a watchdog that
        cannot tell anyone is pointless. This app is different: it controlled
        lights correctly for a long time before it could notify anything, and
        making the key mandatory would stop every light in the house on the
        next pull. So it is optional -- but silence about being unable to speak
        is exactly the failure this whole sprint is about, so it says so once,
        at startup, at WARNING.
        """
        raw = self.args.get("notify_targets") or []
        if isinstance(raw, str):
            raw = [raw]

        targets: list[str] = []
        if not isinstance(raw, list):
            self.log(
                "[N001] notify_targets must be a list of service names, got "
                "{} -- divergences will be logged but not sent".format(
                    type(raw).__name__
                ),
                level="ERROR",
            )
        else:
            for target in raw:
                if not isinstance(target, str) or not target.strip():
                    self.log(
                        "[N001] notify_targets entries must be non-empty "
                        "strings, got {!r} -- skipped".format(target),
                        level="ERROR",
                    )
                    continue
                name = target.strip()
                if name.startswith("notify."):
                    # A whole sprint was lost to this shape once. Correct it and
                    # say so rather than sending to a service that cannot exist.
                    self.log(
                        "[N001] notify_targets takes the service name without "
                        "the domain: using {!r}, not {!r}".format(
                            name[len("notify."):], name
                        ),
                        level="WARNING",
                    )
                    name = name[len("notify."):]
                targets.append(name)

        self.notify_targets = targets
        if not targets:
            self.log(
                "[N002] No notify_targets configured -- light divergences will "
                "be logged only. Nobody will be told.",
                level="WARNING",
            )

        channel = self.args.get("notification_channel", DEFAULT_NOTIFY_CHANNEL)
        self.notification_channel = channel if isinstance(channel, str) else None
        if channel is not None and not isinstance(channel, str):
            self.log(
                "[N001] notification_channel must be a string, got {!r} -- "
                "sending without one, which Android may discard".format(channel),
                level="ERROR",
            )
        priority = self.args.get("notification_priority", DEFAULT_NOTIFY_PRIORITY)
        self.notification_priority = priority if isinstance(priority, str) else None

        self.quiet_start = int(
            self._hour_config("quiet_start", policy.DEFAULT_QUIET_START)
        )
        self.quiet_end = int(
            self._hour_config("quiet_end", policy.DEFAULT_QUIET_END)
        )
        self.repeat_after = self._positive_number(
            self.args.get("repeat_after"), policy.DEFAULT_REPEAT_AFTER, "repeat_after"
        )

    def _hour_config(self, key: str, default: int) -> int:
        value = self.args.get(key, default)
        try:
            hour = int(value)
        except (ValueError, TypeError):
            hour = None
        if hour is None or not 0 <= hour <= 23:
            self.log(
                "[N001] {} must be an hour 0-23, got {!r} -- using {}".format(
                    key, value, default
                ),
                level="WARNING",
            )
            return default
        return hour

    def _notification_data(self) -> dict:
        """Companion-app data block. See T-52, and do not drop it.

        A notification that does not name a channel goes to the companion app's
        default channel, which on this household's phone is disabled. Android
        discards it and Home Assistant reports success -- the exact shape of
        failure this sprint exists to remove.
        """
        if not self.notification_channel:
            return {}
        data = {"channel": self.notification_channel}
        if self.notification_priority:
            data["priority"] = self.notification_priority
            data["ttl"] = 0
        return data

    def _notify_divergences(self, result: AuditResult) -> list[str]:
        """Tell someone, once per light per repeat window.

        Keyed per entity rather than per scene, so a single stuck light does
        not produce a message at every transition all day -- and so a second
        light going wrong is still news even while the first is being held.
        """
        # A light that was reported and is now fine must have its state
        # cleared, or its NEXT failure is held as a repeat and nobody is told.
        # policy.apply drops keys absent from the active set for exactly this
        # reason, but it only sees the entities of one scene, so the clearing
        # has to be scoped to what this run actually checked.
        healthy = set(result.checked_ids) - {d.entity_id for d in result.diverged}
        for entity_id in healthy:
            if self._notify_state.pop(entity_id, None) is not None:
                self.log(
                    "[N006] {} is back in the commanded state; its next failure "
                    "will be reported as news".format(entity_id)
                )

        if result.clean or not self.notify_targets:
            return []

        keys = {d.entity_id for d in result.diverged}
        now_epoch = self._now_epoch()
        to_send, held, new_state = policy.apply(
            keys,
            now_epoch,
            self._now_hour(),
            self._notify_state,
            quiet_start=self.quiet_start,
            quiet_end=self.quiet_end,
            repeat_after=self.repeat_after,
        )
        self._notify_state = new_state

        if held:
            self.log(
                "[N005] Holding {} light divergence(s): {}".format(
                    len(held),
                    ", ".join("{} ({})".format(k, why) for k, why in held),
                )
            )
        if not to_send:
            return []

        sending = {k for k, _ in to_send}
        lines = [
            "{} wanted {} but is {}".format(d.entity_id, d.expected, d.actual)
            for d in result.diverged
            if d.entity_id in sending
        ]
        body = "Scene '{}':\n{}".format(result.scene, "\n".join(lines))

        sent = []
        for target in self.notify_targets:
            try:
                self.call_service(
                    "notify/{}".format(target),
                    title="Lights did not do as told",
                    message=body,
                    data=self._notification_data(),
                )
                sent.append(target)
            except Exception as exc:
                if isinstance(exc, (TypeError, AttributeError, NameError)):
                    raise
                # One bad target must not stop the others being told.
                self.log(
                    "[N004] Could not notify {}: {}".format(target, exc),
                    level="ERROR",
                )
        self.log(
            "[N003] Notified {} about {} light divergence(s)".format(
                ", ".join(sent) or "nobody", len(sending)
            )
        )
        return sent

    def _now_epoch(self) -> float:
        """Epoch seconds. Note .timestamp(), not datetime subtraction: two
        datetimes sharing a tzinfo subtract to WALL-CLOCK difference, which is
        an hour wrong across the 25 October fold."""
        return self.get_now().timestamp()

    def _now_hour(self) -> int:
        return self.get_now().hour

    # ── Did it actually happen ─────────────────────────────────────

    def _expected_text(self, target_state: bool) -> str:
        return "on" if target_state else "off"

    def _verify_scene(self, kwargs):
        """Compare what a scene commanded against what the house did.

        Runs once per scene activation, after the staggered commands have had
        time to land. Unreachable entities are NOT reported here -- the U0xx
        audit already owns those, and repeating them would bury the finding
        this check exists for: an entity that IS reachable, DID accept the
        command, and is still in the wrong state.
        """
        scene_name = kwargs.get("scene")
        expectations = kwargs.get("expectations") or []

        diverged: list[Divergence] = []
        checked_ids: list[str] = []
        skipped = 0

        for entity_id, group, target in expectations:
            reachable, actual = self._entity_reachability(entity_id)
            if not reachable:
                skipped += 1
                continue
            verdict, _entry = self._absence_verdict(entity_id)
            if verdict == "expected":
                skipped += 1
                continue

            checked_ids.append(entity_id)
            want = self._expected_text(target)
            if actual != want:
                diverged.append(Divergence(entity_id, group, want, actual))

        result = AuditResult(
            scene=scene_name,
            when=self._now_text(),
            checked=len(checked_ids),
            skipped=skipped,
            diverged=diverged,
            checked_ids=checked_ids,
        )
        self._record_audit(result)

        if result.clean:
            self.log(
                "[V001] Scene '{}' verified: {} entities in the commanded "
                "state ({} skipped)".format(scene_name, len(checked_ids), skipped)
            )
        else:
            self.log(
                "[V002] Scene '{}' DIVERGED: {} of {} entities not in the "
                "commanded state -- {}".format(
                    scene_name,
                    len(diverged),
                    len(checked_ids),
                    ", ".join(
                        "{} ({}) wanted {} but is {}".format(
                            d.entity_id, d.group, d.expected, d.actual
                        )
                        for d in diverged
                    ),
                ),
                level="WARNING",
            )
        self._notify_divergences(result)
        return result

    def _record_audit(self, result: AuditResult):
        """Keep a bounded history so the daily report has something to read."""
        self.audit_history.append(result)
        if len(self.audit_history) > self.audit_history_limit:
            del self.audit_history[: -self.audit_history_limit]

    def _now_text(self) -> str:
        return self.get_now().isoformat(timespec="seconds")

    def _schedule_verification(
        self, scene_name: str, entities: list[EntityControl], after: float
    ):
        """Check the result once the last staggered command has had time."""
        if not entities:
            return
        delay = max(0.0, after) + self.audit_margin
        expectations = [(e.entity_id, e.group, e.target_state) for e in entities]
        self.log(
            "[V003] Scene '{}': verifying {} entities in {:.0f}s".format(
                scene_name, len(expectations), delay
            )
        )
        handle = self.run_in(
            self._verify_scene, delay, scene=scene_name, expectations=expectations
        )
        self._pending_timers.append(handle)

    def _daily_light_report(self, kwargs=None) -> dict:
        """Summarise the day: how many transitions, how many diverged, and where.

        This is the artefact O1 has never had. "The lights behave as expected"
        stops being an opinion the moment there is a day of transitions with a
        pass or fail against each one.
        """
        today = str(self._today())
        todays = [r for r in self.audit_history if r.when.startswith(today)]

        if not todays:
            self.log(
                "[V004] Daily light report {}: no scene transitions recorded"
                .format(today),
                level="WARNING",
            )
            return {"date": today, "transitions": 0, "diverged": 0, "offenders": {}}

        bad = [r for r in todays if not r.clean]
        offenders: dict[str, int] = {}
        for result in bad:
            for d in result.diverged:
                offenders[d.entity_id] = offenders.get(d.entity_id, 0) + 1

        summary = {
            "date": today,
            "transitions": len(todays),
            "diverged": len(bad),
            "offenders": offenders,
        }

        if not bad:
            self.log(
                "[V005] Daily light report {}: {} transitions, all verified "
                "clean".format(today, len(todays))
            )
            return summary

        self.log(
            "[V006] Daily light report {}: {} of {} transitions diverged. "
            "Worst offenders: {}".format(
                today,
                len(bad),
                len(todays),
                ", ".join(
                    "{} x{}".format(e, n)
                    for e, n in sorted(
                        offenders.items(), key=lambda kv: -kv[1]
                    )[:5]
                ),
            ),
            level="WARNING",
        )
        return summary

    def _entity_reachability(self, entity_id: str) -> tuple[bool, str]:
        """Return (reachable, why). `why` is the state, or 'missing'."""
        state = self.get_state(entity_id)
        if state is None:
            return False, "missing"
        text = str(state).lower()
        if text in HA_UNAVAILABLE_STATES:
            return False, text
        return True, text

    def _unreachable_members(self, group_id: str) -> list[tuple[str, str]]:
        """Members of `group_id` that would not respond to a command."""
        broken = []
        for entity_id in self.groups.get(group_id, []):
            reachable, why = self._entity_reachability(entity_id)
            if not reachable:
                broken.append((entity_id, why))
        return broken

    def _scenes_using(self, group_name: str) -> list[str]:
        """Scene names referencing this group, so a warning can name them."""
        return sorted(
            name for name, config in self.scenes.items() if group_name in config
        )

    def _configured_group_names(self) -> list[str]:
        """Group names any scene refers to, deduplicated, in first-seen order."""
        seen: dict[str, None] = {}
        for config in self.scenes.values():
            for group_name in config:
                seen.setdefault(group_name, None)
        return list(seen)

    def _audit_lighting_groups(self, context: str) -> int:
        """Warn about every group member that cannot respond to a command.

        Why this exists. On 2026-09-03 `group.bedroom_lightning` had exactly one
        member and that member was unavailable, so the late_morning, early_night
        and evening scenes had all been commanding a group that could not
        respond. The app logged "[H001] Turned ON" every time and nothing said
        otherwise. The group's own state read `unknown` rather than `off`.

        Returns the number of unreachable members, so callers and tests can
        assert on a number rather than on log text.
        """
        self.log("[U001] Reachability audit ({})".format(context))
        total = 0

        for group_name in self._configured_group_names():
            group_id = "group.{}".format(group_name)
            members = self.groups.get(group_id, [])
            scenes = self._scenes_using(group_name) or ["none"]

            if not members:
                self.log(
                    "[U004] Group '{}' has no members but is referenced by "
                    "scene(s) {} -- those scenes control nothing".format(
                        group_name, ", ".join(scenes)
                    ),
                    level="WARNING",
                )
                continue

            broken = self._unreachable_members(group_id)
            broken_ids = {e for e, _ in broken}

            # An entity that is reachable but still on the expected-absent list
            # has come back. Say so, or the entry outlives its own reason.
            for entity_id in members:
                if entity_id in self.expected_absent and entity_id not in broken_ids:
                    self.log(
                        "[U011] {} is reachable again but is still listed as "
                        "expected-absent ({}) -- remove the entry".format(
                            entity_id, self.expected_absent[entity_id].reason
                        )
                    )

            if not broken:
                continue

            # Split by verdict before deciding severity. A group that is dark on
            # purpose is not the same event as one that is dark by accident.
            unexpected: list[tuple[str, str]] = []
            for entity_id, why in broken:
                verdict, entry = self._absence_verdict(entity_id)
                if verdict == "expected":
                    self.log(
                        "[U009] Group '{}': {} is {}, expected until {} ({})".format(
                            group_name, entity_id, why, entry.review, entry.reason
                        )
                    )
                elif verdict == "expired":
                    self.log(
                        "[U010] Group '{}': {} is {} and its expected-absent "
                        "entry expired on {} ({}) -- confirm it is still "
                        "expected, or fix the device".format(
                            group_name, entity_id, why, entry.review, entry.reason
                        ),
                        level="WARNING",
                    )
                    unexpected.append((entity_id, why))
                else:
                    unexpected.append((entity_id, why))

            total += len(unexpected)
            if not unexpected:
                continue

            # A group where every member is down is a different problem from
            # one bad bulb: the scene has no effect at all. That is exactly what
            # went unnoticed with bedroom_lightning, so it gets its own level.
            if len(broken) == len(members):
                self.log(
                    "[U003] Group '{}' is ENTIRELY unreachable ({}/{}): {} -- "
                    "scene(s) {} command nothing".format(
                        group_name,
                        len(broken),
                        len(members),
                        ", ".join("{} is {}".format(e, w) for e, w in broken),
                        ", ".join(scenes),
                    ),
                    level="ERROR",
                )
                continue

            for entity_id, why in unexpected:
                self.log(
                    "[U002] Group '{}': {} is {} -- wanted by scene(s) {}".format(
                        group_name, entity_id, why, ", ".join(scenes)
                    ),
                    level="WARNING",
                )

        if total == 0:
            self.log("[U005] Reachability audit clean ({})".format(context))
        return total

    def _audit_scene_entities(
        self, scene_name: str, entities: list[EntityControl]
    ) -> int:
        """Warn about the entities this scene is about to fail to control."""
        broken = []
        for control in entities:
            reachable, why = self._entity_reachability(control.entity_id)
            if reachable:
                continue
            verdict, _entry = self._absence_verdict(control.entity_id)
            if verdict == "expected":
                continue  # dark on purpose; the startup audit records it
            broken.append((control, why))

        if not broken:
            return 0

        if len(broken) == len(entities):
            self.log(
                "[U006] Scene '{}' will control NOTHING: all {} entities are "
                "unreachable ({})".format(
                    scene_name,
                    len(entities),
                    ", ".join(
                        "{} is {}".format(c.entity_id, w) for c, w in broken
                    ),
                ),
                level="ERROR",
            )
            return len(broken)

        self.log(
            "[U007] Scene '{}': {} of {} entities unreachable -- {}".format(
                scene_name,
                len(broken),
                len(entities),
                ", ".join(
                    "{} ({}) is {}".format(c.entity_id, c.group, w)
                    for c, w in broken
                ),
            ),
            level="WARNING",
        )
        return len(broken)

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
        self._audit_scene_entities(scene_name, entities)

        if not entities:
            return

        if immediate:
            self.log(
                "[F004] Immediate control for {} entities".format(len(entities))
            )
            for ec in entities:
                self._turn_onoff(entity=ec.entity_id, state=ec.target_state)
            self._schedule_verification(scene_name, entities, 0.0)
        else:
            self.log(
                "[F005] Staggered control for {} entities".format(len(entities))
            )
            last = self._execute_staggered_control(entities)
            self._schedule_verification(scene_name, entities, last)

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

    def _execute_staggered_control(self, entities: list[EntityControl]) -> float:
        """Schedule entity control with randomised area-based staggering.

        Returns the largest delay scheduled, so the caller knows when the last
        command will have landed and can verify the result after it.
        """
        self.log("[G001] Starting staggered control")

        if not entities:
            self.log("[G002] No entities to control")
            return 0.0

        # Group by area
        area_groups: dict[str, list[EntityControl]] = {}
        for ec in entities:
            area_groups.setdefault(ec.area, []).append(ec)

        areas = list(area_groups)
        random.shuffle(areas)
        self.log("[G007] Randomised area order: {}".format(areas))

        current_delay = 0.0
        max_delay = 0.0

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
                max_delay = max(max_delay, entity_delay)

            if len(areas) > 1:
                current_delay += random.uniform(
                    self.stagger.room_delay_min,
                    self.stagger.room_delay_max,
                )

        self.log(
            "[G013] Staggered control scheduled, last command in {:.0f}s".format(
                max_delay
            )
        )
        return max_delay

    def _turn_onoff(self, **kwargs):
        """Turn an entity on or off, and be honest about whether it can land."""
        entity = kwargs.get("entity")
        state = kwargs.get("state")

        if not entity or state is None:
            self.log(
                "[H004] Invalid call: entity={}, state={}".format(entity, state)
            )
            return

        # Read reachability BEFORE commanding. The command is still issued
        # either way, so a device that comes back finds the right state waiting
        # for it -- what changes is that an unreachable entity no longer gets
        # logged as a success. "[H001] Turned ON" against a dead entity is
        # precisely the line that let bedroom_lightning look healthy for weeks
        # while commanding nothing at all.
        reachable, why = self._entity_reachability(entity)

        try:
            if state:
                self.turn_on(entity)
            else:
                self.turn_off(entity)
        except Exception as exc:
            if isinstance(exc, (TypeError, AttributeError, NameError)):
                raise  # Programming error, do not swallow
            self.log(
                "[H003] Failed to control {}: {} ({})".format(
                    entity, exc, type(exc).__name__
                ),
                level="ERROR",
            )
            return

        if not reachable:
            verdict, _entry = self._absence_verdict(entity)
            if verdict != "expected":
                self.log(
                    "[U008] Commanded {} {} but it is {} -- the command cannot "
                    "have taken effect".format(
                        entity, "ON" if state else "OFF", why
                    ),
                    level="WARNING",
                )
            return

        if state:
            self.log("[H001] Turned ON: {}".format(entity))
        else:
            self.log("[H002] Turned OFF: {}".format(entity))
