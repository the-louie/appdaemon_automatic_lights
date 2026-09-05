"""One definition of "this entity is not telling us anything".

Before this module (T-06 / S7-07) the estate held at least seven private
variants of the same idea, and the differences were not style:

    ("unavailable", "unknown", "none", "")      battery_checker
    (None, "unavailable", "unknown")            humid_trigger -- defined, then
                                                unused: its methods used literals
    ("unavailable", "unknown", "")              growlights
    frozenset({"unavailable", "unknown", ""})   automatic_lights
    ["unavailable", "unknown", None]            open_window, twice, differently
    == "unavailable"                            temp_notification -- misses
                                                "unknown" entirely
    != "unavailable"                            the old growlights UV check,
                                                which is what produced T-50 G3

Each gap was a real behaviour: a sensor reporting "unknown" sailed past the
temperature alarm's guard, an empty-string state fell through humid_trigger's
classifier into silence, and growlights re-commanded dead switches every ten
minutes forever. The variant zoo is not seven answers to one question; it is
seven different bugs wearing the same comment.

The set is the union of every variant, deliberately: "none" is included because
Home Assistant surfaces the literal string in places, and matching one variant's
extra state can only ever *stop* a value being treated as real -- the safe
direction for every consumer this estate has.

Prefer the predicates over the frozenset: they fold in the `None` check and the
case/whitespace normalisation that half the variants forgot.

Copied byte-identical into every consumer repo, on the `notification_policy`
pattern, guarded by the copies-match test. Canonical copy:
`appdaemon_automatic_lights/ha_states.py`.
"""

HA_UNAVAILABLE_STATES = frozenset({"unavailable", "unknown", "none", ""})


def is_reporting(state) -> bool:
    """True when `state` carries a real value rather than an absence marker."""
    if state is None:
        return False
    return str(state).strip().lower() not in HA_UNAVAILABLE_STATES


def not_reporting(state) -> bool:
    """The same question from the other side, for call sites that read better this way."""
    return not is_reporting(state)
