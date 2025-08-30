# app/auth.py
import os
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Request, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from passlib.context import CryptContext

from .db import SessionLocal
from .models import User
from .db import Base, engine

pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")

router = APIRouter()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

# ---------- DB dependency ----------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ---------- Helpers ----------
def hash_password(pw: str) -> str:
    return pwd.hash(pw)

def verify_password(pw: str, pw_hash: str) -> bool:
    try:
        return pwd.verify(pw, pw_hash)
    except Exception:
        return False

# Guards
async def require_user(request: Request, db: Session = Depends(get_db)) -> User:
    uid = request.session.get("uid")
    if not uid:
        # hard redirect
        raise HTTPException(status_code=303, headers={"Location": "/auth/login"})
    user = db.get(User, uid)
    if not user:
        request.session.clear()
        raise HTTPException(status_code=303, headers={"Location": "/auth/login"})
    return user

async def require_admin(current_user: User = Depends(require_user)) -> User:
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Forbudt")
    return current_user

# ---------- Auth routes ----------
@router.get("/auth/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("auth_login.html", {"request": request, "error": ""})

@router.post("/auth/login")
def login_post(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.execute(select(User).where(User.email == email.lower().strip())).scalar_one_or_none()
    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse("auth_login.html", {"request": request, "error": "Feil e‑post eller passord."}, status_code=401)
    request.session["uid"] = user.id
    return RedirectResponse(url="/", status_code=303)

@router.post("/auth/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/auth/login", status_code=303)

# Bootstrap første admin
@router.get("/auth/bootstrap", response_class=HTMLResponse)
def bootstrap_page(request: Request, db: Session = Depends(get_db)):
    count = db.execute(select(func.count(User.id))).scalar_one()
    if count > 0:
        return RedirectResponse(url="/auth/login", status_code=303)
    return templates.TemplateResponse("auth_bootstrap.html", {"request": request, "error": ""})

@router.post("/auth/bootstrap")
def bootstrap_post(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    count = db.execute(select(func.count(User.id))).scalar_one()
    if count > 0:
        return RedirectResponse(url="/auth/login", status_code=303)

    email = email.lower().strip()
    user = User(name=name.strip(), email=email, role="admin", password_hash=hash_password(password))
    db.add(user)
    db.commit()
    db.refresh(user)
    request.session["uid"] = user.id
    return RedirectResponse(url="/", status_code=303)

# ---------- ADMIN: user management ----------
@router.get("/admin/users", response_class=HTMLResponse)
def users_list(request: Request, db: Session = Depends(get_db), current_user: User = Depends(require_admin)):
    rows = db.execute(select(User).order_by(User.created_at.desc())).scalars().all()
    return templates.TemplateResponse("admin_users_list.html", {"request": request, "user": current_user, "rows": rows})

@router.get("/admin/users/new", response_class=HTMLResponse)
def users_new_page(request: Request, current_user: User = Depends(require_admin)):
    return templates.TemplateResponse("admin_user_form.html", {"request": request, "user": current_user, "editing": False, "row": None})

@router.post("/admin/users/new")
def users_new_post(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    role: str = Form("user"),
    password: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    email = email.lower().strip()
    if role not in ("user", "admin"):
        role = "user"
    existing = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if existing:
        return templates.TemplateResponse("admin_user_form.html", {"request": request, "user": current_user, "editing": False, "row": None, "error": "E‑post er allerede i bruk."}, status_code=400)
    u = User(name=name.strip(), email=email, role=role, password_hash=hash_password(password))
    db.add(u); db.commit(); db.refresh(u)
    return RedirectResponse(url="/admin/users", status_code=303)

@router.get("/admin/users/{uid}/edit", response_class=HTMLResponse)
def users_edit_page(uid: int, request: Request, db: Session = Depends(get_db), current_user: User = Depends(require_admin)):
    row = db.get(User, uid)
    if not row:
        raise HTTPException(404)
    return templates.TemplateResponse("admin_user_form.html", {"request": request, "user": current_user, "editing": True, "row": row})

@router.post("/admin/users/{uid}/edit")
def users_edit_post(
    uid: int,
    request: Request,
    name: str = Form(...),
    role: str = Form("user"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    if role not in ("user", "admin"):
        role = "user"
    row = db.get(User, uid)
    if not row:
        raise HTTPException(404)
    row.name = name.strip()
    row.role = role
    db.add(row); db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)

@router.get("/admin/users/{uid}/resetpw", response_class=HTMLResponse)
def users_resetpw_page(uid: int, request: Request, db: Session = Depends(get_db), current_user: User = Depends(require_admin)):
    row = db.get(User, uid)
    if not row:
        raise HTTPException(404)
    return templates.TemplateResponse("admin_user_resetpw.html", {"request": request, "user": current_user, "row": row})

@router.post("/admin/users/{uid}/resetpw")
def users_resetpw_post(
    uid: int,
    request: Request,
    password: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    row = db.get(User, uid)
    if not row:
        raise HTTPException(404)
    row.password_hash = hash_password(password)
    db.add(row); db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)

@router.post("/admin/users/{uid}/delete")
def users_delete(uid: int, request: Request, db: Session = Depends(get_db), current_user: User = Depends(require_admin)):
    row = db.get(User, uid)
    if not row:
        raise HTTPException(404)
    # ikke tillat å slette seg selv for å unngå lockout
    if row.id == current_user.id:
        raise HTTPException(status_code=400, detail="Kan ikke slette egen konto.")
    db.delete(row); db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)
