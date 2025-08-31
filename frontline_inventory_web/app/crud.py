# app/crud.py
from sqlalchemy import select, func, update
from sqlalchemy.orm import Session
from datetime import datetime
from typing import Optional, Tuple, List, Iterable, Dict
from collections import defaultdict
from fastapi import HTTPException
from sqlalchemy import select, func, update

from .models import Item, Category, Location, Tx, User, ItemUnit, PurchaseOrder, PurchaseOrderLine, CustomerOrder, CustomerOrderLine, Customer

def create_customer(db: Session, name: str, email: str = "", phone: str = "", notes: str = "") -> Customer:
    c = Customer(name=name.strip(), email=email.strip(), phone=phone.strip(), notes=notes.strip())
    db.add(c); db.commit(); db.refresh(c)
    return c

def create_customer_order(db: Session, customer: Customer, code: str, notes: str = "") -> CustomerOrder:
    code = code.strip()
    co = db.execute(select(CustomerOrder).where(CustomerOrder.code == code)).scalar_one_or_none()
    if co:
        return co
    co = CustomerOrder(customer_id=customer.id, code=code, notes=notes.strip(), status="open")
    db.add(co); db.commit(); db.refresh(co)
    return co

def _gen_co_code(db: Session) -> str:
    # CO-ÅÅÅÅ-NNN (løpenr per år)
    yr = datetime.utcnow().year
    prefix = f"CO-{yr}-"
    last = db.execute(select(CustomerOrder).where(CustomerOrder.code.like(f"{prefix}%"))).scalars().all()
    seq = 1 + max(
        [int(c.code.split("-")[-1]) for c in last if c.code.split("-")[-1].isdigit()] or [0]
    )
    return f"{prefix}{seq:03d}"

def get_or_create_open_co_for_customer(db: Session, customer_id: int) -> CustomerOrder:
    cust = db.get(Customer, customer_id)
    if not cust:
        raise HTTPException(400, "Ugyldig kunde")

    co = db.execute(
        select(CustomerOrder)
        .where(CustomerOrder.customer_id == customer_id)
        .where(CustomerOrder.status == "open")
        .order_by(CustomerOrder.id.desc())
    ).scalar_one_or_none()

    if co:
        return co

    code = _gen_co_code(db)
    co = CustomerOrder(customer_id=customer_id, code=code, status="open", created_at=datetime.utcnow())
    db.add(co)
    db.commit()
    db.refresh(co)
    return co

def reserve_qty_for_customer(
    db,
    item_id: int,
    qty: int,
    customer_id: int,
    note: str,
    actor,
    co_id: int | None = None,   # kan spesifisere eksisterende CO
):
    if qty <= 0:
        raise HTTPException(400, "Antall må være > 0")

    item = db.get(Item, item_id)
    if not item:
        raise HTTPException(404, "Fant ikke varen")

    cust = db.get(Customer, customer_id)
    if not cust:
        raise HTTPException(404, "Fant ikke kunden")

    # Finn/valider CO
    if co_id:
        co = db.get(CustomerOrder, co_id)
        if not co:
            raise HTTPException(404, "Fant ikke kundeordre")
        if co.customer_id and co.customer_id != customer_id:
            raise HTTPException(400, "Valgt ordre tilhører en annen kunde")
        if co.status and co.status != "open":
            raise HTTPException(400, f"Ordre {co.code} er ikke åpen")
        if not co.customer_id:
            co.customer_id = customer_id
    else:
        # hent/lag åpen CO for kunden
        co = get_or_create_open_co_for_customer(db, customer_id)

    # Plukk ledige enheter (støtt både 'ledig' og 'available')
    units = db.execute(
        select(ItemUnit)
        .where(ItemUnit.item_id == item_id)
        .where(ItemUnit.status.in_(("ledig", "available")))
        .order_by(ItemUnit.id.asc())
        .limit(qty)
    ).scalars().all()

    take = min(qty, len(units))
    if take == 0:
        raise HTTPException(400, "Ingen ledige enheter å reservere.")

    # Marker enheter + logg transaksjoner pr enhet
    reserved_now = 0
    for u in units[:take]:
        u.status = "reservert"
        u.reserved_co_id = co.id
        tx = Tx(
            item_id=item.id,
            sku=item.sku,
            name=item.name,
            delta=0,
            note=note or f"Reservert til CO {co.code}",
            ts=datetime.utcnow(),
            user_id=getattr(actor, "id", None),
            user_name=getattr(actor, "name", None),
            unit_id=u.id,
            co_id=co.id,
        )
        db.add(tx)
        reserved_now += 1

    # Summer/oppdater ordrelinje for varen (NB: uten unit_id)
    line = db.execute(
        select(CustomerOrderLine)
        .where(CustomerOrderLine.co_id == co.id)
        .where(CustomerOrderLine.item_id == item.id)
    ).scalar_one_or_none()

    if line:
        line.qty = (line.qty or 0) + reserved_now
        if note:
            existing = (line.notes or "")
            if note not in existing:
                line.notes = (existing + (" | " if existing else "") + note)
    else:
        db.add(CustomerOrderLine(
            co_id=co.id,
            item_id=item.id,
            qty=reserved_now,
            notes=note or "",
            created_at=datetime.utcnow(),
        ))

    db.commit()
    return co, reserved_now

def get_or_create_co_by_code(db: Session, code: str, customer: Customer | None = None) -> CustomerOrder:
    code = code.strip()
    co = db.execute(select(CustomerOrder).where(CustomerOrder.code == code)).scalar_one_or_none()
    if co:
        return co
    if not customer:
        # fallback "ukjent kunde"
        customer = db.execute(select(Customer).where(Customer.name == "Ukjent")).scalar_one_or_none()
        if not customer:
            customer = create_customer(db, "Ukjent")
    return create_customer_order(db, customer, code)

def ensure_line(db: Session, co: CustomerOrder, item: Item) -> CustomerOrderLine:
    line = db.execute(
        select(CustomerOrderLine).where(CustomerOrderLine.co_id == co.id, CustomerOrderLine.item_id == item.id)
    ).scalar_one_or_none()
    if not line:
        line = CustomerOrderLine(co_id=co.id, item_id=item.id, qty_ordered=0, qty_reserved=0, qty_fulfilled=0)
        db.add(line); db.commit(); db.refresh(line)
    return line

def reserve_units(db: Session, item: Item, co: CustomerOrder, qty: int, note: str = "", actor: User | None = None):
    qty = max(0, int(qty))
    if qty == 0:
        return

    # Finn ledige enheter
    units = db.execute(
        select(ItemUnit).where(ItemUnit.item_id == item.id, ItemUnit.status == "available").limit(qty)
    ).scalars().all()

    if len(units) < qty:
        raise HTTPException(status_code=400, detail=f"For få ledige enheter. Ledig: {len(units)}, ønsket: {qty}")

    # Marker som reservert
    now = datetime.utcnow()
    for u in units:
        u.status = "reserved"
        u.reserved_co_id = co.id

    # Oppdater ordrelinje
    line = ensure_line(db, co, item)
    line.qty_reserved = (line.qty_reserved or 0) + qty

    # Logg (delta=0)
    db.add(Tx(
        item_id=item.id, sku=item.sku, name=item.name, delta=0,
        note=note or f"Reservert {qty} stk til {co.code}",
        user_id=(actor.id if actor else None), user_name=(actor.name if actor else None),
        co_id=co.id
    ))
    db.commit()

def release_units(db: Session, item: Item, co: CustomerOrder, qty: int, note: str = "", actor: User | None = None):
    qty = max(0, int(qty))
    if qty == 0:
        return
    # Finn reserverte enheter på denne CO
    units = db.execute(
        select(ItemUnit).where(ItemUnit.item_id == item.id, ItemUnit.status == "reserved", ItemUnit.reserved_co_id == co.id).limit(qty)
    ).scalars().all()
    if len(units) < qty:
        raise HTTPException(status_code=400, detail=f"For få reserverte å frigi. Reservert: {len(units)}, ønsket: {qty}")

    for u in units:
        u.status = "available"
        u.reserved_co_id = None

    line = ensure_line(db, co, item)
    line.qty_reserved = max(0, (line.qty_reserved or 0) - qty)

    db.add(Tx(
        item_id=item.id, sku=item.sku, name=item.name, delta=0,
        note=note or f"Frigitt {qty} stk fra {co.code}",
        user_id=(actor.id if actor else None), user_name=(actor.name if actor else None),
        co_id=co.id
    ))
    db.commit()

def fulfill_units(db: Session, item: Item, co: CustomerOrder, qty: int, note: str = "", actor: User | None = None) -> Tx:
    qty = max(0, int(qty))
    if qty == 0:
        raise HTTPException(status_code=400, detail="Angi antall > 0")

    # Ta fra reserverte først
    reserved = db.execute(
        select(ItemUnit).where(ItemUnit.item_id == item.id, ItemUnit.status == "reserved", ItemUnit.reserved_co_id == co.id).limit(qty)
    ).scalars().all()
    if len(reserved) < qty:
        raise HTTPException(status_code=400, detail=f"Mangler reserverte enheter. Reservert: {len(reserved)}, ønsket: {qty}")

    now = datetime.utcnow()
    for u in reserved:
        u.status = "used"
        u.used_at = now

    line = ensure_line(db, co, item)
    take = len(reserved)
    line.qty_reserved = max(0, (line.qty_reserved or 0) - take)
    line.qty_fulfilled = (line.qty_fulfilled or 0) + take

    # Lageruttak
    tx = adjust_stock(db, item, delta=-take, note=note or f"Utlevert {take} stk til {co.code}", actor=actor, co=co)
    db.commit(); db.refresh(tx)
    return tx

def get_or_create_category(db: Session, name: str | None) -> Optional[Category]:
    if not name:
        return None
    name = name.strip()
    if not name:
        return None
    cat = db.execute(select(Category).where(Category.name == name)).scalar_one_or_none()
    if not cat:
        cat = Category(name=name)
        db.add(cat)
        db.commit()
        db.refresh(cat)
    return cat


def get_or_create_location(db: Session, name: str | None) -> Optional[Location]:
    if not name:
        return None
    name = name.strip()
    if not name:
        return None
    loc = db.execute(select(Location).where(Location.name == name)).scalar_one_or_none()
    if not loc:
        loc = Location(name=name)
        db.add(loc)
        db.commit()
        db.refresh(loc)
    return loc


def create_item(db: Session, actor: Optional[User] = None, **data) -> Item:
    """
    Forventer felter som passer Item + 'category' og 'location' (strings).
    'actor' brukes kun til audit og må ikke lekke inn i Item(**data).
    """
    # Ta ut felter som ikke tilhører Item
    category_name = data.pop("category", None)
    location_name = data.pop("location", None)
    data.pop("actor", None)  # paranoid cleanup i tilfelle noen kaller med actor i **data

    # Default-felt for Item som kan mangle
    data.setdefault("qty", 0)
    data.setdefault("min_qty", 0)
    data.setdefault("price", 0.0)
    data.setdefault("currency", "NOK")
    data.setdefault("notes", "")
    data.setdefault("image_path", "")

    # Slå opp/lag kategori og lokasjon
    cat = get_or_create_category(db, category_name)
    loc = get_or_create_location(db, location_name)

    item = Item(**data)
    item.category_obj = cat
    item.location_obj = loc
    db.add(item)
    db.commit()
    db.refresh(item)

    # Audit (delta=0)
    db.add(Tx(
        item_id=item.id, sku=item.sku, name=item.name, delta=0, note="Opprettet vare",
        user_id=(actor.id if actor else None), user_name=(actor.name if actor else None),
    ))
    db.commit()
    return item


def update_item(db: Session, item: Item, actor: Optional[User] = None, **data) -> Item:
    # Ta ut felter som ikke tilhører Item
    category_name = data.pop("category", None)
    location_name = data.pop("location", None)
    data.pop("actor", None)

    # Oppdater primitive felter på Item
    for k, v in data.items():
        setattr(item, k, v)

    # Oppdater relasjoner
    cat = get_or_create_category(db, category_name)
    loc = get_or_create_location(db, location_name)
    if cat is not None:
        item.category_obj = cat
    if loc is not None:
        item.location_obj = loc

    item.last_updated = datetime.utcnow()
    db.add(item)
    db.commit()
    db.refresh(item)

    # Audit (delta=0)
    db.add(Tx(
        item_id=item.id, sku=item.sku, name=item.name, delta=0, note="Oppdatert vare",
        user_id=(actor.id if actor else None), user_name=(actor.name if actor else None),
    ))
    db.commit()
    return item


from sqlalchemy import select, func, update  # ← legg til update her
# ...


def delete_item(db: Session, item: Item, actor: Optional[User] = None, confirm_code: str | None = None) -> None:
    units_cnt = db.execute(select(func.count(ItemUnit.id)).where(ItemUnit.item_id == item.id)).scalar() or 0
    pol_cnt   = db.execute(select(func.count(PurchaseOrderLine.id)).where(PurchaseOrderLine.item_id == item.id)).scalar() or 0

    # Krev kode hvis noe refererer til varen
    if (units_cnt > 0 or pol_cnt > 0) and confirm_code != "1234":
        raise HTTPException(
            status_code=400,
            detail=f"Varen er i bruk ({units_cnt} enhet(er), {pol_cnt} PO-linje(r)). Skriv 1234 for å bekrefte sletting."
        )

    # Audit
    db.add(Tx(
        item_id=item.id, sku=item.sku, name=item.name, delta=0, note="Slettet vare",
        user_id=(actor.id if actor else None), user_name=(actor.name if actor else None),
    ))
    db.commit()

    # Frikoble referanser (vi har satt FK til SET NULL i migreringen)
    db.execute(update(Tx).where(Tx.item_id == item.id).values(item_id=None))
    db.execute(update(ItemUnit).where(ItemUnit.item_id == item.id).values(item_id=None))
    db.execute(update(PurchaseOrderLine).where(PurchaseOrderLine.item_id == item.id).values(item_id=None))
    db.commit()

    db.delete(item)
    db.commit()



def adjust_stock(db: Session, item: Item, delta: int, note: str = "", actor: Optional[User] = None) -> Tx:
    item.qty = max(0, (item.qty or 0) + int(delta))
    item.last_updated = datetime.utcnow()
    tx = Tx(
        item_id=item.id, sku=item.sku, name=item.name, delta=int(delta), note=note,
        user_id=(actor.id if actor else None), user_name=(actor.name if actor else None),
    )
    db.add_all([item, tx])
    db.commit()
    db.refresh(tx)
    return tx


def inventory_stats(db: Session) -> Tuple[int, float]:
    total_items = db.execute(select(func.count(Item.id))).scalar_one()
    total_value = db.execute(select(func.sum((Item.price or 0) * (Item.qty or 0)))).scalar()
    return total_items or 0, float(total_value or 0.0)


def get_or_create_po(db: Session, code: str, supplier: str = "") -> PurchaseOrder:
    code = code.strip()
    po = db.execute(select(PurchaseOrder).where(PurchaseOrder.code == code)).scalar_one_or_none()
    if not po:
        po = PurchaseOrder(code=code, supplier=supplier or "")
        db.add(po); db.commit(); db.refresh(po)
    return po

def get_or_create_co(db: Session, code: str, customer: str = "") -> CustomerOrder:
    code = code.strip()
    co = db.execute(select(CustomerOrder).where(CustomerOrder.code == code)).scalar_one_or_none()
    if not co:
        co = CustomerOrder(code=code, customer=customer or "")
        db.add(co); db.commit(); db.refresh(co)
    return co

def create_units_for_receive(
    db: Session,
    item: Item,
    qty: int,
    po_code: str,
    note: str,
    actor: User | None = None,
) -> Tx:
    qty = int(qty)

    # 1) Sørg for PO
    po_code = (po_code or "").strip()
    if not po_code:
        # du kan evt. gjøre den påkrevd i /receive-skjemaet; her aksepterer vi tom
        po = None
    else:
        po = db.execute(select(PurchaseOrder).where(PurchaseOrder.code == po_code)).scalar_one_or_none()
        if not po:
            po = PurchaseOrder(code=po_code, supplier="")
            db.add(po)
            db.commit()
            db.refresh(po)

    # 2) Opprett enheter
    for _ in range(qty):
        db.add(ItemUnit(
            item_id=item.id,
            po_id=(po.id if po else None),
            status="available",
            created_at=datetime.utcnow(),
        ))

    # 3) Øk lagerantall på varen
    item.qty = (item.qty or 0) + qty

    # 4) Oppdater PO-linje (qty_received)
    if po:
        pol = db.execute(
            select(PurchaseOrderLine).where(
                PurchaseOrderLine.po_id == po.id,
                PurchaseOrderLine.item_id == item.id
            )
        ).scalar_one_or_none()
        if not pol:
            pol = PurchaseOrderLine(po_id=po.id, item_id=item.id, qty_ordered=0, qty_received=0)
            db.add(pol)
        pol.qty_received = (pol.qty_received or 0) + qty

    # 5) Tx for mottaket
    tx = Tx(
        item_id=item.id,
        sku=item.sku,
        name=item.name,
        delta=qty,
        note=note or "Mottak",
        user_id=(actor.id if actor else None),
        user_name=(actor.name if actor else None),
        po_id=(po.id if po else None),
    )
    db.add(tx)

    db.commit()
    db.refresh(tx)
    return tx

def reserve_units(db: Session, unit_ids: Iterable[int], co_code: str, note: str, actor: Optional[User]) -> int:
    co = get_or_create_co(db, co_code)
    ids = list(map(int, unit_ids))
    rows = db.execute(select(ItemUnit).where(ItemUnit.id.in_(ids))).scalars().all()
    by_item: Dict[int, List[ItemUnit]] = defaultdict(list)
    for u in rows:
        if u.status == "available":
            u.status = "reserved"
            u.reserved_co_id = co.id
            by_item[u.item_id].append(u)
    # Audit (delta=0 pr vare)
    for item_id, units in by_item.items():
        item = db.get(Item, item_id)
        db.add(Tx(
            item_id=item_id, sku=item.sku, name=item.name, delta=0,
            note=(note or "Reservert") + f" {len(units)} stk (CO {co.code})",
            co_id=co.id, user_id=(actor.id if actor else None), user_name=(actor.name if actor else None),
        ))
    db.commit()
    return sum(len(v) for v in by_item.values())

def unreserve_units(db: Session, unit_ids: Iterable[int], note: str, actor: Optional[User]) -> int:
    ids = list(map(int, unit_ids))
    rows = db.execute(select(ItemUnit).where(ItemUnit.id.in_(ids))).scalars().all()
    by_item: Dict[int, List[ItemUnit]] = defaultdict(list)
    for u in rows:
        if u.status == "reserved":
            by_item[u.item_id].append(u)
            u.status = "available"
            u.reserved_co_id = None
    for item_id, units in by_item.items():
        item = db.get(Item, item_id)
        db.add(Tx(
            item_id=item_id, sku=item.sku, name=item.name, delta=0,
            note=(note or "Opphevet reservasjon") + f" {len(units)} stk",
            user_id=(actor.id if actor else None), user_name=(actor.name if actor else None),
        ))
    db.commit()
    return sum(len(v) for v in by_item.values())

def issue_units(db: Session, unit_ids: Iterable[int], co_code: str, note: str, actor: Optional[User]) -> int:
    co = get_or_create_co(db, co_code)
    ids = list(map(int, unit_ids))
    rows = db.execute(select(ItemUnit).where(ItemUnit.id.in_(ids))).scalars().all()

    # splitte pr vare og pr PO (for tydelig dokumentasjon)
    by_item_po: Dict[tuple[int, int | None], List[ItemUnit]] = defaultdict(list)
    for u in rows:
        if u.status in ("available", "reserved"):
            by_item_po[(u.item_id, u.po_id)].append(u)

    total = 0
    for (item_id, po_id), units in by_item_po.items():
        item = db.get(Item, item_id)
        k = len(units)
        total += k
        # endre status
        for u in units:
            u.status = "used"
            u.used_at = datetime.utcnow()
            u.reserved_co_id = co.id  # “forbrukt til” CO
        # trekk fra lager
        item.qty = max(0, (item.qty or 0) - k)
        item.last_updated = datetime.utcnow()
        # audit (delta=-k)
        db.add_all([
            item,
            Tx(
                item_id=item.id, sku=item.sku, name=item.name, delta=-k,
                note=(note or "Uttak") + f" (CO {co.code}" + (f", PO {db.get(PurchaseOrder, po_id).code}" if po_id else "") + ")",
                co_id=co.id, po_id=po_id,
                user_id=(actor.id if actor else None), user_name=(actor.name if actor else None),
            )
        ])
    db.commit()
    return total

def unit_counts(db: Session, item: Item) -> Tuple[int,int,int]:
    total_avail = db.execute(select(func.count(ItemUnit.id)).where(ItemUnit.item_id==item.id, ItemUnit.status=="available")).scalar() or 0
    total_res = db.execute(select(func.count(ItemUnit.id)).where(ItemUnit.item_id==item.id, ItemUnit.status=="reserved")).scalar() or 0
    total_used = db.execute(select(func.count(ItemUnit.id)).where(ItemUnit.item_id==item.id, ItemUnit.status=="used")).scalar() or 0
    return int(total_avail), int(total_res), int(total_used)