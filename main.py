"""The only job that holds a Brevo credential.

Why it exists (Raivis, 2026-09-02): so that tiktik-marketing-sender holds NO Brevo credential
at all. An unbound key leaves the sender able to send and merely without a password - one
config mistake away from an accident. A sender with no sending code cannot send. This job takes
the credential so the sender does not have to.

What it does, and nothing else:
  1. exports Brevo contacts -> business_marts.brevo_contacts_snapshot (straight into BigQuery)
  2. refreshes Brevo template isActive -> mkt_control.brevo_template_status
  3. records, per track, whether its letter could actually go out
  4. syncs email_suppression_all -> Brevo list 4 (tiktik_suppression), ADD-ONLY
  5. writes the weekly akcija residual -> Brevo list 65 (tiktik_akcija_atlikums)
  6. filters every list it writes against email_suppression_all BEFORE writing
  7. materialises one Brevo list per variant from mkt_control.variant_list_plan (Phase 3)

What it cannot do: send. See brevo_client.py - the client exposes four operations and the
surface is asserted at import time.

Exit codes: 0 = ran and reported. 1 = crash. 3 = deliberate stop (a guard refused). A hold must
never look like a breakage - the 03:00 job exited 1 for 57 nights and nobody could tell.
"""
import datetime as dt
import json
import logging
import os
import sys
import uuid

import config as C
import bq
import variant_lists
from brevo_client import BrevoContactsClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("snapshot")

RUN_ID = os.environ.get("CLOUD_RUN_EXECUTION") or f"local-{uuid.uuid4().hex[:12]}"
EXIT_DELIBERATE_STOP = 3

READY = "READY"


class Hold(RuntimeError):
    """A deliberate refusal. Distinct from a crash, on purpose."""


def _now():
    return dt.datetime.now(dt.timezone.utc)


def step1_snapshot(brevo):
    if not C.REFRESH_SNAPSHOT:
        log.warning("SNAPSHOT_REFRESH_SKIPPED (REFRESH_SNAPSHOT=false) - the sender's "
                    "SUPPRESSION_FRESH guard will stop it once this goes stale")
        return None
    data = brevo.export_contacts_ndjson()
    n = bq.load_snapshot(data)
    log.info("SNAPSHOT_REFRESHED contacts=%s", n)
    return n


def step2_template_status(brevo, report):
    """Brevo template isActive -> BigQuery, so the sender can see it without a credential.

    Brevo refuses to send an INACTIVE template. Before 2026-09-03 that would have surfaced
    mid-send, after a track was already flipped. Now it is a row the pre-launch report and the
    sender both read.
    """
    rows = brevo.list_templates()
    checked = _now().isoformat()
    for r in rows:
        r["checked_at"] = checked
    report["templates_read"] = bq.load_template_status(rows)
    log.info("TEMPLATE_STATUS_REFRESHED n=%s active=%s",
             len(rows), sum(1 for r in rows if r["is_active"]))


def step3_readiness(report):
    """Per track: would this letter actually go out if the track were flipped?

    The report deliberately uses verdict_if_enabled and NOT verdict. Every track is off, so
    verdict answers TRACK_OFF for all of them - true, and useless. The pre-launch question has
    to be answerable BEFORE the risky action, not after it.
    """
    rows = bq.track_readiness()
    problems = [r for r in rows if r["verdict_if_enabled"] != READY]
    report["tracks_ready"] = sum(1 for r in rows if r["verdict_if_enabled"] == READY)
    report["tracks_not_ready"] = len(problems)
    # A list of problems, not a count. A count sends someone hunting.
    report["tracks_not_ready_json"] = json.dumps(
        [{"track": r["track"], "email_type": r["email_type"], "people": int(r["people"]),
          "template_id": r["template_id"], "enabled": bool(r["track_enabled"]),
          "verdict": r["verdict_if_enabled"]} for r in problems],
        ensure_ascii=False)
    for r in rows:
        log.info("READINESS track=%s email_type=%s people=%s enabled=%s template=%s "
                 "brevo_active=%s if_enabled=%s now=%s", r["track"], r["email_type"], r["people"],
                 r["track_enabled"], r["template_id"], r["brevo_active"],
                 r["verdict_if_enabled"], r["verdict"])
    live = [r for r in rows if r["track_enabled"] and r["verdict"] != READY]
    if live:
        log.error("ENABLED_TRACK_NOT_READY n=%s - %s", len(live),
                  ", ".join(f"{r['track']}:{r['verdict']}" for r in live))


def step4_suppression_list(brevo, report):
    """email_suppression_all -> list 4. Add-only, by design."""
    target = bq.suppressed_present_in_brevo()
    current = bq.list_members(C.SUPPRESSION_LIST_ID)
    to_add = sorted(target - current)
    would_remove = sorted(current - target)
    # would_* is what the run DECIDED, in both modes. added/removed is what it EXECUTED.
    # Keeping only the second (until 2026-09-03) made a dry run report three zeros that read
    # as "nothing to do" when they meant "did nothing" - see FINDING G in the pavediens.
    report.update(suppression_target=len(target), suppression_current=len(current),
                  suppression_would_add=len(to_add), suppression_would_remove=len(would_remove),
                  suppression_added=0)
    log.info("SUPPRESSION list=%s target=%s current=%s +%s (would_remove=%s, never executed)",
             C.SUPPRESSION_LIST_ID, len(target), len(current), len(to_add), len(would_remove))
    if would_remove:
        log.warning("SUPPRESSION_REMOVALS_IGNORED n=%s - an address left the suppression set. "
                    "That is not routine; look at why before acting on it.", len(would_remove))
    if C.DRY_RUN:
        log.info("[DRY] suppression list unchanged")
        return
    if to_add:
        report["suppression_added"] = brevo.list_add(C.SUPPRESSION_LIST_ID, to_add)


def step5_akcija_residual(brevo, report):
    """The weekly residual -> list 65, filtered against suppression BEFORE the write."""
    target = bq.akcija_residual()
    # Guard at the door (Raivis, 2026-09-02): every list this job writes is filtered against
    # suppression before the write, never cleaned up afterwards.
    sup = bq.suppressed()
    leaked = target & sup
    if leaked:
        raise Hold(f"RESIDUAL_SUPPRESSED_LEAK n={len(leaked)} - the residual view returned "
                   f"suppressed addresses. Fix the view, do not filter here and continue.")
    current = bq.list_members(C.AKCIJA_LIST_ID)
    to_add = sorted(target - current)
    to_remove = sorted(current - target)
    report.update(akcija_target=len(target), akcija_current=len(current),
                  akcija_would_add=len(to_add), akcija_would_remove=len(to_remove),
                  akcija_added=0, akcija_removed=0)
    log.info("AKCIJA list=%s target=%s current=%s +%s -%s",
             C.AKCIJA_LIST_ID, len(target), len(current), len(to_add), len(to_remove))
    # PART H1.3 and H4: the orphan count is a TRACKED number, not a log line. The cutover
    # watches it shrink as the non-buyer path lands, and "the orphan count moving in the wrong
    # direction" is a stop condition.
    breakdown = bq.akcija_breakdown()
    report["akcija_orphans"] = sum(r["addresses"] for r in breakdown if r["track"] == "orphan")
    report["akcija_held_by_enabled"] = sum(r["addresses"] for r in breakdown if r["enabled"])
    report["akcija_breakdown_json"] = json.dumps(
        [{"track": r["track"], "enabled": bool(r["enabled"]), "addresses": int(r["addresses"]),
          "residual_if_flipped": int(r["residual_if_flipped"])} for r in breakdown],
        ensure_ascii=False)
    for r in breakdown:
        log.info("AKCIJA_BREAKDOWN track=%s enabled=%s addresses=%s residual_if_flipped=%s",
                 r["track"], r["enabled"], r["addresses"], r["residual_if_flipped"])
    if current and len(to_remove) > C.MAX_REMOVE_MIN and len(to_remove) > C.MAX_REMOVE_FRAC * len(current):
        raise Hold(f"AKCIJA_MASS_REMOVAL n={len(to_remove)} of {len(current)} - that is a broken "
                   f"audience, not a real shrink. Nothing written.")
    if C.DRY_RUN:
        log.info("[DRY] akcija list unchanged")
        return
    if to_add:
        report["akcija_added"] = brevo.list_add(C.AKCIJA_LIST_ID, to_add)
    if to_remove:
        report["akcija_removed"] = brevo.list_remove(C.AKCIJA_LIST_ID, to_remove)


def step6_variant_lists(brevo, report):
    """One Brevo list per variant, materialised from the plan. Phase 3, this node's half.

    The plan is READ from mkt_control.variant_list_plan and never recomputed here - it is the
    sender's builder's answer to "who is in this variant", and a second answer is the defect
    class this system removed twice this month.

    The four checks below are the guarantees asked for in _INBOX on 09.09, and they are asserted
    for the WHOLE send_date rather than per list. A plan failing one of them is broken AS A PLAN;
    materialising the lists that happen to pass would leave a half-applied audience nobody can
    reason about, which is harder to recover from than writing nothing.

    This step reports into its OWN table. `report` is loaded into snapshot_run_report, which has
    a fixed 25-column schema, and appending fields to it from a JSON load would fail the whole
    run-report writer - a new step must not be able to break the reporting of the five that came
    before it. Widening that table is a deliberate schema change, not a side effect of this one.
    """
    vrep = {"run_id": RUN_ID, "started_at": _now().isoformat(), "dry_run": C.DRY_RUN,
            "status": "running", "send_dates": None, "lists": 0, "would_add": 0,
            "would_remove": 0, "forced_remove": 0, "held_add": 0, "held_remove": 0,
            "lists_written": 0, "suppressed_in_plan": 0, "unnormalised_in_plan": 0,
            "detail_json": None, "error": None}
    try:
        if not C.VARIANT_LISTS:
            vrep["status"] = "disabled"
            log.info("VARIANT_LISTS disabled")
            return
        dates = bq.variant_plan_send_dates()
        if not dates:
            # Expected until the sender's builder writes a plan. Deliberately quiet: an alarm
            # that fires every night about a known-absent dependency trains everyone to ignore
            # it, and then it fires for a real reason.
            vrep["status"] = "skipped_no_plan"
            log.info("VARIANT_NO_PLAN - no planned rows, nothing to materialise")
            return
        vrep["send_dates"] = ", ".join(str(d) for d in dates)

        owned = bq.owned_list_ids()
        sup = bq.suppressed()
        per_list = []

        for d in dates:
            rows = bq.variant_plan(d)
            snaps = {r["snapshot_id"] for r in rows}
            if len(snaps) > 1:
                raise Hold(f"VARIANT_PLAN_MID_WRITE send_date={d} snapshots={sorted(snaps)} - "
                           f"the plan is being rewritten. Read now and a partial audience reads "
                           f"as a real shrink, which is what the shrink floor exists to catch.")
            if any(r["brevo_list_id"] is None for r in rows):
                raise Hold(f"VARIANT_PLAN_NULL_LIST send_date={d} - a planned row with no list.")
            unowned = sorted({int(r["brevo_list_id"]) for r in rows} - owned)
            if unowned:
                raise Hold(f"VARIANT_PLAN_UNOWNED_LIST send_date={d} lists={unowned} - not in "
                           f"mkt_control.list_plan. Creating the list would invent an audience "
                           f"and writing to someone else's would be worse.")
            seen, dup = set(), 0
            for r in rows:
                k = (r["brevo_list_id"], r["master_key"])
                dup += k in seen
                seen.add(k)
            if dup:
                raise Hold(f"VARIANT_PLAN_GRAIN send_date={d} duplicate_rows={dup} - the plan "
                           f"must hold one row per (send_date, brevo_list_id, master_key). Two "
                           f"rows for one person make a duplicate indistinguishable from a real "
                           f"second membership, and the floors then divide by a wrong "
                           f"denominator.")

            unnormalised = sum(1 for r in rows if r["email_raw"] != r["email"])
            vrep["unnormalised_in_plan"] += unnormalised
            if unnormalised:
                log.warning("VARIANT_PLAN_UNNORMALISED n=%s - addresses not lowercased/trimmed. "
                            "Normalised here, but our counts and the planner's will disagree "
                            "until it is fixed at the source.", unnormalised)

            leaked = sorted({r["email"] for r in rows} & sup)
            vrep["suppressed_in_plan"] += len(leaked)
            if leaked:
                # Dropped, never sent - but said out loud. Silently compensating for a missing
                # filter upstream is how the filter stays missing.
                log.error("PLAN_CONTAINED_SUPPRESSED send_date=%s n=%s - opted-out addresses in "
                          "the membership plan. Dropped here; fix it where the plan is built.",
                          d, len(leaked))

            by_list = {}
            for r in rows:
                if r["email"] in sup:
                    continue
                by_list.setdefault(int(r["brevo_list_id"]), set()).add(r["email"])
            all_planned = set().union(*by_list.values()) if by_list else set()

            for lid, target in sorted(by_list.items()):
                current = bq.list_members(lid)
                ch = variant_lists.plan_list_change(
                    current, target, sup, all_planned - target,
                    C.GROWTH_MIN, C.GROWTH_FRAC, C.SHRINK_MIN, C.SHRINK_FRAC)
                log.info("VARIANT list=%s send_date=%s target=%s current=%s +%s -%s "
                         "(forced=%s held_add=%s held_remove=%s floors=%s/%s first_fill=%s)",
                         lid, d, ch["target"], ch["current"], len(ch["to_add"]),
                         len(ch["to_remove"]), len(ch["forced_remove"]), len(ch["held_add"]),
                         len(ch["held_remove"]), ch["growth_floor"], ch["shrink_floor"],
                         ch["first_fill"])
                per_list.append({"send_date": str(d), "brevo_list_id": lid,
                                 "snapshot_id": sorted(snaps)[0] if snaps else None,
                                 "target": ch["target"], "current": ch["current"],
                                 "would_add": len(ch["to_add"]),
                                 "would_remove": len(ch["to_remove"]),
                                 "forced_remove": len(ch["forced_remove"]),
                                 "held_add": len(ch["held_add"]),
                                 "held_remove": len(ch["held_remove"]),
                                 "first_fill": ch["first_fill"]})
                vrep["would_add"] += len(ch["to_add"])
                vrep["would_remove"] += len(ch["to_remove"])
                vrep["forced_remove"] += len(ch["forced_remove"])
                vrep["held_add"] += len(ch["held_add"])
                vrep["held_remove"] += len(ch["held_remove"])
                if C.DRY_RUN:
                    continue
                if ch["to_add"]:
                    brevo.list_add(lid, ch["to_add"])
                if ch["to_remove"]:
                    brevo.list_remove(lid, ch["to_remove"])
                if ch["to_add"] or ch["to_remove"]:
                    vrep["lists_written"] += 1

        vrep["lists"] = len(per_list)
        vrep["detail_json"] = json.dumps(per_list, ensure_ascii=False)
        if per_list and vrep["would_add"] == 0 and vrep["would_remove"] == 0:
            # Rows were planned and not one list moved. Distinct from "no plan" on purpose: this
            # is the state nobody would otherwise notice, and the one worth waking someone for.
            vrep["status"] = "wrote_nothing"
            log.error("VARIANT_WROTE_NOTHING lists=%s - a plan exists and no list changed. "
                      "Either every list is already exact, or every change sat under a floor.",
                      len(per_list))
        else:
            vrep["status"] = "ok"
    except Hold as e:
        vrep["status"] = "hold"
        vrep["error"] = str(e)[:1000]
        raise
    except Exception as e:                                        # noqa: BLE001
        vrep["status"] = "error"
        vrep["error"] = repr(e)[:1000]
        raise
    finally:
        vrep["finished_at"] = _now().isoformat()
        try:
            bq.write_variant_report(vrep)
        except Exception:                                         # noqa: BLE001
            log.exception("variant run report write failed")


def main():
    report = {"run_id": RUN_ID, "started_at": _now().isoformat(), "dry_run": C.DRY_RUN}
    try:
        brevo = BrevoContactsClient(C.BREVO_API_KEY)
        report["contacts"] = step1_snapshot(brevo)
        step2_template_status(brevo, report)
        step3_readiness(report)
        step4_suppression_list(brevo, report)
        step5_akcija_residual(brevo, report)
        step6_variant_lists(brevo, report)
        report.update(status="ok", finished_at=_now().isoformat())
        bq.write_report(report)
        log.info("RUN_OK %s", report)
        return 0
    except Hold as e:
        report.update(status="hold", error=str(e), finished_at=_now().isoformat())
        try:
            bq.write_report(report)
        finally:
            log.error("DELIBERATE_STOP %s", e)
        return EXIT_DELIBERATE_STOP
    except Exception as e:                                    # noqa: BLE001
        report.update(status="error", error=repr(e)[:1000], finished_at=_now().isoformat())
        try:
            bq.write_report(report)
        finally:
            log.exception("RUN_ERROR")
        return 1


if __name__ == "__main__":
    sys.exit(main())
