# tiktik-brevo-snapshot

**The only job that holds a Brevo credential.**

It exists so that `tiktik-marketing-sender` holds none. An unbound API key leaves the sender
*able* to send and merely without a password — one configuration mistake away from an accident.
A sender with no sending code cannot send at all. This job takes the credential so the sender
does not have to.

## What it does, and nothing else

1. Exports Brevo contacts → GCS → `business_marts.brevo_contacts_snapshot`.
2. Syncs `business_marts.email_suppression_all` → Brevo list **4** (`tiktik_suppression`). **Add-only.**
3. Writes the weekly akcija residual → Brevo list **65** (`tiktik_akcija_atlikums`).
4. Filters every list it writes against suppression **before** the write, never as a cleanup after.

## What it cannot do

Send. `brevo_client.py` exposes exactly three operations — export contacts, list add, list remove —
and asserts that surface at import time. Adding a fourth fails the import with a message saying so.

A Brevo v3 API key **cannot be scoped** (Brevo grants a key full account access), so the limit is
carried by the CODE, not by the credential. That is a deliberate, recorded choice, not an oversight.
A genuinely scoped credential exists via Brevo OAuth (`contacts:read`/`contacts:write` are separate
from `transactional.email:write`); it was not chosen because a refreshing token is a moving part in
a job whose whole value is being boringly reliable, and the sender ends up with no Brevo credential
either way. **If this job ever needs to grow, OAuth with contacts scopes is the path.**

## Where the definitions live

The audience SQL is **not** in this repo. It reads two BigQuery views so there is one definition,
shared with the pre-launch report:

- `mkt_control.akcija_residual` — every sendable member of list 3 not held by an **enabled** track.
  Orphans stay in. Computed at master level.
- `mkt_control.akcija_audience_impact` — what flipping one track does to the akcija audience.

## Safety defaults

`DRY_RUN=true` unless explicitly set to `false`. Dry run computes and reports everything and writes
nothing to Brevo. The snapshot refresh still runs in a dry run — it is a read from Brevo and a write
to our own BigQuery, and it is what the sender's freshness guard depends on.

Exit codes: `0` ran and reported · `1` crash · `3` deliberate stop. A hold must never look like a
breakage — the 03:00 job exited 1 for 57 nights and nobody could tell the difference.

## Home

Tree: `Company-Alenda-SIA/shared-platforms/03-data-analytics.md` ·
pavediens `_pavediens--sender-cutover-switch.md` · registry card `blk-brevo-contacts-snapshot` ·
Pipedrive project 99, task #458.
