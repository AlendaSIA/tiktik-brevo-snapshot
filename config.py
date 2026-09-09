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

# Brevo template isActive, refreshed here because this job holds the key and the sender does
# not. The sender reads the TABLE, never Brevo. 2026-09-03.
T_TEMPLATE_STATUS = f"{PROJECT}.{CONTROL}.brevo_template_status"

# The audience definitions live in BigQuery views, NOT in this repo. One definition, shared with
# the pre-launch report - copying the SQL here would be a second place for one fact.
V_RESIDUAL    = f"`{PROJECT}.{CONTROL}.akcija_residual`"
V_IMPACT      = f"`{PROJECT}.{CONTROL}.akcija_audience_impact`"
V_READINESS   = f"`{PROJECT}.{CONTROL}.track_send_readiness`"

# --- Phase 3: one Brevo list per variant -----------------------------------------
# The membership plan is READ, never recomputed. mkt_control.variant_list_plan is a view over
# campaign_audience_snapshot WHERE dispatch_state='planned', owned by the sender's builder.
# Recomputing it here would make two answers to "who is in this variant", which is the defect
# class this system removed on 04.09 (two template mappings) and again on 09.09 (a second
# pricing path). One answer, read from where it is decided.
VARIANT_LISTS   = os.environ.get("VARIANT_LISTS", "true").lower() != "false"
V_VARIANT_PLAN  = f"`{PROJECT}.{CONTROL}.variant_list_plan`"
T_LIST_PLAN     = f"`{PROJECT}.{CONTROL}.list_plan`"

# Asymmetric drift floors. A FLOOR is the smallest drift worth acting on: below it the list is
# left alone rather than churned for noise. Shrinking needs a louder signal than growing,
# because a wrong audience shows up as a mass removal.
GROWTH_MIN   = int(os.environ.get("GROWTH_MIN", "10"))
GROWTH_FRAC  = float(os.environ.get("GROWTH_FRAC", "0.005"))
SHRINK_MIN   = int(os.environ.get("SHRINK_MIN", "5"))
SHRINK_FRAC  = float(os.environ.get("SHRINK_FRAC", "0.03"))

# "batch 1 %" was given as a third parameter and is deliberately NOT implemented. Two readings
# fit the words - a write batch size, or a per-run cap on how much of a list may change - and
# they behave differently in the case that matters (a first materialisation, where a cap would
# block the fill entirely). A guard whose meaning nobody can state is the same defect as an
# alarm that is always on: it looks like safety and carries none. Asked in _INBOX.md 09.09;
# it will be implemented when it means something. Until then this value is inert on purpose.
BATCH_FRAC_UNIMPLEMENTED = float(os.environ.get("BATCH_FRAC", "0.01"))

T_VARIANT_REPORT = f"{PROJECT}.{CONTROL}.variant_list_run_report"

# The global DRY_RUN is FALSE in production - this job writes to Brevo lists for real every
# night. A new step that writes would therefore go live the moment it deploys, with its first
# real plan as its first real run. VARIANT_DRY_RUN is a separate switch, defaulting to true, so
# the first plan can be read as would_add / would_remove numbers in
# mkt_control.variant_list_run_report before a single address moves. Turning it off is a
# decision somebody makes after reading a run, not a side effect of a merge.
VARIANT_DRY_RUN = os.environ.get("VARIANT_DRY_RUN", "true").lower() != "false"
