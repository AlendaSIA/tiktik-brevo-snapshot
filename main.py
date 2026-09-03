"""The only job that holds a Brevo credential.

Why it exists (Raivis, 2026-09-02): so that tiktik-marketing-sender holds NO Brevo credential
at all. An unbound key leaves the sender able to send and merely without a password - one
config mistake away from an accident. A sender with no sending code cannot send. This job takes
the credential so the sender does not have to.

What it does, and nothing else:
  1. exports Brevo contacts -> business_marts.brevo_contacts_snapshot (straight into BigQuery)
  2. refreshes Brevo template isActive -> mkt_control.brevo_template_status
  3. syncs email_suppression_all -> Brevo list 4 (tiktik_suppression), ADD-ONLY
  4. writes the weekly akcija residual -> Brevo list 65 (tiktik_akcija_atlikums)
  5. filters every list it writes against email_suppression_all BEFORE writing

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
from brevo_client import BrevoContactsClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("snapshot")

RUN_ID = os.environ.get("CLOUD_RUN_EXECUTION") or f"local-{uuid.uuid4().hex[:12]}"
EXIT_DELIBERATE_STOP = 3

# A track is ready only when permission, fitness and deliverability all agree. Everything else
# is a named problem, never a silent zero.
READY = "READY"
NOT_A_PROBLEM = ("READY", "TRACK_OFF")


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
    """Per-track verdict, computed by the view both this report and the sender read."""
    rows = bq.track_readiness()
    problems = [r for r in rows if r["verdict"] not in NOT_A_PROBLEM]
    report["tracks_ready"] = sum(1 for r in rows if r["verdict"] == READY)
    report["tracks_not_ready"] = len(problems)
    # A list of problems, not a count. A count sends someone hunting.
    report["tracks_not_ready_json"] = json.dumps(
        [{"track": r["track"], "email_type": r["email_type"], "people": int(r["people"]),
          "template_id": r["template_id"], "verdict": r["verdict"]} for r in problems],
        ensure_ascii=False)
    for r in rows:
        log.info("READINESS track=%s email_type=%s people=%s enabled=%s template=%s "
                 "brevo_active=%s verdict=%s", r["track"], r["email_type"], r["people"],
                 r["track_enabled"], r["template_id"], r["brevo_active"], r["verdict"])
    enabled_problems = [r for r in problems if r["track_enabled"]]
    if enabled_problems:
        log.error("ENABLED_TRACK_NOT_READY n=%s - %s", len(enabled_problems),
                  ", ".join(f"{r['track']}:{r['verdict']}" for r in enabled_problems))


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


def main():
    report = {"run_id": RUN_ID, "started_at": _now().isoformat(), "dry_run": C.DRY_RUN}
    try:
        brevo = BrevoContactsClient(C.BREVO_API_KEY)
        report["contacts"] = step1_snapshot(brevo)
        step2_template_status(brevo, report)
        step3_readiness(report)
        step4_suppression_list(brevo, report)
        step5_akcija_residual(brevo, report)
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
