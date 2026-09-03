"""The Brevo client, and the reason this file is small.

This job holds a Brevo API key. A Brevo v3 key CANNOT be scoped - Brevo grants a key full
account access, so the key itself does not stop this job from sending e-mail. The limit is
therefore carried by the CODE, and this file is that limit.

Raivis, 2026-09-02: "the limit being carried by the code must not mean a comment." So the
client exposes EXACTLY three operations - export contacts, add to a list, remove from a list -
and the surface is asserted at import time. There is no send method to find. Anyone who wants
this job to send mail has to write that capability deliberately; they cannot inherit it.

A genuinely scoped credential does exist via Brevo OAuth (contacts:read / contacts:write are
separate scopes from transactional.email:write). It was NOT chosen because a refreshing token
is a moving part, and moving parts in this system tend to stop quietly - while the marginal
protection is small, since the SENDER holds no Brevo credential either way. If this job ever
needs to grow, OAuth with contacts scopes is the path; that decision is recorded rather than
forgotten. See the registry card blk-brevo-contacts-snapshot.
"""
import json
import logging
import time

import requests

log = logging.getLogger("brevo")

BASE = "https://api.brevo.com/v3"
ALLOWED_OPERATIONS = ("export_contacts_ndjson", "list_add", "list_remove")


class BrevoContactsClient:
    """Contacts only. Three operations. No sending."""

    def __init__(self, api_key: str):
        if not api_key:
            raise RuntimeError("BREVO_API_KEY is not set")
        self._key = api_key

    # -- the only place an HTTP call is made ---------------------------------
    def _call(self, method: str, path: str, **kw):
        url = f"{BASE}{path}"
        for attempt in range(3):
            r = requests.request(
                method, url, timeout=120,
                headers={"api-key": self._key, "content-type": "application/json",
                         "accept": "application/json"}, **kw)
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 ** attempt)
                continue
            return r
        return r

    # -- 1 -------------------------------------------------------------------
    def export_contacts_ndjson(self) -> bytes:
        """All contacts as newline-delimited JSON: email, list_ids, dates, subscribed, blocklisted."""
        out = []
        offset, limit = 0, 1000
        while True:
            r = self._call("GET", "/contacts",
                           params={"limit": limit, "offset": offset,
                                   "modifiedSince": "2000-01-01T00:00:00Z"})
            if r.status_code != 200:
                raise RuntimeError(f"contacts export failed {r.status_code}: {r.text[:300]}")
            cs = r.json().get("contacts", [])
            if not cs:
                break
            for c in cs:
                out.append(json.dumps({
                    "email": (c.get("email") or "").strip().lower(),
                    "list_ids": c.get("listIds") or [],
                    "added_time": (c.get("createdAt") or "")[:10] or None,
                    "modified_time": (c.get("modifiedAt") or "")[:10] or None,
                    "email_subscribed": not bool(c.get("emailBlacklisted")),
                    "email_blocklisted": bool(c.get("emailBlacklisted")),
                }, ensure_ascii=False))
            if len(cs) < limit:
                break
            offset += limit
            if offset % 10000 == 0:
                log.info("exported %s contacts so far", offset)
        log.info("CONTACTS_EXPORTED n=%s", len(out))
        return ("\n".join(out) + "\n").encode("utf-8")

    # -- 2 -------------------------------------------------------------------
    def list_add(self, list_id: int, emails: list) -> int:
        return self._list_op(list_id, "add", emails)

    # -- 3 -------------------------------------------------------------------
    def list_remove(self, list_id: int, emails: list) -> int:
        return self._list_op(list_id, "remove", emails)

    def _list_op(self, list_id: int, op: str, emails: list) -> int:
        done = 0
        for i in range(0, len(emails), 150):          # Brevo caps this endpoint at 150
            chunk = emails[i:i + 150]
            r = self._call("POST", f"/contacts/lists/{list_id}/contacts/{op}",
                           data=json.dumps({"emails": chunk}))
            if r.status_code not in (200, 201, 204):
                raise RuntimeError(f"list {list_id} {op} failed {r.status_code}: {r.text[:300]}")
            done += len(chunk)
        log.info("LIST_%s list=%s n=%s", op.upper(), list_id, done)
        return done


def _assert_surface():
    """The limit is a shape, not a promise. If someone adds a fourth operation, this fails."""
    public = {n for n in dir(BrevoContactsClient)
              if not n.startswith("_") and callable(getattr(BrevoContactsClient, n))}
    if public != set(ALLOWED_OPERATIONS):
        raise RuntimeError(
            "BrevoContactsClient surface changed: %s. This client is deliberately limited to "
            "%s. Adding an operation here is a decision about what this job can do to the "
            "Brevo account - make it on purpose, and update the registry card "
            "blk-brevo-contacts-snapshot." % (sorted(public), sorted(ALLOWED_OPERATIONS)))


_assert_surface()
