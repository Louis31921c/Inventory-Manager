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
            con.execute(sql)
            con.execute("INSERT INTO schema_migrations (version, description) VALUES (?, ?)",
                        [number, description])
            con.commit()
        except Exception:
            con.rollback()
            raise
        applied.append((number, description))
    return applied
