"""
Spare Part / Machine / Supplier master-data CRUD. Deactivate-only, no hard
delete — mirrors webapp/routes/admin_products.py's pattern exactly, since
a SparePart/Machine/Supplier referenced by any movement/association must
never disappear out from under historical data.
"""
import re

from webapp.extensions import db
from webapp.models.machine import Machine, MachineAlias
from webapp.models.spare_part import SparePart, SparePartMachine, SparePartSupplier, UNITS
from webapp.models.supplier import Supplier


class SparePartError(ValueError):
    pass


def normalize_text(value):
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).strip())


def normalize_key(value):
    return normalize_text(value).casefold()


def normalize_machine(raw_text, actor=None):
    """
    Finds-or-creates the canonical Machine for a raw machine string,
    normalizing only whitespace/case — never spelling. A value containing
    "/" or other separators (e.g. "np/tp") is kept as ONE literal alias
    rather than being split, since which machines it actually refers to
    can't be established automatically (see MachineAlias docstring).
    Returns None for a blank/missing raw_text.
    """
    raw = normalize_text(raw_text)
    if not raw:
        return None
    norm = raw.casefold()
    alias = MachineAlias.query.filter_by(normalized_text=norm).first()
    if alias is not None:
        return db.session.get(Machine, alias.machine_id)

    code = unique_machine_code(raw)
    machine = Machine(code=code, name=raw, active=True)
    db.session.add(machine)
    db.session.flush()
    db.session.add(MachineAlias(machine_id=machine.id, raw_text=raw, normalized_text=norm))
    db.session.flush()
    return machine


def is_ambiguous_machine_text(raw_text):
    raw = normalize_text(raw_text)
    return bool(re.search(r"[/,&]", raw))


def unique_machine_code(raw_text):
    base = re.sub(r"[^a-z0-9]+", "_", raw_text.casefold()).strip("_") or "machine"
    candidate = base
    n = 2
    while Machine.query.filter_by(code=candidate).first() is not None:
        candidate = f"{base}_{n}"
        n += 1
    return candidate


def find_or_create_supplier(name, building=None, contacts=None, notes=None):
    name = normalize_text(name)
    if not name:
        return None
    building_norm = normalize_key(building)
    candidates = Supplier.query.filter(db.func.lower(Supplier.name) == name.casefold()).all()
    for s in candidates:
        if normalize_key(s.building) == building_norm:
            return s
    supplier = Supplier(name=name, building=normalize_text(building) or None,
                         contacts=normalize_text(contacts) or None, notes=notes, active=True)
    db.session.add(supplier)
    db.session.flush()
    return supplier


def _assign_code(spare_part):
    spare_part.code = f"SP-{spare_part.id:05d}"


def create_spare_part(*, name, model=None, size=None, unit="pcs", minimum_stock=None,
                       location=None, notes=None, machine_ids=None):
    name = normalize_text(name)
    if not name:
        raise SparePartError("name is required")
    if unit and unit not in UNITS:
        raise SparePartError(f"unit must be one of {UNITS}")

    spare_part = SparePart(
        name=name, model=normalize_text(model) or None, size=normalize_text(size) or None,
        unit=unit or "pcs", minimum_stock=minimum_stock, location=normalize_text(location) or None,
        notes=notes, active=True, current_stock_cache=0,
    )
    db.session.add(spare_part)
    db.session.flush()
    _assign_code(spare_part)

    for machine_id in (machine_ids or []):
        if db.session.get(Machine, machine_id) is None:
            raise SparePartError(f"unknown machine id {machine_id}")
        db.session.add(SparePartMachine(spare_part_id=spare_part.id, machine_id=machine_id))
    db.session.flush()
    return spare_part


def update_spare_part(spare_part, changes, machine_ids=None):
    if "name" in changes:
        name = normalize_text(changes["name"])
        if not name:
            raise SparePartError("name cannot be empty")
        spare_part.name = name
    if "model" in changes:
        spare_part.model = normalize_text(changes["model"]) or None
    if "size" in changes:
        spare_part.size = normalize_text(changes["size"]) or None
    if "unit" in changes:
        if changes["unit"] not in UNITS:
            raise SparePartError(f"unit must be one of {UNITS}")
        spare_part.unit = changes["unit"]
    if "minimum_stock" in changes:
        spare_part.minimum_stock = changes["minimum_stock"]
    if "location" in changes:
        spare_part.location = normalize_text(changes["location"]) or None
    if "notes" in changes:
        spare_part.notes = changes["notes"]
    if "active" in changes:
        spare_part.active = bool(changes["active"])

    if machine_ids is not None:
        for m_id in machine_ids:
            if db.session.get(Machine, m_id) is None:
                raise SparePartError(f"unknown machine id {m_id}")
        existing = {sm.machine_id: sm for sm in SparePartMachine.query.filter_by(spare_part_id=spare_part.id).all()}
        target = set(machine_ids)
        for m_id in target - set(existing):
            db.session.add(SparePartMachine(spare_part_id=spare_part.id, machine_id=m_id))
        for m_id, row in existing.items():
            if m_id not in target:
                db.session.delete(row)
    db.session.flush()
    return spare_part


def spare_part_machines(spare_part_id):
    return (
        db.session.query(Machine)
        .join(SparePartMachine, SparePartMachine.machine_id == Machine.id)
        .filter(SparePartMachine.spare_part_id == spare_part_id)
        .order_by(Machine.name)
        .all()
    )


def spare_part_suppliers(spare_part_id):
    return SparePartSupplier.query.filter_by(spare_part_id=spare_part_id).order_by(SparePartSupplier.id).all()
