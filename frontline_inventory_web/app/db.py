import os
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, DeclarativeBase

DB_PATH = os.environ.get("INV_DB", os.path.join(os.path.dirname(os.path.dirname(__file__)), "..", "inventory.db"))
DB_URL = f"sqlite:///{DB_PATH}"
print("🔧 INVENTORY DB:", DB_PATH)


class Base(DeclarativeBase):
    pass


engine = create_engine(DB_URL, connect_args={"check_same_thread": False}, future=True)


@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cur = dbapi_connection.cursor()
    cur.execute("PRAGMA journal_mode=WAL;")
    cur.execute("PRAGMA foreign_keys=ON;")
    cur.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, future=True)


def ensure_migrations():
    import sqlite3

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys=ON;")
    cur = conn.cursor()

    # ------------------------------------------------------------
    # customers (opprett hvis mangler)
    # ------------------------------------------------------------
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='customers'")
    if not cur.fetchone():
        cur.execute("""
            CREATE TABLE customers (
                id INTEGER PRIMARY KEY,
                name VARCHAR(200) NOT NULL,
                email VARCHAR(200) DEFAULT '',
                phone VARCHAR(50) DEFAULT '',
                notes VARCHAR(500) DEFAULT '',
                created_at DATETIME
            )
        """)

    # ------------------------------------------------------------
    # customer_orders – fjern legacy 'customer' kolonne og sikre nytt skjema
    # ------------------------------------------------------------
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='customer_orders'")
    if cur.fetchone():
        # Finn nåværende kolonner
        cur.execute("PRAGMA table_info(customer_orders)")
        info = cur.fetchall()  # [cid, name, type, notnull, dflt_value, pk]
        colnames = [r[1] for r in info]
        has_legacy_customer = "customer" in colnames

        if has_legacy_customer:
            # Les gamle rader robust (noen DB-er mangler enkelte kolonner)
            select_sqls = [
                "SELECT id, code, customer, status, notes, created_at FROM customer_orders",
                "SELECT id, code, customer, status, '' as notes, created_at FROM customer_orders",
                "SELECT id, code, customer, 'open' as status, '' as notes, NULL as created_at FROM customer_orders",
            ]
            rows = None
            for sql in select_sqls:
                try:
                    cur.execute(sql)
                    rows = cur.fetchall()
                    break
                except sqlite3.OperationalError:
                    continue
            if rows is None:
                rows = []

            # Hent kundeliste for mapping name -> id
            cur.execute("SELECT id, name FROM customers")
            name_to_id = {name: cid for (cid, name) in cur.fetchall()}

            # Recreate uten legacy-kolonnen, styrt av Python-transaksjon
            cur.execute("PRAGMA foreign_keys=OFF")
            conn.execute("BEGIN")
            cur.execute("""
                CREATE TABLE customer_orders_new (
                    id INTEGER PRIMARY KEY,
                    code VARCHAR(120) NOT NULL UNIQUE,
                    customer_id INTEGER NULL,
                    status VARCHAR(20) DEFAULT 'open',
                    notes VARCHAR(500) DEFAULT '',
                    created_at DATETIME
                )
            """)

            if rows:
                insert_vals = []
                for (row_id, code, legacy_name, status, notes, created_at) in rows:
                    mapped_id = name_to_id.get(legacy_name) if legacy_name is not None else None
                    insert_vals.append((row_id, code, mapped_id, status or "open", notes or "", created_at))
                cur.executemany("""
                    INSERT INTO customer_orders_new
                    (id, code, customer_id, status, notes, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, insert_vals)

            cur.execute("DROP TABLE customer_orders")
            cur.execute("ALTER TABLE customer_orders_new RENAME TO customer_orders")
            conn.commit()
            cur.execute("PRAGMA foreign_keys=ON")

        # Legg til manglende kolonner/indeks
        cur.execute("PRAGMA table_info(customer_orders)")
        co_cols = [r[1] for r in cur.fetchall()]
        if "customer_id" not in co_cols:
            cur.execute("ALTER TABLE customer_orders ADD COLUMN customer_id INTEGER NULL")
        if "status" not in co_cols:
            cur.execute("ALTER TABLE customer_orders ADD COLUMN status VARCHAR(20) DEFAULT 'open'")
        if "notes" not in co_cols:
            cur.execute("ALTER TABLE customer_orders ADD COLUMN notes VARCHAR(500) DEFAULT ''")
        if "created_at" not in co_cols:
            cur.execute("ALTER TABLE customer_orders ADD COLUMN created_at DATETIME NULL")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_co_customer_status ON customer_orders(customer_id, status)")
    else:
        # Finnes ikke: lag komplett skjema
        cur.execute("""
            CREATE TABLE customer_orders (
                id INTEGER PRIMARY KEY,
                code VARCHAR(120) NOT NULL UNIQUE,
                customer_id INTEGER NULL,
                status VARCHAR(20) DEFAULT 'open',
                notes VARCHAR(500) DEFAULT '',
                created_at DATETIME
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS ix_co_customer_status ON customer_orders(customer_id, status)")

    # ------------------------------------------------------------
    # transactions – nullable item_id + ON DELETE SET NULL + nye kolonner
    # ------------------------------------------------------------
    cur.execute("PRAGMA table_info(transactions)")
    if cur.fetchall():
        cur.execute("PRAGMA table_info(transactions)")
        t_cols_all = cur.fetchall()
        t_names = [r[1] for r in t_cols_all]
        # Sørg for nye kolonner
        for addcol in ("user_id", "user_name", "unit_id", "po_id", "co_id"):
            if addcol not in t_names:
                cur.execute(f"ALTER TABLE transactions ADD COLUMN {addcol} {'INTEGER' if addcol.endswith('_id') else 'VARCHAR(120)'}")

        cur.execute("PRAGMA table_info(transactions)")
        t_cols_all = cur.fetchall()
        t_item_notnull = next((r[3] for r in t_cols_all if r[1] == "item_id"), None)
        cur.execute("PRAGMA foreign_key_list(transactions)")
        t_fks = cur.fetchall()
        needs_on_delete = True
        for (_, _, _table, from_col, _to_col, _seq, on_delete, _match) in t_fks:
            if from_col == "item_id" and (on_delete or "").upper() == "SET NULL":
                needs_on_delete = False
                break

        if (t_item_notnull == 1) or needs_on_delete:
            cur.execute("SELECT id, item_id, sku, name, delta, note, ts, user_id, user_name, unit_id, po_id, co_id FROM transactions")
            rows = cur.fetchall()

            cur.execute("PRAGMA foreign_keys=OFF")
            conn.execute("BEGIN")
            cur.execute("""
                CREATE TABLE transactions_new (
                    id INTEGER PRIMARY KEY,
                    item_id INTEGER NULL,
                    sku VARCHAR(120),
                    name VARCHAR(200),
                    delta INTEGER,
                    note VARCHAR(200) DEFAULT '',
                    ts DATETIME,
                    user_id INTEGER NULL,
                    user_name VARCHAR(120) NULL,
                    unit_id INTEGER NULL,
                    po_id INTEGER NULL,
                    co_id INTEGER NULL,
                    FOREIGN KEY(item_id) REFERENCES items(id) ON DELETE SET NULL
                )
            """)
            if rows:
                cur.executemany("""
                    INSERT INTO transactions_new
                    (id, item_id, sku, name, delta, note, ts, user_id, user_name, unit_id, po_id, co_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, rows)
            cur.execute("DROP TABLE transactions")
            cur.execute("ALTER TABLE transactions_new RENAME TO transactions")
            conn.commit()
            cur.execute("PRAGMA foreign_keys=ON")

    # ------------------------------------------------------------
    # item_units – nullable item_id + ON DELETE SET NULL
    # ------------------------------------------------------------
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='item_units'")
    if cur.fetchone():
        cur.execute("PRAGMA table_info(item_units)")
        iu_cols_all = cur.fetchall()
        iu_item_notnull = next((r[3] for r in iu_cols_all if r[1] == "item_id"), None)
        cur.execute("PRAGMA foreign_key_list(item_units)")
        fks = cur.fetchall()
        needs_on_delete = True
        for (_, _, _table, from_col, _to_col, _seq, on_delete, _match) in fks:
            if from_col == "item_id" and (on_delete or "").upper() == "SET NULL":
                needs_on_delete = False
                break

        if (iu_item_notnull == 1) or needs_on_delete:
            cur.execute("SELECT id, item_id, po_id, reserved_co_id, status, created_at, used_at FROM item_units")
            rows = cur.fetchall()

            cur.execute("PRAGMA foreign_keys=OFF")
            conn.execute("BEGIN")
            cur.execute("""
                CREATE TABLE item_units_new (
                    id INTEGER PRIMARY KEY,
                    item_id INTEGER NULL,
                    po_id INTEGER NULL,
                    reserved_co_id INTEGER NULL,
                    status VARCHAR(20),
                    created_at DATETIME,
                    used_at DATETIME,
                    FOREIGN KEY(item_id) REFERENCES items(id) ON DELETE SET NULL
                )
            """)
            if rows:
                cur.executemany("""
                    INSERT INTO item_units_new
                    (id, item_id, po_id, reserved_co_id, status, created_at, used_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, rows)
            cur.execute("DROP TABLE item_units")
            cur.execute("ALTER TABLE item_units_new RENAME TO item_units")
            conn.commit()
            cur.execute("PRAGMA foreign_keys=ON")

    # Ekstra kolonner på item_units
    cur.execute("PRAGMA table_info(item_units)")
    if cur.fetchall():
        cur.execute("PRAGMA table_info(item_units)")
        iu_cols_all = cur.fetchall()
        iu_names = [c[1] for c in iu_cols_all]
        if "reserved_co_id" not in iu_names:
            cur.execute("ALTER TABLE item_units ADD COLUMN reserved_co_id INTEGER NULL")
        # Valgfri: reserved_customer_id hvis du vil ha direkte kobling til kunde
        if "reserved_customer_id" not in iu_names:
            cur.execute("ALTER TABLE item_units ADD COLUMN reserved_customer_id INTEGER NULL")

    # ------------------------------------------------------------
    # purchase_order_lines – nullable item_id + ON DELETE SET NULL
    # ------------------------------------------------------------
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='purchase_order_lines'")
    if cur.fetchone():
        cur.execute("PRAGMA table_info(purchase_order_lines)")
        pol_cols_all = cur.fetchall()
        pol_item_notnull = next((r[3] for r in pol_cols_all if r[1] == "item_id"), None)
        cur.execute("PRAGMA foreign_key_list(purchase_order_lines)")
        fks = cur.fetchall()
        needs_on_delete = True
        for (_, _, _table, from_col, _to_col, _seq, on_delete, _match) in fks:
            if from_col == "item_id" and (on_delete or "").upper() == "SET NULL":
                needs_on_delete = False
                break

        if (pol_item_notnull == 1) or needs_on_delete:
            cur.execute("SELECT id, po_id, item_id, qty_ordered, qty_received FROM purchase_order_lines")
            rows = cur.fetchall()

            cur.execute("PRAGMA foreign_keys=OFF")
            conn.execute("BEGIN")
            cur.execute("""
                CREATE TABLE purchase_order_lines_new (
                    id INTEGER PRIMARY KEY,
                    po_id INTEGER NOT NULL,
                    item_id INTEGER NULL,
                    qty_ordered INTEGER DEFAULT 0,
                    qty_received INTEGER DEFAULT 0,
                    FOREIGN KEY(item_id) REFERENCES items(id) ON DELETE SET NULL
                )
            """)
            if rows:
                cur.executemany("""
                    INSERT INTO purchase_order_lines_new
                    (id, po_id, item_id, qty_ordered, qty_received)
                    VALUES (?, ?, ?, ?, ?)
                """, rows)
            cur.execute("DROP TABLE purchase_order_lines")
            cur.execute("ALTER TABLE purchase_order_lines_new RENAME TO purchase_order_lines")
            conn.commit()
            cur.execute("PRAGMA foreign_keys=ON")

# ------------------------------------------------------------
# customer_order_lines
# ------------------------------------------------------------
    cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='customer_order_lines'")
    exists = cur.fetchone() is not None

    if not exists:
    # Lag tabell med begge kolonnene
        cur.execute("""
        CREATE TABLE customer_order_lines (
            id INTEGER PRIMARY KEY,
            co_id INTEGER NOT NULL,
            item_id INTEGER NULL,
            qty_ordered INTEGER NOT NULL DEFAULT 1,
            qty_reserved INTEGER NOT NULL DEFAULT 0,
            qty_fulfilled INTEGER NOT NULL DEFAULT 0,
            notes VARCHAR(500) DEFAULT '',
            created_at DATETIME,
            FOREIGN KEY(co_id) REFERENCES customer_orders(id) ON DELETE CASCADE,
            FOREIGN KEY(item_id) REFERENCES items(id) ON DELETE SET NULL
        )
    """)
    else:
        cur.execute("PRAGMA table_info(customer_order_lines)")
    cols = [r[1] for r in cur.fetchall()]

    # hvis eldre db har 'qty' men ikke 'qty_ordered'
    if "qty_ordered" not in cols:
        cur.execute("ALTER TABLE customer_order_lines ADD COLUMN qty_ordered INTEGER")
        if "qty" in cols:
            cur.execute("UPDATE customer_order_lines SET qty_ordered = COALESCE(qty, 1)")
        else:
            cur.execute("UPDATE customer_order_lines SET qty_ordered = 1")
        cur.execute("UPDATE customer_order_lines SET qty_ordered = 1 WHERE qty_ordered IS NULL")

    if "qty_reserved" not in cols:
        cur.execute("ALTER TABLE customer_order_lines ADD COLUMN qty_reserved INTEGER")
        cur.execute("UPDATE customer_order_lines SET qty_reserved = 0 WHERE qty_reserved IS NULL")

    if "notes" not in cols:
        cur.execute("ALTER TABLE customer_order_lines ADD COLUMN notes VARCHAR(500) DEFAULT ''")
    if "created_at" not in cols:
        cur.execute("ALTER TABLE customer_order_lines ADD COLUMN created_at DATETIME")
    if "qty_fulfilled" not in cols:
        cur.execute("ALTER TABLE customer_order_lines ADD COLUMN qty_fulfilled INTEGER")
        cur.execute("UPDATE customer_order_lines SET qty_fulfilled = 0 WHERE qty_fulfilled IS NULL")

# Indekser
    cur.execute("CREATE INDEX IF NOT EXISTS ix_col_co ON customer_order_lines(co_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_col_item ON customer_order_lines(item_id)")

    conn.commit()
    conn.close()
    print("✅ Migrations sjekket/utført.")