"""
Module-level feature flags for future commercial/white-label use. Not a
permission system — a flag only decides whether a module's routes/nav are
reachable at all right now. Disabling a module never touches its data:
every table it owns is completely untouched by this module, and
re-enabling immediately restores full access to whatever was already
there. See webapp/auth.py's feature_required decorator for the backend
enforcement side.

Dependency rules (a small, hand-maintained map — "keep the implementation
simple" per the original request):

- Dispatch requires Customer Management (recipient search/selection uses
  the customer admin API).
- Daily Figures requires Dispatch (Issued totals are derived from
  finalized Dispatch records).
- Reporting requires both Dispatch and Daily Figures (its two report
  types — recipient totals, date-range summary — draw from each
  respectively).
- History & Exports has no hard dependency: its two sections (Dispatch
  History, Daily Figures History) are independently sourced, and the
  frontend already hides/shows each section on its own rather than the
  whole module needing to come down when only one source is off.
- Dashboard has no hard dependency for the same reason: it degrades
  (fewer populated panels) rather than becoming actively broken when a
  data source module is off.

A disable/enable is never allowed to leave the flag set in a state where
an enabled module depends on a disabled one:
  - Enabling a module whose dependencies aren't all enabled is rejected
    outright — nothing is silently enabled on the caller's behalf.
  - Disabling a module that an enabled module (transitively) depends on
    is rejected by default, naming every affected module; the caller may
    resend with cascade=True to atomically disable the module and all of
    its (transitive) dependents together, in one transaction, each
    individually audited.
"""
from webapp.extensions import db
from webapp.models.feature_flag import MODULE_LABELS, MODULES, FeatureFlag
from webapp.services.audit_service import record_audit

REQUIRES = {
    "dispatch": ["customer_management"],
    "daily_figures": ["dispatch"],
    "reporting": ["dispatch", "daily_figures"],
}


class FeatureFlagError(ValueError):
    pass


def get_all_flags():
    """Every module, defaulting a missing row to enabled=True — a module
    that somehow has no row yet must never silently appear disabled."""
    existing = {f.module_key: f for f in FeatureFlag.query.all()}
    result = []
    for module_key in MODULES:
        flag = existing.get(module_key)
        if flag is None:
            flag = FeatureFlag(module_key=module_key, enabled=True)
            db.session.add(flag)
            db.session.flush()
        result.append(flag)
    return result


def is_enabled(module_key):
    flag = FeatureFlag.query.filter_by(module_key=module_key).first()
    return True if flag is None else flag.enabled  # missing row -> safe default: enabled


def _label(module_key):
    return MODULE_LABELS.get(module_key, module_key)


def _all_dependents(module_key):
    """Every module that (directly or transitively) requires module_key —
    not filtered by current enabled state, that's the caller's job."""
    dependents = set()
    queue = [module_key]
    while queue:
        current = queue.pop()
        for dependent, reqs in REQUIRES.items():
            if current in reqs and dependent not in dependents:
                dependents.add(dependent)
                queue.append(dependent)
    return dependents


def _apply_change(flag, enabled, user):
    before = flag.to_dict()
    flag.enabled = enabled
    flag.updated_by = user.id
    db.session.flush()
    after = flag.to_dict()
    record_audit(user, "update", "feature_flag", entity_id=flag.module_key, before=before, after=after)
    return flag


def set_flag(module_key, enabled, user, cascade=False):
    """
    Returns the list of FeatureFlag rows actually changed (length 1 unless
    cascade disabled more than one module). Raises FeatureFlagError,
    leaving every flag untouched, if the change would violate a
    dependency rule.
    """
    if module_key not in MODULES:
        raise FeatureFlagError(f"unknown module '{module_key}'")

    by_key = {f.module_key: f for f in get_all_flags()}
    flag = by_key[module_key]

    if flag.enabled == enabled:
        return []  # no-op, nothing to audit

    if enabled:
        missing = [req for req in REQUIRES.get(module_key, []) if not by_key[req].enabled]
        if missing:
            labels = ", ".join(_label(m) for m in missing)
            raise FeatureFlagError(
                f"Cannot enable '{_label(module_key)}' — required module(s) not enabled: {labels}. "
                f"Enable {'them' if len(missing) > 1 else 'it'} first."
            )
        return [_apply_change(flag, True, user)]

    # Disabling: block if any currently-enabled module (transitively)
    # depends on this one, unless the caller explicitly asked to cascade.
    dependents = sorted(
        d for d in _all_dependents(module_key) if by_key[d].enabled
    )
    if dependents and not cascade:
        labels = ", ".join(_label(m) for m in dependents)
        raise FeatureFlagError(
            f"Cannot disable '{_label(module_key)}' — the following enabled module(s) depend on it: "
            f"{labels}. Disable {'them' if len(dependents) > 1 else 'it'} first, or resend with "
            f"cascade: true to disable '{_label(module_key)}' and all of them together."
        )

    changed = [_apply_change(flag, False, user)]
    for dependent_key in dependents:
        changed.append(_apply_change(by_key[dependent_key], False, user))
    return changed
