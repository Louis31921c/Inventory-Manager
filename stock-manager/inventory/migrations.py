def rename_tables(con):
    """Step 7, written in Python because the rename has to look before it leaps."""
    def tables():
        return {row[0] for row in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()}

    def rows(name):
        return con.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]

    for old, new in (("deliveries", "inventory_lines"),
                     ("hardware_lists", "purchase_orders"),
                     ("hardware_list_lines", "purchase_order_lines")):
        here = tables()
        if old not in here:
            continue
        if new in here:
            if rows(new) == 0:
                con.execute(f"DROP TABLE {new}")
            elif rows(old) == 0:
                con.execute(f"DROP TABLE {old}")
                continue
            else:
                raise RuntimeError(f"both {old} and {new} hold rows: merge them by hand")
        con.execute(f"ALTER TABLE {old} RENAME TO {new}")


MIGRATIONS = [
    (
        1,
        "core: notes, deliveries, catalog",
        """
        CREATE SEQUENCE IF NOT EXISTS note_seq START 1;

        CREATE TABLE IF NOT EXISTS notes (
            note_id       INTEGER PRIMARY KEY DEFAULT nextval('note_seq'),
            image_sha256  VARCHAR UNIQUE,
            image_path    VARCHAR,
            confirmed_at  TIMESTAMP NOT NULL DEFAULT current_timestamp,
            confirmed_by  VARCHAR
        );

        CREATE TABLE IF NOT EXISTS deliveries (
            article        VARCHAR NOT NULL,
            quantity       DOUBLE,
            supplier       VARCHAR NOT NULL,
            delivery_date  DATE    NOT NULL,
            weekday        VARCHAR GENERATED ALWAYS AS (dayname(delivery_date)) VIRTUAL,
            order_date     DATE,
            site           VARCHAR,
            work_item      VARCHAR,
            backorder      BOOLEAN NOT NULL DEFAULT false,
            designation    VARCHAR,
            note_id        INTEGER NOT NULL,
            line_no        INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS catalog (
            supplier      VARCHAR NOT NULL,
            designation   VARCHAR NOT NULL,
            reference     VARCHAR,
            article       VARCHAR NOT NULL,
            seen          INTEGER NOT NULL DEFAULT 1,
            updated_at    TIMESTAMP NOT NULL DEFAULT current_timestamp,
            PRIMARY KEY (supplier, designation)
        );
        """,
    ),
    (
        2,
        "hardware lists",
        """
        CREATE SEQUENCE IF NOT EXISTS list_seq START 1;

        CREATE TABLE IF NOT EXISTS hardware_lists (
            list_id       INTEGER PRIMARY KEY DEFAULT nextval('list_seq'),
            list_date     DATE,
            site          VARCHAR,
            work_item     VARCHAR,
            drafter       VARCHAR,
            image_sha256  VARCHAR UNIQUE,
            image_path    VARCHAR,
            created_at    TIMESTAMP NOT NULL DEFAULT current_timestamp,
            created_by    VARCHAR
        );

        CREATE TABLE IF NOT EXISTS hardware_list_lines (
            list_id     INTEGER NOT NULL,
            line_no     INTEGER NOT NULL,
            article     VARCHAR NOT NULL,
            designation VARCHAR,
            reference   VARCHAR,
            quantity    DOUBLE,
            stock       DOUBLE,
            in_stock    BOOLEAN,
            PRIMARY KEY (list_id, line_no)
        );
        """,
    ),
    (
        3,
        "reading measurements",
        """
        CREATE SEQUENCE IF NOT EXISTS reading_seq START 1;
        CREATE TABLE IF NOT EXISTS readings (
            reading_id   INTEGER PRIMARY KEY DEFAULT nextval('reading_seq'),
            measured_at  TIMESTAMP NOT NULL DEFAULT current_timestamp,
            document     VARCHAR NOT NULL,   -- note | list
            fields       INTEGER NOT NULL,   -- fields compared at review time
            corrected    INTEGER NOT NULL,   -- fields the human changed
            lines        INTEGER NOT NULL DEFAULT 0
        );
        """,
    ),
    (
        4,
        "article vocabulary",
        """
        CREATE TABLE IF NOT EXISTS article_names (
            article     VARCHAR PRIMARY KEY,
            first_seen  TIMESTAMP NOT NULL DEFAULT current_timestamp,
            last_seen   TIMESTAMP NOT NULL DEFAULT current_timestamp,
            seen        INTEGER NOT NULL DEFAULT 0,
            source      VARCHAR NOT NULL DEFAULT 'delivery'
        );

        CREATE TABLE IF NOT EXISTS article_aliases (
            alias       VARCHAR PRIMARY KEY,
            article     VARCHAR NOT NULL,
            created_at  TIMESTAMP NOT NULL DEFAULT current_timestamp,
            created_by  VARCHAR
        );
        """,
    ),
    (
        5,
        "users and audit trail",
        """
        CREATE TABLE IF NOT EXISTS users (
            user_name     VARCHAR PRIMARY KEY,
            created_at    TIMESTAMP NOT NULL DEFAULT current_timestamp,
            last_seen_at  TIMESTAMP NOT NULL DEFAULT current_timestamp
        );

        CREATE SEQUENCE IF NOT EXISTS audit_seq START 1;
        CREATE TABLE IF NOT EXISTS audit (
            event_id     INTEGER PRIMARY KEY DEFAULT nextval('audit_seq'),
            happened_at  TIMESTAMP NOT NULL DEFAULT current_timestamp,
            user_name    VARCHAR NOT NULL DEFAULT '?',
            action       VARCHAR NOT NULL,   -- note.save, list.tick, login...
            target       VARCHAR,
            detail       VARCHAR,
            address      VARCHAR
        );
        """,
    ),
    (
        6,
        "reported errors",
        """
        CREATE SEQUENCE IF NOT EXISTS error_seq START 1;
        CREATE TABLE IF NOT EXISTS errors (
            error_no     INTEGER PRIMARY KEY DEFAULT nextval('error_seq'),
            happened_at  TIMESTAMP NOT NULL DEFAULT current_timestamp,
            kind         VARCHAR NOT NULL,   -- request, backup, monitor, test...
            description  VARCHAR NOT NULL,
            context      VARCHAR,
            user_name    VARCHAR,
            sms_status   VARCHAR NOT NULL DEFAULT 'pending'
        );
        """,
    ),
    (7, "rename the tables: deliveries -> inventory_lines, lists -> purchase orders", rename_tables),
    (
        8,
        "stock counts, so the planner knows what is on the shelf",
        """
        CREATE TABLE IF NOT EXISTS stock_counts (
            article    VARCHAR PRIMARY KEY,
            quantity   DOUBLE NOT NULL,
            counted_on DATE NOT NULL DEFAULT current_date,
            counted_by VARCHAR,
            noted_at   TIMESTAMP NOT NULL DEFAULT current_timestamp
        );
        """,
    ),
    (
        9,
        "the intake queue: files and mail waiting to be read",
        """
        CREATE SEQUENCE IF NOT EXISTS intake_seq START 1;
        CREATE TABLE IF NOT EXISTS intake (
            intake_id   INTEGER PRIMARY KEY DEFAULT nextval('intake_seq'),
            source      VARCHAR NOT NULL,
            name        VARCHAR NOT NULL,
            media_type  VARCHAR,
            sha256      VARCHAR,
            path        VARCHAR NOT NULL,
            bytes       BIGINT,
            sender      VARCHAR,
            subject     VARCHAR,
            arrived_at  TIMESTAMP NOT NULL DEFAULT current_timestamp,
            state       VARCHAR NOT NULL DEFAULT 'waiting',
            note        VARCHAR
        );
        """,
    ),
    (
        10,
        "supplier bills, so what is owed is tracked next to what arrived",
        """
        CREATE SEQUENCE IF NOT EXISTS bill_seq START 1;
        CREATE TABLE IF NOT EXISTS bills (
            bill_id      INTEGER PRIMARY KEY DEFAULT nextval('bill_seq'),
            supplier     VARCHAR NOT NULL,
            number       VARCHAR,
            bill_date    DATE NOT NULL,
            month        VARCHAR GENERATED ALWAYS AS (strftime(bill_date, '%Y-%m')) VIRTUAL,
            due_date     DATE,
            currency     VARCHAR NOT NULL DEFAULT 'EUR',
            net          DOUBLE,
            tax          DOUBLE,
            total        DOUBLE,
            site         VARCHAR,
            order_ref    VARCHAR,
            state        VARCHAR NOT NULL DEFAULT 'to pay',
            paid_on      DATE,
            source       VARCHAR NOT NULL DEFAULT 'file',
            sender       VARCHAR,
            image_sha256 VARCHAR UNIQUE,
            image_path   VARCHAR,
            added_at     TIMESTAMP NOT NULL DEFAULT current_timestamp,
            added_by     VARCHAR,
            note         VARCHAR
        );
        """,
    ),
    (
        11,
        "chat baskets: what a group chat asks for before it becomes a purchase order",
        """
        CREATE SEQUENCE IF NOT EXISTS basket_seq START 1;
        CREATE TABLE IF NOT EXISTS baskets (
            line_id    INTEGER PRIMARY KEY DEFAULT nextval('basket_seq'),
            channel    VARCHAR NOT NULL,
            shop       VARCHAR,
            article    VARCHAR NOT NULL,
            quantity   DOUBLE,
            site       VARCHAR,
            work_item  VARCHAR,
            note       VARCHAR,
            added_by   VARCHAR,
            added_at   TIMESTAMP NOT NULL DEFAULT current_timestamp
        );
        """,
    ),
    (
        12,
        "quote requests: what to ask a supplier for, by the metre, the kilo or the cubic metre",
        """
        CREATE SEQUENCE IF NOT EXISTS quote_seq START 1;
        CREATE TABLE IF NOT EXISTS quotes (
            quote_id   INTEGER PRIMARY KEY DEFAULT nextval('quote_seq'),
            supplier   VARCHAR,
            site       VARCHAR,
            work_item  VARCHAR,
            needed_by  DATE,
            state      VARCHAR NOT NULL DEFAULT 'open',
            note       VARCHAR,
            asked_at   TIMESTAMP NOT NULL DEFAULT current_timestamp,
            asked_by   VARCHAR
        );

        CREATE TABLE IF NOT EXISTS quote_lines (
            quote_id    INTEGER NOT NULL,
            line_no     INTEGER NOT NULL,
            article     VARCHAR NOT NULL,
            quantity    DOUBLE,
            unit        VARCHAR,
            designation VARCHAR,
            PRIMARY KEY (quote_id, line_no)
        );
        """,
    ),
    (
        13,
        "places: the yard the truck leaves from and the addresses it delivers to",
        """
        CREATE SEQUENCE IF NOT EXISTS place_seq START 1;
        CREATE TABLE IF NOT EXISTS places (
            place_id  INTEGER PRIMARY KEY DEFAULT nextval('place_seq'),
            kind      VARCHAR NOT NULL DEFAULT 'stop',
            label     VARCHAR,
            address   VARCHAR NOT NULL,
            lat       DOUBLE,
            lon       DOUBLE,
            note      VARCHAR,
            done      BOOLEAN NOT NULL DEFAULT FALSE,
            added_at  TIMESTAMP NOT NULL DEFAULT current_timestamp,
            added_by  VARCHAR
        );
        """,
    ),
    (
        14,
        "contacts: who an outbound order goes to, and on what terms",
        """
        CREATE SEQUENCE IF NOT EXISTS contact_seq START 1;
        CREATE TABLE IF NOT EXISTS contacts (
            contact_id INTEGER PRIMARY KEY DEFAULT nextval('contact_seq'),
            company    VARCHAR NOT NULL,
            person     VARCHAR,
            phone      VARCHAR,
            email      VARCHAR,
            address    VARCHAR,
            incoterm   VARCHAR,
            site       VARCHAR,
            note       VARCHAR,
            added_at   TIMESTAMP NOT NULL DEFAULT current_timestamp,
            added_by   VARCHAR
        );

        ALTER TABLE purchase_orders ADD COLUMN IF NOT EXISTS contact_id INTEGER;
        ALTER TABLE purchase_orders ADD COLUMN IF NOT EXISTS incoterm VARCHAR;
        ALTER TABLE purchase_orders ADD COLUMN IF NOT EXISTS ship_by DATE;
        """,
    ),
    (
        15,
        "what a delivery note still owes: the day the rest is expected",
        """
        ALTER TABLE inventory_lines ADD COLUMN IF NOT EXISTS expected_on DATE;
        ALTER TABLE inventory_lines ADD COLUMN IF NOT EXISTS settled_on DATE;
        """,
    ),
    (
        16,
        "the vans, what they can carry and when a stop can be served",
        """
        CREATE SEQUENCE IF NOT EXISTS vehicle_seq START 1;
        CREATE TABLE IF NOT EXISTS vehicles (
            vehicle_id  INTEGER PRIMARY KEY DEFAULT nextval('vehicle_seq'),
            name        VARCHAR NOT NULL,
            shape       VARCHAR NOT NULL DEFAULT 'van',
            plate       VARCHAR,
            cubic       DOUBLE,
            kilos       DOUBLE,
            note        VARCHAR
        );

        INSERT INTO vehicles (name, shape, cubic, kilos)
        SELECT * FROM (VALUES ('Renault Partner 1', 'van', 3.3, 650.0),
                              ('Renault Partner 2', 'van', 3.3, 650.0),
                              ('Iveco Daily', 'truck', 19.6, 1800.0)) AS v(name, shape, cubic, kilos)
        WHERE NOT EXISTS (SELECT 1 FROM vehicles);

        ALTER TABLE places ADD COLUMN IF NOT EXISTS vehicle_id INTEGER;
        ALTER TABLE places ADD COLUMN IF NOT EXISTS cubic DOUBLE;
        ALTER TABLE places ADD COLUMN IF NOT EXISTS kilos DOUBLE;
        ALTER TABLE places ADD COLUMN IF NOT EXISTS open_from VARCHAR;
        ALTER TABLE places ADD COLUMN IF NOT EXISTS open_to VARCHAR;
        """,
    ),
]

VERSION_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    description VARCHAR,
    applied_at  TIMESTAMP NOT NULL DEFAULT current_timestamp
);
"""


def current_version(con):
    row = con.execute("SELECT coalesce(max(version), 0) FROM schema_migrations").fetchone()
    return row[0] if row else 0


def apply(con):
    
    con.execute(VERSION_TABLE)
    version = current_version(con)
    applied = []
    for number, description, sql in MIGRATIONS:
        if number <= version:
            continue
        con.begin()
        try:
            if callable(sql):
                sql(con)
            else:
                con.execute(sql)
            con.execute("INSERT INTO schema_migrations (version, description) VALUES (?, ?)",
                        [number, description])
            con.commit()
        except Exception:
            con.rollback()
            raise
        applied.append((number, description))
    return applied
