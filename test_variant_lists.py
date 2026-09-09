"""Offline tests for the drift floors. No Brevo, no BigQuery, no network.

They pin the one property that matters: a floor may delay a cosmetic change, and may never
delay a correctness one.
"""
import sys

from variant_lists import plan_list_change, floor

G_MIN, G_FRAC, S_MIN, S_FRAC = 10, 0.005, 5, 0.03


def change(current, target, suppressed=(), elsewhere=()):
    return plan_list_change(set(current), set(target), set(suppressed), set(elsewhere),
                            G_MIN, G_FRAC, S_MIN, S_FRAC)


def test_floor_is_the_larger_of_absolute_and_relative():
    assert floor(10, 0.005, 100) == 10
    assert floor(10, 0.005, 4000) == 20
    assert floor(5, 0.03, 1000) == 30


def test_first_fill_ignores_the_growth_floor():
    r = change([], ["a", "b", "c", "d", "e", "f"])
    assert r["first_fill"] is True
    assert len(r["to_add"]) == 6 and r["held_add"] == []


def test_small_growth_is_held_on_a_live_list():
    cur = [f"u{i}" for i in range(100)]
    r = change(cur, cur + ["new1", "new2"])
    assert r["to_add"] == [] and r["held_add"] == ["new1", "new2"]


def test_growth_above_the_floor_is_applied():
    cur = [f"u{i}" for i in range(100)]
    new = [f"n{i}" for i in range(10)]
    r = change(cur, cur + new)
    assert sorted(r["to_add"]) == sorted(new) and r["held_add"] == []


def test_small_ordinary_shrink_is_held():
    cur = [f"u{i}" for i in range(100)]
    r = change(cur, cur[:98])
    assert r["to_remove"] == [] and len(r["held_remove"]) == 2


def test_a_suppressed_address_is_removed_however_small():
    cur = [f"u{i}" for i in range(1000)]
    r = change(cur, cur[:999], suppressed=["u999"])
    assert r["to_remove"] == ["u999"] and r["forced_remove"] == ["u999"]
    assert r["held_remove"] == []


def test_a_person_moved_to_another_variant_is_removed_however_small():
    cur = [f"u{i}" for i in range(1000)]
    r = change(cur, cur[:999], elsewhere=["u999"])
    assert r["to_remove"] == ["u999"]


def test_forced_and_held_removals_coexist():
    cur = [f"u{i}" for i in range(1000)]
    r = change(cur, cur[:997], suppressed=["u999"])
    assert r["forced_remove"] == ["u999"]
    assert sorted(r["held_remove"]) == ["u997", "u998"]
    assert r["to_remove"] == ["u999"]


def test_no_change_is_no_change():
    cur = [f"u{i}" for i in range(50)]
    r = change(cur, cur)
    assert r["to_add"] == [] and r["to_remove"] == [] and r["held_remove"] == []


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print("PASS", name)
            except AssertionError as e:
                failures += 1; print("FAIL", name, e)
    sys.exit(1 if failures else 0)
