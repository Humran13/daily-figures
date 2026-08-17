"""
Web Push delivery. No Web Push infrastructure existed anywhere in this
codebase before this round (confirmed by inspection — no PushManager/
VAPID/push-event code anywhere). This module is deliberately built so
every function EXCEPT actual delivery works with zero extra dependencies
and zero configuration:

  - subscribing/unsubscribing/storing PushSubscription rows always works.
  - is_configured() is the one gate: it's only True when BOTH
    VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY are present in the environment
    (never hardcoded, never committed — see .env.example). Missing
    configuration is a graceful no-op everywhere, never an error — the
    in-app Requests badge/nav-count is the guaranteed fallback regardless
    of whether push is configured or delivered at all.
  - the `pywebpush` package (needed only to actually send a message) is
    imported lazily inside _send_to_subscription() — its absence (e.g. in
    this dev/test environment, where it is intentionally not installed)
    never breaks subscribing, unsubscribing, or any other part of the app;
    it only means delivery itself no-ops, exactly like missing VAPID
    configuration does.

Deployment still needs, beyond `pip install pywebpush` (already added to
requirements.txt):
  - VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY (a real VAPID keypair — see
    pywebpush's own `vapid.py` or `web-push generate-vapid-keys` for how
    to generate one; never generated or hardcoded by this code).
  - VAPID_SUBJECT (a mailto: or https: contact URL some push services
    require — defaults to a placeholder if unset, but should be a real
    contact for production).
See the completion report for the exact remaining deployment steps.
"""
import json
import logging
import os

from webapp.extensions import db
from webapp.models.push_subscription import PushSubscription

logger = logging.getLogger(__name__)


def is_configured():
    return bool(os.environ.get("VAPID_PUBLIC_KEY")) and bool(os.environ.get("VAPID_PRIVATE_KEY"))


def vapid_public_key():
    """None when push isn't configured — the frontend feature-detects on
    this being falsy and simply never offers the "Enable notifications"
    opt-in in that case, rather than attempting a subscribe that could
    only fail."""
    return os.environ.get("VAPID_PUBLIC_KEY") or None


class PushSubscriptionError(ValueError):
    """User-facing validation problem — mapped to HTTP 400 by routes."""


def save_subscription(user, subscription):
    """
    Stores (or refreshes) one browser/device's PushSubscription — user/
    role-associated via the authenticated session's own user id, never a
    client-supplied one. Re-subscribing the same device (same endpoint)
    updates the existing row rather than creating a duplicate.
    """
    endpoint = (subscription or {}).get("endpoint")
    keys = (subscription or {}).get("keys") or {}
    p256dh = keys.get("p256dh")
    auth = keys.get("auth")
    if not endpoint or not p256dh or not auth:
        raise PushSubscriptionError("incomplete push subscription")

    existing = PushSubscription.query.filter_by(endpoint=endpoint).first()
    if existing is not None:
        existing.user_id = user.id
        existing.p256dh = p256dh
        existing.auth = auth
        db.session.flush()
        return existing
    row = PushSubscription(user_id=user.id, endpoint=endpoint, p256dh=p256dh, auth=auth)
    db.session.add(row)
    db.session.flush()
    return row


def remove_subscription(endpoint):
    if not endpoint:
        return
    PushSubscription.query.filter_by(endpoint=endpoint).delete()
    db.session.flush()


def _send_to_subscription(subscription_row, title, body, url, badge_count=None):
    """
    Sends exactly one push message; NEVER raises — every failure mode
    (missing config, missing package, expired/revoked subscription, any
    transport error) is caught and logged, never propagated to the
    caller, since a failed/absent push must never break the request that
    triggered it (request creation / approval / rejection all already
    committed their own database change before this is ever called).

    badge_count: the authoritative pending-request count at send time
    (correction_request_service.pending_count()), included so the
    service worker's own `push` handler (static/sw.js) can call the
    Badging API from inside itself and keep the app icon badge accurate
    even while the PWA/browser tab is fully closed — the in-app nav
    badge only updates once the app is opened, which isn't "background"
    at all. None for notifications that aren't badge-relevant (e.g. an
    Operator's approve/reject notice).
    """
    if not is_configured():
        return False
    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        logger.info(
            "pywebpush is not installed — push notification not sent "
            "(the in-app Requests badge remains the guaranteed fallback)."
        )
        return False
    try:
        payload = {"title": title, "body": body, "url": url}
        if badge_count is not None:
            payload["badgeCount"] = badge_count
        webpush(
            subscription_info={
                "endpoint": subscription_row.endpoint,
                "keys": {"p256dh": subscription_row.p256dh, "auth": subscription_row.auth},
            },
            data=json.dumps(payload),
            vapid_private_key=os.environ["VAPID_PRIVATE_KEY"],
            vapid_claims={"sub": os.environ.get("VAPID_SUBJECT", "mailto:admin@example.com")},
        )
        return True
    except WebPushException as e:
        status_code = getattr(getattr(e, "response", None), "status_code", None)
        if status_code in (404, 410):
            # The browser/push service says this subscription is gone —
            # stop trying forever rather than erroring on every future
            # notification for a device that will never receive one again.
            db.session.delete(subscription_row)
        else:
            logger.warning("push delivery failed: %s", e)
        return False
    except Exception as e:  # never let a delivery failure break the caller
        logger.warning("push delivery failed: %s", e)
        return False


def notify_users(user_ids, title, body, url, badge_count=None):
    """
    Sends the same notification once to every subscription belonging to
    any of these users. A graceful no-op whenever push isn't configured,
    `pywebpush` isn't installed, or there are simply no subscriptions —
    the in-app badge is the guaranteed fallback regardless of any of
    that. Never called more than once per logical event by this
    codebase's own call sites (see notification_service.py), so this
    never needs its own duplicate-suppression logic.
    """
    if not user_ids or not is_configured():
        return
    subs = PushSubscription.query.filter(PushSubscription.user_id.in_(list(user_ids))).all()
    for sub in subs:
        _send_to_subscription(sub, title, body, url, badge_count=badge_count)
    db.session.commit()
