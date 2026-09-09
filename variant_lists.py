"""Phase 3, this node's half: turn the membership plan into Brevo list membership.

The plan is READ from mkt_control.variant_list_plan and never recomputed here. Everything in
this file is about the gap between a plan and a live list: what to add, what to remove, and
which removals are allowed to wait.

THE FLOORS, AND THE ONE THING THEY MUST NOT DO
----------------------------------------------
A drift floor is the smallest change worth making. Below it the list is left alone, because
churning a Brevo list for two addresses costs API calls and buries the real movements in noise.
Growth floor max(10; 0,5 %); shrink floor max(5; 3 %) - asymmetric on purpose, since a wrong
audience shows up as a mass removal and should need a louder signal to be acted on.

But a floor that holds back a removal can produce a WRONG SEND, and that is not a trade this
system makes. Two removals are therefore never subject to the floor:

  * a suppressed address - somebody said no, and "below the threshold" is not an answer to that;
  * a person planned for a DIFFERENT list on the same send_date - if they stay on both they get
    two letters, and the sender's union-dedup assertion then fires on a state we created here.

Only "no longer planned at all" removals wait for the floor. That split is the whole point: the
floor suppresses noise, never correctness.

A first materialisation is not drift. When a list is empty there is nothing to drift from, so
the floors do not apply to it - otherwise a genuine six-person variant could never be filled.
"""
import logging

log = logging.getLogger("variant")


def floor(minimum, frac, current):
    """The smallest change worth acting on for a list of this size."""
    return max(minimum, int(frac * current))


def plan_list_change(current, target, suppressed, planned_elsewhere,
                     growth_min, growth_frac, shrink_min, shrink_frac):
    """Decide one list's changes. Pure - no Brevo, no BigQuery - so it can be tested offline.

    current           : addresses on the Brevo list now
    target            : addresses the plan says belong on it (already suppression-filtered)
    suppressed        : opted-out addresses
    planned_elsewhere : addresses planned for a different list on the same send_date

    Returns what should happen and, as importantly, what was held and why.
    """
    to_add = sorted(target - current)
    remove_candidates = current - target
    forced = {e for e in remove_candidates if e in suppressed or e in planned_elsewhere}
    forced_remove = sorted(forced)
    optional_remove = sorted(remove_candidates - forced)

    first_fill = len(current) == 0
    g_floor = floor(growth_min, growth_frac, len(current))
    s_floor = floor(shrink_min, shrink_frac, len(current))

    add_ok = first_fill or len(to_add) >= g_floor
    opt_remove_ok = len(optional_remove) >= s_floor

    return {
        "target": len(target), "current": len(current),
        "to_add": to_add if add_ok else [],
        "held_add": [] if add_ok else to_add,
        "to_remove": forced_remove + (optional_remove if opt_remove_ok else []),
        "forced_remove": forced_remove,
        "held_remove": [] if opt_remove_ok else optional_remove,
        "growth_floor": g_floor, "shrink_floor": s_floor, "first_fill": first_fill,
    }
