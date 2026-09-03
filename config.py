"""Configuration. Safe defaults: it reads and reports; writing to Brevo lists is opt-in."""
import os

PROJECT   = os.environ.get("BQ_PROJECT", "jaunais-za-aizv04022026")
MARTS     = os.environ.get("BQ_MARTS", "business_marts")
CONTROL   = os.environ.get("BQ_CONTROL", "mkt_control")
LOCATION  = os.environ.get("BQ_LOCATION", "EU")

BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "")

# The snapshot refresh is a READ from Brevo and a write to our own BigQuery, so it runs even in
# a dry run: it is the thing the sender's SUPPRESSION_FRESH guard depends on.
#
# There is deliberately NO GCS staging. The first dry run (2026-09-03) failed because the runner
# could not write to the bucket, and the honest reading of that error was that the hop was never
# needed: the export goes from memory straight into BigQuery. One less dependency, one less
# permission, and no "which bucket" question for the next person.
REFRESH_SNAPSHOT = os.environ.get("REFRESH_SNAPSHOT", "true").lower() != "false"

# DRY_RUN governs ONLY the two Brevo list writes. Everything is computed and reported either way.
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() != "false"

SUPPRESSION_LIST_ID = int(os.environ.get("SUPPRESSION_LIST_ID", "4"))    # tiktik_suppression
AKCIJA_LIST_ID      = int(os.environ.get("AKCIJA_LIST_ID", "65"))        # tiktik_akcija_atlikums

# The suppression list is ADD-ONLY. Removing an address from a suppression list is never a
# routine operation, and a bug that empties it would be invisible in exactly the way that
# matters. Would-be removals are reported, never executed.
SUPPRESSION_ALLOW_REMOVE = False

# Safety on the akcija list: refuse a mass removal that looks like a broken audience rather
# than a real shrink. Same shape as the A6.1 reconcile's guard, for the same reason.
MAX_REMOVE_MIN  = int(os.environ.get("MAX_REMOVE_MIN", "500"))
MAX_REMOVE_FRAC = float(os.environ.get("MAX_REMOVE_FRAC", "0.5"))

T_SNAPSHOT    = f"{PROJECT}.{MARTS}.brevo_contacts_snapshot"
T_SUPPRESSION = f"`{PROJECT}.{MARTS}.email_suppression_all`"
T_REPORT      = f"{PROJECT}.{CONTROL}.snapshot_run_report"

# The audience definitions live in BigQuery views, NOT in this repo. One definition, shared with
# the pre-launch report - copying the SQL here would be a second place for one fact.
V_RESIDUAL    = f"`{PROJECT}.{CONTROL}.akcija_residual`"
V_IMPACT      = f"`{PROJECT}.{CONTROL}.akcija_audience_impact`"
