from sqlalchemy import Column, Integer, String, Float, Text, DateTime, ForeignKey
from sqlalchemy.orm import relationship, Mapped, mapped_column
from datetime import datetime
from .db import Base
from typing import Optional

class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="user")  # 'admin' eller 'user'
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class Category(Base):
    __tablename__ = "categories"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)

    items = relationship("Item", back_populates="category_obj")

class Location(Base):
    __tablename__ = "locations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)

    items = relationship("Item", back_populates="location_obj")

class Item(Base):
    __tablename__ = "items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    sku: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    qty: Mapped[int] = mapped_column(Integer, default=0)
    min_qty: Mapped[int] = mapped_column(Integer, default=0)
    price: Mapped[float] = mapped_column(Float, default=0.0)
    currency: Mapped[str] = mapped_column(String(8), default="NOK")
    notes: Mapped[str] = mapped_column(Text, default="")
    image_path: Mapped[str] = mapped_column(String(300), default="")

    category_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("categories.id"))
    location_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("locations.id"))

    category_obj = relationship("Category", back_populates="items")
    location_obj = relationship("Location", back_populates="items")

    last_updated: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class Tx(Base):
    __tablename__ = "transactions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id = mapped_column(Integer, ForeignKey("items.id", ondelete="SET NULL"), nullable=True)
    sku: Mapped[str] = mapped_column(String(120), index=True)
    name: Mapped[str] = mapped_column(String(200))
    delta: Mapped[int] = mapped_column(Integer)
    note: Mapped[str] = mapped_column(String(200), default="")
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # audit
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    user_name: Mapped[str | None] = mapped_column(String(120), default=None)

    # nye koblinger for sporing
    unit_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("item_units.id"), nullable=True)
    po_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("purchase_orders.id"), nullable=True)
    co_id: Mapped[Optional[int]] = mapped_column(ForeignKey("customer_orders.id", ondelete="SET NULL"), nullable=True)

    item = relationship("Item")


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(80), unique=True, index=True)     # f.eks. PO-2025-001
    supplier: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class PurchaseOrderLine(Base):
    __tablename__ = "purchase_order_lines"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    po_id: Mapped[int] = mapped_column(Integer, ForeignKey("purchase_orders.id"))
    item_id = mapped_column(Integer, ForeignKey("items.id", ondelete="SET NULL"), nullable=True)
    qty_ordered: Mapped[int] = mapped_column(Integer, default=0)
    qty_received: Mapped[int] = mapped_column(Integer, default=0)

    po = relationship("PurchaseOrder")
    item = relationship("Item")

class CustomerOrder(Base):
    __tablename__ = "customer_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(20), default="open")  # open|fulfilled|cancelled
    notes: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    customer: Mapped[customer] = relationship("Customer")

class ItemUnit(Base):
    __tablename__ = "item_units"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id = mapped_column(Integer, ForeignKey("items.id", ondelete="SET NULL"), nullable=True)
    # Hvilken bestillingsordre enheten kom inn på (opprinnelse)
    po_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("purchase_orders.id", ondelete="SET NULL"), nullable=True)
    reserved_co_id: Mapped[Optional[int]] = mapped_column(ForeignKey("customer_orders.id", ondelete="SET NULL"), nullable=True)

    status: Mapped[str] = mapped_column(String(20), default="available")  # available | reserved | used
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    item = relationship("Item")
    po = relationship("PurchaseOrder", lazy="joined")
    reserved_co: Mapped[Optional[CustomerOrder]] = relationship("CustomerOrder", foreign_keys=[reserved_co_id])

class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str] = mapped_column(String(200), default="")
    phone: Mapped[str] = mapped_column(String(50), default="")
    notes: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class CustomerOrderLine(Base):
    __tablename__ = "customer_order_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    co_id: Mapped[int] = mapped_column(ForeignKey("customer_orders.id", ondelete="CASCADE"))
    item_id: Mapped[Optional[int]] = mapped_column(ForeignKey("items.id", ondelete="SET NULL"), nullable=True)

    qty_ordered: Mapped[int] = mapped_column(Integer, default=0)
    qty_reserved: Mapped[int] = mapped_column(Integer, default=0)
    qty_fulfilled: Mapped[int] = mapped_column(Integer, default=0)