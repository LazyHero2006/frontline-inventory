import os
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, DeclarativeBase

DB_PATH = os.environ.get("INV_DB", os.path.join(os.path.dirname(os.path.dirname(__file__)), "..", "inventory.db"))
DB_URL = f"sqlite:///{DB_PATH}"
print("🔧 INVENTORY DB:", DB_PATH)

class Base(DeclarativeBase):
    pass

engine = create_engine(DB_URL, connect_args={"check_same_thread": False}, future=True)

# Enable WAL for better concurrency
@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA foreign_keys=ON;")
    cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, future=True)

# app/db.py
def ensure_migrations():
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys=ON;")
    cur = conn.cursor()

    # --- transactions: sørg for kolonner + nullable item_id + SET NULL ---
    cur.execute("PRAGMA table_info(transactions)")
    cols = cur.fetchall()
    names = [r[1] for r in cols]
    for addcol in ("user_id", "user_name", "unit_id", "po_id", "co_id"):
        if addcol not in names:
            cur.execute(f"ALTER TABLE transactions ADD COLUMN {addcol} {'INTEGER' if addcol.endswith('_id') else 'VARCHAR(120)'}")
    conn.commit()

    cur.execute("PRAGMA table_info(transactions)")
    cols = cur.fetchall()
    t_item_notnull = next((r[3] for r in cols if r[1] == "item_id"), None)
    cur.execute("PRAGMA foreign_key_list(transactions)")
    t_fks = cur.fetchall()
    t_needs_on_delete = True
    for (_, _, table, from_col, _, _, on_delete, _) in t_fks:
        if from_col == "item_id" and (on_delete or "").upper() == "SET NULL":
            t_needs_on_delete = False
            break

    if (t_item_notnull == 1) or t_needs_on_delete:
        cur.execute("SELECT id, item_id, sku, name, delta, note, ts, user_id, user_name, unit_id, po_id, co_id FROM transactions")
        data = cur.fetchall()
        cur.executescript("""
        PRAGMA foreign_keys=OFF;
        BEGIN TRANSACTION;
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
        );
        """)
        cur.executemany("""
            INSERT INTO transactions_new
            (id, item_id, sku, name, delta, note, ts, user_id, user_name, unit_id, po_id, co_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, data)
        cur.executescript("""
        DROP TABLE transactions;
        ALTER TABLE transactions_new RENAME TO transactions;
        COMMIT;
        PRAGMA foreign_keys=ON;
        """)
        conn.commit()

    # --- item_units: nullable item_id + SET NULL ---
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='item_units'")
    if cur.fetchone():
        cur.execute("PRAGMA table_info(item_units)")
        cols = cur.fetchall()
        iu_item_notnull = next((r[3] for r in cols if r[1] == "item_id"), None)
        cur.execute("PRAGMA foreign_key_list(item_units)")
        fks = cur.fetchall()
        iu_needs_on_delete = True
        for (_, _, table, from_col, _, _, on_delete, _) in fks:
            if from_col == "item_id" and (on_delete or "").upper() == "SET NULL":
                iu_needs_on_delete = False
                break
        if (iu_item_notnull == 1) or iu_needs_on_delete:
            cur.execute("SELECT id, item_id, po_id, reserved_co_id, status, created_at, used_at FROM item_units")
            data = cur.fetchall()
            cur.executescript("""
            PRAGMA foreign_keys=OFF;
            BEGIN TRANSACTION;
            CREATE TABLE item_units_new (
                id INTEGER PRIMARY KEY,
                item_id INTEGER NULL,
                po_id INTEGER NULL,
                reserved_co_id INTEGER NULL,
                status VARCHAR(20),
                created_at DATETIME,
                used_at DATETIME,
                FOREIGN KEY(item_id) REFERENCES items(id) ON DELETE SET NULL
            );
            """)
            cur.executemany("""
                INSERT INTO item_units_new
                (id, item_id, po_id, reserved_co_id, status, created_at, used_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, data)
            cur.executescript("""
            DROP TABLE item_units;
            ALTER TABLE item_units_new RENAME TO item_units;
            COMMIT;
            PRAGMA foreign_keys=ON;
            """)
            conn.commit()

    # --- purchase_order_lines: nullable item_id + SET NULL ---
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='purchase_order_lines'")
    if cur.fetchone():
        cur.execute("PRAGMA table_info(purchase_order_lines)")
        cols = cur.fetchall()
        pol_item_notnull = next((r[3] for r in cols if r[1] == "item_id"), None)
        cur.execute("PRAGMA foreign_key_list(purchase_order_lines)")
        fks = cur.fetchall()
        pol_needs_on_delete = True
        for (_, _, table, from_col, _, _, on_delete, _) in fks:
            if from_col == "item_id" and (on_delete or "").upper() == "SET NULL":
                pol_needs_on_delete = False
                break
        if (pol_item_notnull == 1) or pol_needs_on_delete:
            cur.execute("SELECT id, po_id, item_id, qty_ordered, qty_received FROM purchase_order_lines")
            data = cur.fetchall()
            cur.executescript("""
            PRAGMA foreign_keys=OFF;
            BEGIN TRANSACTION;
            CREATE TABLE purchase_order_lines_new (
                id INTEGER PRIMARY KEY,
                po_id INTEGER NOT NULL,
                item_id INTEGER NULL,
                qty_ordered INTEGER DEFAULT 0,
                qty_received INTEGER DEFAULT 0,
                FOREIGN KEY(item_id) REFERENCES items(id) ON DELETE SET NULL
            );
            """)
            cur.executemany("""
                INSERT INTO purchase_order_lines_new
                (id, po_id, item_id, qty_ordered, qty_received)
                VALUES (?, ?, ?, ?, ?)
            """, data)
            cur.executescript("""
            DROP TABLE purchase_order_lines;
            ALTER TABLE purchase_order_lines_new RENAME TO purchase_order_lines;
            COMMIT;
            PRAGMA foreign_keys=ON;
            """)
            conn.commit()

    # --- customers ---
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='customers'")
    if not cur.fetchone():
        cur.executescript("""
        CREATE TABLE customers (
            id INTEGER PRIMARY KEY,
            name VARCHAR(200) NOT NULL,
            email VARCHAR(200) DEFAULT '',
            phone VARCHAR(50) DEFAULT '',
            notes VARCHAR(500) DEFAULT '',
            created_at DATETIME
        );
        """)

    # --- customer_orders ---
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='customer_orders'")
    if not cur.fetchone():
        cur.executescript("""
        CREATE TABLE customer_orders (
            id INTEGER PRIMARY KEY,
            code VARCHAR(120) NOT NULL UNIQUE,
            customer_id INTEGER NOT NULL,
            status VARCHAR(20) DEFAULT 'open',
            notes VARCHAR(500) DEFAULT '',
            created_at DATETIME,
            FOREIGN KEY(customer_id) REFERENCES customers(id) ON DELETE CASCADE
        );
        """)

    # --- customer_order_lines ---
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='customer_order_lines'")
    if not cur.fetchone():
        cur.executescript("""
        CREATE TABLE customer_order_lines (
            id INTEGER PRIMARY KEY,
            co_id INTEGER NOT NULL,
            item_id INTEGER NULL,
            unit_id INTEGER NULL,
            qty INTEGER DEFAULT 1,
            notes VARCHAR(500) DEFAULT '',
            created_at DATETIME,
            FOREIGN KEY(co_id) REFERENCES customer_orders(id) ON DELETE CASCADE,
            FOREIGN KEY(item_id) REFERENCES items(id) ON DELETE SET NULL,
            FOREIGN KEY(unit_id) REFERENCES item_units(id) ON DELETE SET NULL
        );
        """)

    # reserved_customer_id på item_units (ny kolonne)
    cur.execute("PRAGMA table_info(item_units)")
    iu_cols = cur.fetchall()
    iu_names = [c[1] for c in iu_cols]
    if "reserved_customer_id" not in iu_names:
        cur.execute("ALTER TABLE item_units ADD COLUMN reserved_customer_id INTEGER NULL")

    # reserved_co_id på item_units
    cur.execute("PRAGMA table_info(item_units)")
    iu_cols = cur.fetchall()
    iu_names = [c[1] for c in iu_cols]
    if "reserved_co_id" not in iu_names:
        cur.execute("ALTER TABLE item_units ADD COLUMN reserved_co_id INTEGER NULL")

    # co_id på transactions
    cur.execute("PRAGMA table_info(transactions)")
    t_cols = cur.fetchall()
    t_names = [c[1] for c in t_cols]
    if "co_id" not in t_names:
        cur.execute("ALTER TABLE transactions ADD COLUMN co_id INTEGER NULL")

    conn.commit()
    conn.close()
