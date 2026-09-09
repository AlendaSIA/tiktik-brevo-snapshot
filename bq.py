"""Every statement this job runs, in one file, so the data contract is readable at a glance.

The audience SQL is deliberately absent: it lives in the BigQuery views
mkt_control.akcija_residual, mkt_control.akcija_audience_impact and
mkt_control.track_send_readiness, which the pre-launch report and the sender read too.
One definition, one place.
"""
import io
import logging

from google.cloud import bigquery

import config as C

log = logging.getLogger("bq")
_client = None

SNAPSHOT_SCHEMA = [
    bigquery.SchemaField("email", "STRING"),
    bigquery.SchemaField("list_ids", "INTEGER", mode="REPEATED"),
    bigquery.SchemaField("added_time", "DATE"),
    bigquery.SchemaField("modified_time", "DATE"),
    bigquery.SchemaField("email_subscribed", "BOOL"),
    bigquery.SchemaField("email_blocklisted", "BOOL"),
]

TEMPLATE_STATUS_SCHEMA = [
    bigquery.SchemaField("template_id", "INTEGER"),
    bigquery.SchemaField("name", "STRING"),
    bigquery.SchemaField("subject", "STRING"),
    bigquery.SchemaField("is_active", "BOOL"),
    bigquery.SchemaField("checked_at", "TIMESTAMP"),
]

# The columns this job reads out of the readiness view. Listed, not SELECT *, so a view change
# fails loudly here instead of quietly changing what the report means.
READINESS_COLUMNS = ("track", "email_type", "people", "track_enabled", "template_id",
                     "sendable", "brevo_active", "verdict_if_enabled", "verdict")


def client():
    global _client
    if _client is None:
        _client = bigquery.Client(project=C.PROJECT, location=C.LOCATION)
    return _client


def q(sql, params=None):
    cfg = bigquery.QueryJobConfig(query_parameters=params or [])
    return list(client().query(sql, job_config=cfg).result())


def scalar(sql, params=None):
    rows = q(sql, params)
    return None if not rows else list(rows[0].values())[0]


def load_snapshot(ndjson: bytes) -> int:
    """Load the Brevo export straight from memory into BigQuery. No GCS staging.

    Removed 2026-09-03 after the first dry run failed on storage.objects.create: the bucket hop
    was a dependency and a permission this job never needed.
    """
    job = client().load_table_from_file(
        io.BytesIO(ndjson), C.T_SNAPSHOT,
        job_config=bigquery.LoadJobConfig(
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
            schema=SNAPSHOT_SCHEMA))
    job.result()
    return int(scalar(f"SELECT COUNT(*) FROM `{C.T_SNAPSHOT}`"))


def load_template_status(rows) -> int:
    """Replace mkt_control.brevo_template_status with what Brevo says right now.

    WRITE_TRUNCATE on purpose: a template that disappeared from Brevo must disappear here too,
    or track_send_readiness would keep answering from a template that no longer exists.
    """
    job = client().load_table_from_json(
        rows, C.T_TEMPLATE_STATUS,
        job_config=bigquery.LoadJobConfig(
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
            schema=TEMPLATE_STATUS_SCHEMA))
    job.result()
    return len(rows)


# --------------------------------------------------------------------------- #
# List membership is read from the SNAPSHOT we just refreshed, not from a fourth
# Brevo call. One export answers "who is on which list" for every list at once.
# --------------------------------------------------------------------------- #
def list_members(list_id):
    return {r["email"] for r in q(
        f"SELECT DISTINCT LOWER(TRIM(email)) AS email FROM `{C.T_SNAPSHOT}` "
        f"WHERE @lid IN UNNEST(list_ids) AND email IS NOT NULL",
        [bigquery.ScalarQueryParameter("lid", "INT64", list_id)])}


def suppressed():
    return {r["email"] for r in q(
        f"SELECT DISTINCT LOWER(TRIM(email)) AS email FROM {C.T_SUPPRESSION} WHERE email IS NOT NULL")}


def suppressed_present_in_brevo():
    """Suppressed addresses that Brevo actually holds.

    The 1 809 suppressed addresses Brevo does not hold are deliberately NOT created (Raivis,
    2026-09-02): a contact Brevo does not have cannot be mailed, the risk only exists at an
    import, and the guard belongs there - see _HOWTO-before-any-brevo-import.md.
    """
    return {r["email"] for r in q(
        f"SELECT DISTINCT LOWER(TRIM(s.email)) AS email FROM {C.T_SUPPRESSION} s "
        f"JOIN `{C.T_SNAPSHOT}` b ON b.email = LOWER(TRIM(s.email)) WHERE s.email IS NOT NULL")}


def akcija_residual():
    """The weekly akcija audience. Definition lives in the view, not here."""
    return {r["email"] for r in q(f"SELECT email FROM {C.V_RESIDUAL}")}


def akcija_breakdown():
    """Per-track composition and what flipping each track would do to the audience.

    PART H1.3 makes the orphan count a dry-run acceptance criterion and H4 makes it moving in
    the wrong direction a stop condition, so it is stored, not logged.
    """
    return [dict(r) for r in q(
        f"SELECT track, enabled, addresses, residual_now, residual_if_flipped FROM {C.V_IMPACT}")]


def track_readiness():
    """Per track x email_type for the current week: can this letter actually go out.

    Read AFTER the template status refresh, so the verdict reflects Brevo as of this run and
    not as of yesterday. The verdict logic lives in the view, shared with the sender.
    """
    cols = ", ".join(READINESS_COLUMNS)
    return [dict(r) for r in q(f"SELECT {cols} FROM {C.V_READINESS} ORDER BY people DESC")]


def write_report(rec):
    client().load_table_from_json([rec], C.T_REPORT).result()


# --------------------------------------------------------------------------- #
# Phase 3: the variant membership plan. READ ONLY. Every function here selects
# from mkt_control.variant_list_plan; none of them recomputes who belongs where.
# --------------------------------------------------------------------------- #
def variant_plan_send_dates():
    """Distinct send_date values currently carrying planned rows."""
    return [r["send_date"] for r in q(
        f"SELECT DISTINCT send_date FROM {C.V_VARIANT_PLAN} ORDER BY send_date")]


def variant_plan(send_date):
    """The planned rows for one send_date: list, person, address, and the snapshot they came from."""
    return [dict(r) for r in q(
        f"SELECT brevo_list_id, master_key, LOWER(TRIM(email)) AS email, email AS email_raw, "
        f"       email_type, track, template_id, snapshot_id "
        f"FROM {C.V_VARIANT_PLAN} WHERE send_date = @d",
        [bigquery.ScalarQueryParameter("d", "DATE", send_date)])]


def owned_list_ids():
    """Brevo list ids this system is allowed to write, from the control table.

    A plan row pointing at a list we do not own is the one case that cannot be made safe by
    filtering: creating the list would invent an audience, and writing to someone else's list
    would be worse. It is refused.
    """
    return {int(r["brevo_list_id"]) for r in q(
        f"SELECT DISTINCT brevo_list_id FROM {C.T_LIST_PLAN} WHERE brevo_list_id IS NOT NULL")}


def write_variant_report(rec):
    client().load_table_from_json([rec], C.T_VARIANT_REPORT).result()
