import os, csv, io, asyncio, json
from datetime import datetime
from typing import Optional, List
from fastapi import FastAPI, Request, Form, UploadFile, File, Depends, HTTPException, Response
from fastapi.responses import HTMLResponse, StreamingResponse, PlainTextResponse
from starlette.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import select
from .models import Customer
from .db import DB_PATH
from .models import Item, Category, Location, Tx, ItemUnit, PurchaseOrder, CustomerOrder, Customer, CustomerOrderLine

from . import crud
from fastapi import Form

from fastapi import HTTPException, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from sqlalchemy import select, func

from .db import SessionLocal, engine, Base, ensure_migrations
from .models import Item, Category, Location, Tx
from . import crud
from .auth import router as auth_router, require_user

# --------- App init ---------
app = FastAPI(title="Frontline Inventory (Server-drevet)")

# Sessions (cookie-basert)
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, max_age=60*60*8, same_site="lax", https_only=False)

# Static og templates
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(os.path.join(STATIC_DIR, "uploads"), exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

# DB tabeller + mini-migrering
Base.metadata.create_all(bind=engine)
ensure_migrations()  # <- VIKTIG: legger til transactions.user_id / user_name hvis de mangler

# Auth-ruter
app.include_router(auth_router)

# --------- DB dependency ---------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --------- SSE broadcast (enkel) ---------
class Broadcaster:
    def __init__(self):
        self.listeners: List[asyncio.Queue] = []
    async def publish(self, event: dict):
        for q in list(self.listeners):
            try:
                await q.put(event)
            except Exception:
                pass
    async def subscribe(self) -> asyncio.Queue:
        q = asyncio.Queue()
        self.listeners.append(q)
        return q
    def unsubscribe(self, q: asyncio.Queue):
        try:
            self.listeners.remove(q)
        except ValueError:
            pass

bcast = Broadcaster()

# --------- Helpers ---------
def fmt_currency(v: float) -> str:
    try:
        return f"{v:,.2f}".replace(",", " ").replace(".", ",")
    except Exception:
        return str(v)

# --------- Routes ---------
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, q: str = "", category: str = "Alle", location: str = "Alle",
              sort: str = "name", page: int = 1, per_page: int = 25, db: Session = Depends(get_db),
              current_user=Depends(require_user)):
    # Lister
    cats = [c.name for c in db.execute(select(Category).order_by(Category.name)).scalars()]
    locs = [l.name for l in db.execute(select(Location).order_by(Location.name)).scalars()]

    stmt = select(Item)
    if q:
        like = f"%{q}%"
        from sqlalchemy import or_
        stmt = stmt.where(or_(Item.name.like(like), Item.sku.like(like), Item.notes.like(like)))
    if category != "Alle":
        stmt = stmt.join(Item.category_obj).where(Category.name == category)
    if location != "Alle":
        stmt = stmt.join(Item.location_obj).where(Location.name == location)

    # sortering
    if sort == "qty":
        from sqlalchemy import desc
        stmt = stmt.order_by(desc(Item.qty), Item.name)
    elif sort == "value":
        from sqlalchemy import desc
        stmt = stmt.order_by(desc((Item.price) * (Item.qty)), Item.name)
    elif sort == "sku":
        stmt = stmt.order_by(Item.sku)
    else:
        stmt = stmt.order_by(Item.name)

    items = db.execute(stmt).scalars().all()

    # paginering
    total = len(items)
    start = (page - 1) * per_page
    end = start + per_page
    page_items = items[start:end]

    low_count = sum(1 for i in items if (i.qty or 0) <= (i.min_qty or 0))
    total_items, total_value = crud.inventory_stats(db)

    return templates.TemplateResponse("index.html", {
        "request": request,
        "user": current_user,
        "items": page_items,
        "q": q,
        "category": category,
        "location": location,
        "cats": cats,
        "locs": locs,
        "sort": sort,
        "page": page,
        "per_page": per_page,
        "total": total,
        "low_count": low_count,
        "total_items": total_items,
        "total_value": total_value,
        "fmt_currency": fmt_currency
    })

@app.get("/dev/whoami")
def dev_whoami(db: Session = Depends(get_db), current_user = Depends(require_user)):
    rows = db.execute(select(Customer).order_by(Customer.id.desc()).limit(5)).scalars().all()
    return {
        "db_path": DB_PATH,
        "customers_count": db.execute(select(Customer)).scalars().count() if hasattr(db.execute(select(Customer)).scalars(), "count") else len(db.execute(select(Customer)).scalars().all()),
        "last5": [{"id": c.id, "name": c.name, "email": c.email} for c in rows][::-1],
    }

@app.get("/api/customers")
def api_customers(db: Session = Depends(get_db), current_user = Depends(require_user)):
    rows = db.execute(select(Customer).order_by(Customer.name.asc())).scalars().all()
    return [{"id": c.id, "name": c.name, "email": c.email or ""} for c in rows]

@app.get("/item/new", response_class=HTMLResponse)
def item_new(request: Request, current_user=Depends(require_user)):
    return templates.TemplateResponse("item_form.html", {"request": request, "user": current_user, "item": None})

@app.post("/item/new", response_class=HTMLResponse)
def item_create(
    request: Request,
    name: str = Form(...),
    sku: str = Form(...),
    qty: int = Form(0),
    min_qty: int = Form(0),
    price: float = Form(0.0),
    currency: str = Form("NOK"),
    category: str = Form(""),
    location: str = Form(""),
    notes: str = Form(""),
    image: UploadFile | None = File(None),
    db: Session = Depends(get_db),
    current_user=Depends(require_user)
):
    image_path = ""
    if image and image.filename:
        uploads_dir = os.path.join(os.path.dirname(__file__), "static", "uploads")
        os.makedirs(uploads_dir, exist_ok=True)
        fname = f"{datetime.utcnow().timestamp()}_{image.filename}"
        fpath = os.path.join(uploads_dir, fname)
        with open(fpath, "wb") as f:
            f.write(image.file.read())
        image_path = f"/static/uploads/{fname}"

    item = crud.create_item(db,
        actor=current_user,
        name=name.strip(), sku=sku.strip(), qty=qty, min_qty=min_qty, price=price, currency=currency.strip(),
        category=category.strip(), location=location.strip(), notes=notes.strip(), image_path=image_path
    )
    return RedirectResponse(url="/", status_code=303)

@app.get("/item/{item_id}")
async def item_detail(
    request: Request,
    item_id: int,
    db: Session = Depends(get_db),
    current_user = Depends(require_user),
):
    item = db.get(Item, item_id)
    if not item:
        raise HTTPException(status_code=404)

    # Enheter til tabellen nederst
    units = db.execute(
        select(ItemUnit)
        .where(ItemUnit.item_id == item.id)
        .order_by(ItemUnit.status.desc(), ItemUnit.id.desc())
    ).scalars().all()

    # Transaksjoner (hvis vist i UI)
    txs = db.execute(
        select(Tx).where(Tx.item_id == item.id).order_by(Tx.id.desc()).limit(50)
    ).scalars().all()

    # Tellerne slik templaten din forventer (count_avail/res/used)
    count_avail = sum(1 for u in units if u.status in ("available", "ledig"))
    count_res   = sum(1 for u in units if u.status in ("reserved",  "reservert"))
    count_used  = sum(1 for u in units if u.status in ("used",      "brukt"))

    # KUNDER til dropdown (nøkkelen)
    customers = db.execute(
        select(Customer).order_by(Customer.name.asc())
    ).scalars().all()

    # HØYLYTT DEBUG i konsollen – skal være > 0 hvis kunder finnes
    print(f"[item_detail] DB={DB_PATH} customers_len={len(customers)} at {datetime.utcnow().isoformat()}")

    return templates.TemplateResponse(
        "item_units.html",
        {
            "request": request,
            "item": item,
            "units": units,
            "txs": txs,
            "count_avail": count_avail,
            "count_res": count_res,
            "count_used": count_used,
            "customers": customers,
        },
    )

@app.get("/item/{item_id}/edit", response_class=HTMLResponse)
def item_edit(request: Request, item_id: int, db: Session = Depends(get_db), current_user=Depends(require_user)):
    item = db.get(Item, item_id)
    if not item:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse("item_form.html", {"request": request, "user": current_user, "item": item})

@app.post("/item/{item_id}/edit", response_class=HTMLResponse)
def item_update(
    request: Request, item_id: int,
    name: str = Form(...),
    sku: str = Form(...),
    qty: int = Form(0),
    min_qty: int = Form(0),
    price: float = Form(0.0),
    currency: str = Form("NOK"),
    category: str = Form(""),
    location: str = Form(""),
    notes: str = Form(""),
    image: UploadFile | None = File(None),
    db: Session = Depends(get_db),
    current_user=Depends(require_user)
):
    
    
    item = db.get(Item, item_id)
    if not item:
        raise HTTPException(status_code=404)

    image_path = item.image_path or ""
    if image and image.filename:
        uploads_dir = os.path.join(os.path.dirname(__file__), "static", "uploads")
        os.makedirs(uploads_dir, exist_ok=True)
        fname = f"{datetime.utcnow().timestamp()}_{image.filename}"
        fpath = os.path.join(uploads_dir, fname)
        with open(fpath, "wb") as f:
            f.write(image.file.read())
        image_path = f"/static/uploads/{fname}"

    crud.update_item(db, item,
        actor=current_user,
        name=name.strip(), sku=sku.strip(), qty=qty, min_qty=min_qty, price=price, currency=currency.strip(),
        category=category.strip(), location=location.strip(), notes=notes.strip(), image_path=image_path
    )
    return HTMLResponse(headers={"HX-Redirect": "/"}, content="")

@app.post("/item/{item_id}/delete")
def item_delete(
    request: Request,
    item_id: int,
    confirm: str = Form(""),
    db: Session = Depends(get_db),
    current_user = Depends(require_user)
):
    item = db.get(Item, item_id)
    if not item:
        raise HTTPException(status_code=404)
    try:
        crud.delete_item(db, item, actor=current_user, confirm_code=confirm.strip())
        return RedirectResponse(url="/", status_code=303)
    except HTTPException as e:
        if e.status_code == 400:
            items = db.execute(select(Item).order_by(Item.name)).scalars().all()
            return templates.TemplateResponse(
                "index.html",
                {"request": request, "user": current_user, "items": items, "error": e.detail},
                status_code=400
            )
        raise

@app.post("/item/{item_id}/adjust")
async def item_adjust(
    request: Request,
    item_id: int,
    delta: int = Form(...),
    note: str = Form("Justering"),
    db: Session = Depends(get_db),
    current_user = Depends(require_user),
):
    item = db.get(Item, item_id)
    if not item:
        raise HTTPException(status_code=404)
    tx = crud.adjust_stock(db, item, delta=delta, note=note, actor=current_user)
    await bcast.publish({
        "type": "tx",
        "id": tx.id,
        "name": tx.name,
        "sku": tx.sku,
        "delta": tx.delta,
        "note": tx.note,
        "ts": tx.ts.isoformat(),
        "by": tx.user_name,
    })
    return RedirectResponse(url="/", status_code=303)

@app.get("/item/{item_id}/units", response_class=HTMLResponse)
def item_units_page(request: Request, item_id: int, db: Session = Depends(get_db), current_user=Depends(require_user)):
    item = db.get(Item, item_id)
    if not item:
        raise HTTPException(status_code=404)
    units = db.execute(
    select(ItemUnit)
    .where(ItemUnit.item_id == item.id)
    .order_by(ItemUnit.status.desc(), ItemUnit.id.desc())
).scalars().all()
    avail, res, used = crud.unit_counts(db, item)
    return templates.TemplateResponse("item_units.html", {
        "request": request, "user": current_user, "item": item, "units": units,
        "count_avail": avail, "count_res": res, "count_used": used
    })

@app.post("/receive")
async def receive_post(
    request: Request,
    sku: str = Form(...),
    qty: int = Form(1),
    note: str = Form("Mottak"),
    po_code: str = Form(""),                # ⬅️ NYTT felt fra skjema
    db: Session = Depends(get_db),
    current_user = Depends(require_user),
):
    sku = sku.strip()
    item = db.execute(select(Item).where(Item.sku == sku)).scalar_one_or_none()
    if not item:
        # auto-opprette enkel vare hvis SKU ikke finnes
        item = crud.create_item(
            db, actor=current_user,
            name=sku, sku=sku, qty=0, min_qty=0, price=0.0,
            currency="NOK", category="Uncategorized", location="Hovedlager", notes=""
        )

    # NYTT: bruk enhets-mottak + PO-oppdatering i stedet for bare adjust_stock
    tx = crud.create_units_for_receive(
        db, item, qty=int(qty), po_code=po_code, note=(note.strip() or "Mottak"), actor=current_user
    )

    # behold broadcast (samme format som tidligere)
    await bcast.publish({
        "type": "tx",
        "id": tx.id,
        "name": tx.name,
        "sku": tx.sku,
        "delta": tx.delta,
        "note": tx.note,
        "ts": tx.ts.isoformat(),
        "by": tx.user_name,
    })

    # Vis resultatet på enhetssiden til varen
    return RedirectResponse(url="/", status_code=303)

@app.post("/item/{item_id}/reserve_customer")
async def item_reserve_customer(
    request: Request,
    item_id: int,
    customer_id: int = Form(...),
    qty: int = Form(1),
    note: str = Form("Reservert"),
    co_id: int | None = Form(None),   # ← NYTT
    db: Session = Depends(get_db),
    current_user = Depends(require_user),
):
    try:
        co, taken = crud.reserve_qty_for_customer(
            db, item_id=item_id, qty=qty, customer_id=customer_id, note=note, actor=current_user, co_id=co_id
        )
        request.session["flash_success"] = (
            f"Reserverte {taken} av {qty} stk til {co.code} (resten manglet på lager)."
            if taken < qty else f"Reserverte {taken} stk til {co.code}."
        )
    except HTTPException as e:
        request.session["flash_error"] = e.detail if hasattr(e, "detail") else str(e)

    return RedirectResponse(url=f"/item/{item_id}", status_code=303)

# Åpne kundeordre for valgt kunde (til dropdown)
@app.get("/api/customers/{customer_id}/open_cos")
def api_open_cos(customer_id: int, db: Session = Depends(get_db), current_user = Depends(require_user)):
    from sqlalchemy import select
    from .models import CustomerOrder
    rows = db.execute(
        select(CustomerOrder)
        .where(CustomerOrder.customer_id == customer_id)
        .where(CustomerOrder.status == "open")
        .order_by(CustomerOrder.id.desc())
    ).scalars().all()
    return [{"id": co.id, "code": co.code} for co in rows]

# Lag en NY åpen CO for kunden (brukes av “+ Ny”-knappen)
@app.post("/api/customers/{customer_id}/co/new")
def api_new_co(customer_id: int, db: Session = Depends(get_db), current_user = Depends(require_user)):
    from .models import CustomerOrder
    from datetime import datetime
    n = db.query(CustomerOrder).count() + 1
    code = f"CO-{datetime.utcnow().year}-{n:03d}"
    co = CustomerOrder(code=code, customer_id=customer_id, status="open", notes="", created_at=datetime.utcnow())
    db.add(co)
    db.commit()
    return {"id": co.id, "code": co.code}

# CO-opplysninger for bekreftelse i UI
@app.get("/api/co/info")
def api_co_info(code: str, db: Session = Depends(get_db), current_user = Depends(require_user)):
    code = (code or "").strip()
    if not code:
        return {"exists": False}
    co = db.execute(select(CustomerOrder).where(CustomerOrder.code == code)).scalar_one_or_none()
    if not co:
        return {"exists": False}
    customer = db.get(Customer, co.customer_id) if co.customer_id else None
    return {
        "exists": True,
        "id": co.id,
        "code": co.code,
        "customer_id": co.customer_id or None,
        "customer_name": (customer.name if customer else None),
        "status": co.status,
    }

# Neste anbefalte CO-kode (sekvens per år)
@app.get("/api/co/next_code")
def api_co_next_code(db: Session = Depends(get_db), current_user = Depends(require_user)):
    return {"code": crud._gen_co_code(db)}

@app.post("/item/{item_id}/units/reserve")
async def item_units_reserve(
    request: Request,
    item_id: int,
    unit_ids: str = Form(...),
    co_code: str = Form(...),
    note: str = Form("Reservert"),
    db: Session = Depends(get_db),
    current_user = Depends(require_user),
):
    ids = [int(x) for x in unit_ids.split(",") if x.strip()]
    crud.reserve_units(db, ids, co_code=co_code, note=note, actor=current_user)
    return RedirectResponse(url=f"/item/{item_id}/units", status_code=303)

@app.post("/item/{item_id}/units/unreserve")
async def item_units_unreserve(
    request: Request,
    item_id: int,
    unit_ids: str = Form(...),
    note: str = Form("Opphevet reservasjon"),
    db: Session = Depends(get_db),
    current_user = Depends(require_user),
):
    ids = [int(x) for x in unit_ids.split(",") if x.strip()]
    crud.unreserve_units(db, ids, note=note, actor=current_user)
    return RedirectResponse(url=f"/item/{item_id}/units", status_code=303)


@app.post("/item/{item_id}/units/issue")
async def item_units_issue(
    request: Request,
    item_id: int,
    unit_ids: str = Form(...),
    co_code: str = Form(...),
    note: str = Form("Uttak"),
    db: Session = Depends(get_db),
    current_user = Depends(require_user),
):
    ids = [int(x) for x in unit_ids.split(",") if x.strip()]
    k = crud.issue_units(db, ids, co_code=co_code, note=note, actor=current_user)

    item = db.get(Item, item_id)
    if item:
        await bcast.publish({
            "type": "tx",
            "id": 0,
            "name": item.name,
            "sku": item.sku,
            "delta": -len(ids),
            "note": note,
            "ts": datetime.utcnow().isoformat(),
            "by": current_user.name,
        })
    return RedirectResponse(url=f"/item/{item_id}/units", status_code=303)

@app.get("/receive", response_class=HTMLResponse)
def receive_page(request: Request, current_user=Depends(require_user)):
    return templates.TemplateResponse("receive.html", {"request": request, "user": current_user})

@app.post("/receive")
def receive_post(
    sku: str = Form(...),
    qty: int = Form(...),
    po_code: str = Form(...),
    note: str = Form("Mottak"),
    db: Session = Depends(get_db),
    current_user = Depends(require_user),
):
    item = db.execute(select(Item).where(Item.sku == sku.strip())).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=400, detail=f"Ukjent SKU: {sku}")

    # NYTT: opprett faktiske enheter + koble til PO
    crud.create_units_for_receive(
        db, item, qty=qty, po_code=po_code.strip(), note=note, actor=current_user
    )

    # Etter enkelt-mottak: gå rett til enhetssiden så du ser tellere og rader
    return RedirectResponse(url=f"/item/{item.id}/units", status_code=303)

@app.get("/tx", response_class=HTMLResponse)
def tx_log(request: Request, q: str = "", db: Session = Depends(get_db), current_user=Depends(require_user)):
    stmt = select(Tx).order_by(Tx.ts.desc()).limit(500)
    rows = db.execute(stmt).scalars().all()
    if q:
        qq = q.lower()
        rows = [t for t in rows if qq in (t.name or "").lower() or qq in (t.sku or "").lower() or qq in (t.note or "").lower() or qq in (t.user_name or "").lower()]
    return templates.TemplateResponse("tx.html", {"request": request, "user": current_user, "rows": rows, "q": q})

@app.get("/stream/tx")
async def stream_tx(current_user=Depends(require_user)):
    async def event_generator():
        q = await bcast.subscribe()
        try:
            while True:
                event = await q.get()
                yield f"data: {json.dumps(event)}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            bcast.unsubscribe(q)
    return StreamingResponse(event_generator(), media_type="text/event-stream")

# ---------- Import/Export ----------
@app.get("/import", response_class=HTMLResponse)
def import_page(request: Request, current_user=Depends(require_user)):
    return templates.TemplateResponse("import.html", {"request": request, "user": current_user})

@app.post("/import")
def import_post(request: Request, file: UploadFile = File(...), mode: str = Form("merge"), db: Session = Depends(get_db), current_user=Depends(require_user)):
    content = file.file.read()
    name = file.filename or ""
    count = 0
    if name.endswith(".json"):
        arr = json.loads(content.decode("utf-8"))
        if not isinstance(arr, list):
            raise HTTPException(400, "JSON må være en liste")
        if mode == "replace":
            from sqlalchemy import delete
            db.execute(delete(Tx))
            db.execute(delete(Item))
            db.commit()
        for raw in arr:
            sku = str(raw.get("sku","")).strip()
            if not sku:
                continue
            existing = db.execute(select(Item).where(Item.sku == sku)).scalar_one_or_none()
            payload = {
                "name": raw.get("name") or sku,
                "sku": sku,
                "qty": int(raw.get("qty",0)),
                "min_qty": int(raw.get("minQty", raw.get("min_qty",0))),
                "price": float(raw.get("price",0.0)),
                "currency": raw.get("currency","NOK"),
                "category": raw.get("category",""),
                "location": raw.get("location",""),
                "notes": raw.get("notes","")
            }
            if existing:
                crud.update_item(db, existing, actor=current_user, **payload)
            else:
                crud.create_item(db, actor=current_user, **payload)
            count += 1
    else:
        text = content.decode("utf-8")
        import csv as _csv, io as _io
        reader = _csv.DictReader(_io.StringIO(text))
        if mode == "replace":
            from sqlalchemy import delete
            db.execute(delete(Tx))
            db.execute(delete(Item))
            db.commit()
        for row in reader:
            sku = str(row.get("sku","")).strip()
            if not sku:
                continue
            existing = db.execute(select(Item).where(Item.sku == sku)).scalar_one_or_none()
            payload = {
                "name": row.get("name") or sku,
                "sku": sku,
                "qty": int(row.get("qty", row.get("Antall", 0) or 0)),
                "min_qty": int(row.get("min_qty", row.get("Min", 0) or 0)),
                "price": float(row.get("price", 0.0) or 0.0),
                "currency": row.get("currency", "NOK"),
                "category": row.get("category",""),
                "location": row.get("location",""),
                "notes": row.get("notes","")
            }
            if existing:
                crud.update_item(db, existing, actor=current_user, **payload)
            else:
                crud.create_item(db, actor=current_user, **payload)
            count += 1

    return PlainTextResponse(f"Importert {count} varer")

@app.get("/export.json")
def export_json(db: Session = Depends(get_db), current_user=Depends(require_user)):
    rows = db.execute(select(Item)).scalars().all()
    arr = []
    for i in rows:
        arr.append({
            "name": i.name, "sku": i.sku, "qty": i.qty, "minQty": i.min_qty, "price": i.price, "currency": i.currency,
            "category": i.category_obj.name if i.category_obj else "", "location": i.location_obj.name if i.location_obj else "",
            "notes": i.notes, "image": i.image_path
        })
    data = json.dumps(arr, ensure_ascii=False, indent=2)
    return Response(content=data, media_type="application/json", headers={"Content-Disposition": "attachment; filename=frontline-inventory.json"})

@app.get("/export.csv")
def export_csv(db: Session = Depends(get_db), current_user=Depends(require_user)):
    rows = db.execute(select(Item)).scalars().all()
    out = io.StringIO()
    import csv as _csv
    writer = _csv.writer(out)
    writer.writerow(["name","sku","qty","min_qty","price","currency","category","location","notes","image"])
    for i in rows:
        writer.writerow([i.name, i.sku, i.qty, i.min_qty, i.price, i.currency, i.category_obj.name if i.category_obj else "", i.location_obj.name if i.location_obj else "", i.notes, i.image_path])
    return Response(content=out.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=frontline-inventory.csv"})

@app.get("/customers")
def customers_list(request: Request, db: Session = Depends(get_db), current_user=Depends(require_user)):
    rows = db.execute(select(Customer).order_by(Customer.name)).scalars().all()
    return templates.TemplateResponse("customers.html", {"request": request, "user": current_user, "rows": rows})

@app.get("/customers/new")
def customers_new(request: Request, db: Session = Depends(get_db), current_user=Depends(require_user)):
    return templates.TemplateResponse("customer_form.html", {"request": request, "user": current_user})

@app.post("/customers/new")
def customers_new_post(
    request: Request,
    name: str = Form(...), email: str = Form(""), phone: str = Form(""), notes: str = Form(""),
    db: Session = Depends(get_db), current_user=Depends(require_user)
):
    if not name.strip():
        raise HTTPException(status_code=400, detail="Navn er påkrevd")
    crud.create_customer(db, name=name, email=email, phone=phone, notes=notes)
    return RedirectResponse(url="/customers", status_code=303)

@app.get("/customers/{customer_id}/delete")
def customers_delete_confirm(request: Request, customer_id: int, db: Session = Depends(get_db), current_user=Depends(require_user)):
    cust = db.get(Customer, customer_id)
    if not cust:
        raise HTTPException(status_code=404)
    co_cnt = db.execute(select(func.count(CustomerOrder.id)).where(CustomerOrder.customer_id == cust.id)).scalar() or 0
    return templates.TemplateResponse(
        "customer_delete.html",
        {
            "request": request,
            "user": current_user,
            "customer": cust,
            "co_cnt": int(co_cnt),
            "error": None,
        },
    )

@app.post("/customers/{customer_id}/delete")
def customers_delete_post(
    request: Request,
    customer_id: int,
    confirm: str = Form(""),
    delete_cos: str | None = Form(None),
    db: Session = Depends(get_db),
    current_user=Depends(require_user),
):
    cust = db.get(Customer, customer_id)
    if not cust:
        raise HTTPException(status_code=404)
    try:
        # Slett tilknyttede CO-er hvis krysset av
        if delete_cos:
            crud.delete_customer_orders_for_customer(db, cust.id, confirm_code=confirm.strip())
        crud.delete_customer(db, cust, confirm_code=confirm.strip())
        return RedirectResponse(url="/customers", status_code=303)
    except HTTPException as e:
        if e.status_code == 400:
            co_cnt = db.execute(select(func.count(CustomerOrder.id)).where(CustomerOrder.customer_id == cust.id)).scalar() or 0
            return templates.TemplateResponse(
                "customer_delete.html",
                {
                    "request": request,
                    "user": current_user,
                    "customer": cust,
                    "co_cnt": int(co_cnt),
                    "error": e.detail,
                },
                status_code=400,
            )
        raise

@app.get("/co")
def co_list(
    request: Request,
    q: str | None = None,
    customer_id: int | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
    current_user=Depends(require_user),
):
    stmt = select(CustomerOrder).order_by(CustomerOrder.created_at.desc())
    if q and q.strip():
        qs = f"%{q.strip()}%"
        from sqlalchemy import or_
        stmt = stmt.where(CustomerOrder.code.ilike(qs))
    if customer_id:
        stmt = stmt.where(CustomerOrder.customer_id == customer_id)
    if status and status.strip():
        stmt = stmt.where(CustomerOrder.status == status.strip())
    rows = db.execute(stmt).scalars().all()
    customers = db.execute(select(Customer).order_by(Customer.name.asc())).scalars().all()
    return templates.TemplateResponse(
        "co_list.html",
        {
            "request": request,
            "user": current_user,
            "rows": rows,
            "customers": customers,
            "q": q or "",
            "selected_customer_id": int(customer_id) if customer_id else None,
            "selected_status": status or "",
        },
    )

@app.get("/co/new")
def co_new(request: Request, db: Session = Depends(get_db), current_user=Depends(require_user)):
    customers = db.execute(select(Customer).order_by(Customer.name)).scalars().all()
    return templates.TemplateResponse("co_form.html", {"request": request, "user": current_user, "customers": customers})

@app.post("/co/new")
def co_new_post(
    request: Request,
    customer_id: int = Form(...), code: str = Form(...), notes: str = Form(""),
    db: Session = Depends(get_db), current_user=Depends(require_user)
):
    cust = db.get(Customer, int(customer_id))
    if not cust:
        raise HTTPException(status_code=400, detail="Ugyldig kunde")
    crud.create_customer_order(db, cust, code=code, notes=notes)
    return RedirectResponse(url="/co", status_code=303)

@app.get("/co/{co_id}")
def co_detail(request: Request, co_id: int, db: Session = Depends(get_db), current_user=Depends(require_user)):
    co = db.get(CustomerOrder, co_id)
    if not co:
        raise HTTPException(status_code=404)
    lines = db.execute(select(CustomerOrderLine).where(CustomerOrderLine.co_id == co.id)).scalars().all()
    return templates.TemplateResponse("co_detail.html", {"request": request, "user": current_user, "co": co, "lines": lines})

@app.get("/co/{co_id}/delete")
def co_delete_confirm(request: Request, co_id: int, db: Session = Depends(get_db), current_user=Depends(require_user)):
    co = db.get(CustomerOrder, co_id)
    if not co:
        raise HTTPException(status_code=404)
    reserved_cnt = db.execute(select(func.count(ItemUnit.id)).where(ItemUnit.reserved_co_id == co.id, ItemUnit.status.in_(("reserved","reservert")))).scalar() or 0
    lines_cnt = db.execute(select(func.count(CustomerOrderLine.id)).where(CustomerOrderLine.co_id == co.id)).scalar() or 0
    return templates.TemplateResponse(
        "co_delete.html",
        {
            "request": request,
            "user": current_user,
            "co": co,
            "reserved_cnt": int(reserved_cnt),
            "lines_cnt": int(lines_cnt),
            "error": None,
        },
    )

@app.post("/co/{co_id}/delete")
def co_delete_post(
    request: Request,
    co_id: int,
    confirm: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(require_user),
):
    co = db.get(CustomerOrder, co_id)
    if not co:
        raise HTTPException(status_code=404)
    try:
        crud.delete_customer_order(db, co, confirm_code=confirm.strip())
        return RedirectResponse(url="/co", status_code=303)
    except HTTPException as e:
        if e.status_code == 400:
            reserved_cnt = db.execute(select(func.count(ItemUnit.id)).where(ItemUnit.reserved_co_id == co.id, ItemUnit.status.in_(("reserved","reservert")))).scalar() or 0
            lines_cnt = db.execute(select(func.count(CustomerOrderLine.id)).where(CustomerOrderLine.co_id == co.id)).scalar() or 0
            return templates.TemplateResponse(
                "co_delete.html",
                {
                    "request": request,
                    "user": current_user,
                    "co": co,
                    "reserved_cnt": int(reserved_cnt),
                    "lines_cnt": int(lines_cnt),
                    "error": e.detail,
                },
                status_code=400,
            )
        raise

@app.post("/item/{item_id}/reserve")
def item_reserve(
    request: Request, item_id: int,
    co_code: str = Form(...),
    qty: int = Form(...),
    note: str = Form("Reservasjon"),
    db: Session = Depends(get_db), current_user=Depends(require_user)
):
    item = db.get(Item, item_id)
    if not item:
        raise HTTPException(status_code=404)
    co = crud.get_or_create_co_by_code(db, co_code.strip(), None)
    crud.reserve_units(db, item, co, qty=int(qty), note=note, actor=current_user)
    return RedirectResponse(url=f"/item/{item.id}/units", status_code=303)

@app.post("/co/{co_id}/release")
def co_release(
    request: Request, co_id: int,
    item_id: int = Form(...),
    qty: int = Form(...),
    note: str = Form("Frigitt reservasjon"),
    db: Session = Depends(get_db), current_user=Depends(require_user)
):
    co = db.get(CustomerOrder, co_id); item = db.get(Item, int(item_id))
    if not co or not item:
        raise HTTPException(status_code=404)
    crud.release_units(db, item, co, qty=int(qty), note=note, actor=current_user)
    return RedirectResponse(url=f"/co/{co.id}", status_code=303)

@app.post("/co/{co_id}/fulfill")
def co_fulfill(
    request: Request, co_id: int,
    item_id: int = Form(...),
    qty: int = Form(...),
    note: str = Form("Utlevert"),
    db: Session = Depends(get_db), current_user=Depends(require_user)
):
    co = db.get(CustomerOrder, co_id); item = db.get(Item, int(item_id))
    if not co or not item:
        raise HTTPException(status_code=404)
    crud.fulfill_units(db, item, co, qty=int(qty), note=note, actor=current_user)
    return RedirectResponse(url=f"/co/{co.id}", status_code=303)

@app.post("/co/{co_id}/reserve")
def co_reserve(
    request: Request, co_id: int,
    item_id: int = Form(...),
    qty: int = Form(...),
    note: str = Form("Reservert"),
    db: Session = Depends(get_db), current_user=Depends(require_user)
):
    co = db.get(CustomerOrder, co_id); item = db.get(Item, int(item_id))
    if not co or not item:
        raise HTTPException(status_code=404)
    crud.reserve_qty_for_customer(db, item_id=item.id, qty=int(qty), customer_id=(co.customer_id or 0), note=note, actor=current_user, co_id=co.id)
    return RedirectResponse(url=f"/co/{co.id}", status_code=303)

@app.post("/co/{co_id}/unfulfill")
def co_unfulfill(
    request: Request, co_id: int,
    item_id: int = Form(...),
    qty: int = Form(...),
    note: str = Form("Tilbakeført"),
    db: Session = Depends(get_db), current_user=Depends(require_user)
):
    co = db.get(CustomerOrder, co_id); item = db.get(Item, int(item_id))
    if not co or not item:
        raise HTTPException(status_code=404)
    crud.unfulfill_units(db, item, co, qty=int(qty), note=note, actor=current_user)
    return RedirectResponse(url=f"/co/{co.id}", status_code=303)

@app.post("/co/{co_id}/line/order")
def co_line_order(
    request: Request, co_id: int,
    item_id: int = Form(...),
    qty: int = Form(...),
    note: str = Form("Bestilt"),
    db: Session = Depends(get_db), current_user=Depends(require_user)
):
    co = db.get(CustomerOrder, co_id); item = db.get(Item, int(item_id))
    if not co or not item:
        raise HTTPException(status_code=404)
    line = crud.ensure_line(db, co, item)
    line.qty = (line.qty or 0) + int(qty)
    db.add(Tx(item_id=item.id, sku=item.sku, name=item.name, delta=0, note=f"{note} {qty} stk for CO {co.code}", co_id=co.id, user_id=None, user_name=None))
    db.commit()
    return RedirectResponse(url=f"/co/{co.id}", status_code=303)

@app.post("/co/{co_id}/line/delete")
def co_line_delete(
    request: Request, co_id: int,
    item_id: int = Form(...),
    db: Session = Depends(get_db), current_user=Depends(require_user)
):
    co = db.get(CustomerOrder, co_id); item = db.get(Item, int(item_id))
    if not co or not item:
        raise HTTPException(status_code=404)
    crud.delete_co_line(db, co, item, actor=current_user)
    return RedirectResponse(url=f"/co/{co.id}", status_code=303)

@app.post("/co/{co_id}/receive")
def co_receive(
    request: Request, co_id: int,
    item_id: int = Form(...),
    qty: int = Form(...),
    po_code: str = Form(...),
    note: str = Form("Mottak"),
    db: Session = Depends(get_db), current_user=Depends(require_user)
):
    co = db.get(CustomerOrder, co_id); item = db.get(Item, int(item_id))
    if not co or not item:
        raise HTTPException(status_code=404)
    crud.create_units_for_receive(db, item, qty=int(qty), po_code=po_code.strip(), note=f"{note} (CO {co.code})", actor=current_user)
    return RedirectResponse(url=f"/co/{co.id}", status_code=303)
