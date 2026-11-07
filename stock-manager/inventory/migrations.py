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
    (
        17,
        "what a basket costs, what an article usually costs, and the gate on a first purchase",
        """
        ALTER TABLE baskets ADD COLUMN IF NOT EXISTS supplier VARCHAR;
        ALTER TABLE baskets ADD COLUMN IF NOT EXISTS unit_price DOUBLE;
        ALTER TABLE baskets ADD COLUMN IF NOT EXISTS shop_ref VARCHAR;

        CREATE SEQUENCE IF NOT EXISTS price_seq START 1;
        CREATE TABLE IF NOT EXISTS article_prices (
            price_id   INTEGER PRIMARY KEY DEFAULT nextval('price_seq'),
            article    VARCHAR NOT NULL,
            supplier   VARCHAR,
            unit_price DOUBLE NOT NULL,
            source     VARCHAR NOT NULL,
            seen_on    DATE NOT NULL DEFAULT current_date,
            added_at   TIMESTAMP NOT NULL DEFAULT current_timestamp
        );

        CREATE SEQUENCE IF NOT EXISTS gate_seq START 1;
        CREATE TABLE IF NOT EXISTS gate_requests (
            request_id    INTEGER PRIMARY KEY DEFAULT nextval('gate_seq'),
            channel       VARCHAR,
            shop          VARCHAR,
            asked_by      VARCHAR,
            site          VARCHAR,
            work_item     VARCHAR,
            reasons       VARCHAR NOT NULL,
            basket        VARCHAR NOT NULL,
            total         DOUBLE,
            state         VARCHAR NOT NULL DEFAULT 'waiting',
            agent_verdict VARCHAR,
            agent_said    VARCHAR,
            agent_at      TIMESTAMP,
            decided_by    VARCHAR,
            decided_at    TIMESTAMP,
            asked_at      TIMESTAMP NOT NULL DEFAULT current_timestamp
        );

        CREATE SEQUENCE IF NOT EXISTS seal_seq START 1;
        CREATE TABLE IF NOT EXISTS gate_log (
            entry_id   INTEGER PRIMARY KEY DEFAULT nextval('seal_seq'),
            request_id INTEGER,
            event      VARCHAR NOT NULL,
            body       VARCHAR NOT NULL,
            who        VARCHAR,
            made_at    TIMESTAMP NOT NULL DEFAULT current_timestamp,
            prev_seal  VARCHAR NOT NULL,
            seal       VARCHAR NOT NULL
        );
        """,
    ),
    (
        18,
        "the lines ticked to order, waiting fifteen days before Plutus orders them",
        """
        CREATE SEQUENCE IF NOT EXISTS order_queue_seq START 1;
        CREATE TABLE IF NOT EXISTS order_queue (
            queue_id     INTEGER PRIMARY KEY DEFAULT nextval('order_queue_seq'),
            list_id      INTEGER NOT NULL,
            line_no      INTEGER NOT NULL,
            article      VARCHAR NOT NULL,
            designation  VARCHAR,
            reference    VARCHAR,
            quantity     DOUBLE,
            site         VARCHAR,
            work_item    VARCHAR,
            queued_on    DATE NOT NULL,
            due_on       DATE NOT NULL,
            state        VARCHAR NOT NULL DEFAULT 'waiting',
            ordered_on   DATE,
            ordered_list INTEGER,
            UNIQUE (list_id, line_no)
        );
        """,
    ),
    (
        19,
        "stock control: movements between sites, adjustments, reservations",
        """
        CREATE SEQUENCE IF NOT EXISTS stock_move_seq START 1;
        CREATE TABLE IF NOT EXISTS stock_moves (
            move_id    INTEGER PRIMARY KEY DEFAULT nextval('stock_move_seq'),
            article    VARCHAR NOT NULL,
            site       VARCHAR,
            quantity   DOUBLE NOT NULL,
            kind       VARCHAR NOT NULL,
            reason     VARCHAR,
            ref        VARCHAR,
            moved_on   DATE NOT NULL DEFAULT current_date,
            created_at TIMESTAMP NOT NULL DEFAULT current_timestamp,
            created_by VARCHAR
        );

        CREATE SEQUENCE IF NOT EXISTS reservation_seq START 1;
        CREATE TABLE IF NOT EXISTS reservations (
            reservation_id INTEGER PRIMARY KEY DEFAULT nextval('reservation_seq'),
            article        VARCHAR NOT NULL,
            site           VARCHAR,
            work_item      VARCHAR,
            quantity       DOUBLE NOT NULL,
            needed_by      DATE,
            note           VARCHAR,
            state          VARCHAR NOT NULL DEFAULT 'active',
            created_on     DATE NOT NULL DEFAULT current_date,
            created_at     TIMESTAMP NOT NULL DEFAULT current_timestamp,
            created_by     VARCHAR
        );
        """,
    ),
    (
        20,
        "mail to and from suppliers: drafts, what was sent, what came back",
        """
        CREATE SEQUENCE IF NOT EXISTS supplier_mail_seq START 1;
        CREATE TABLE IF NOT EXISTS supplier_mail (
            mail_id         INTEGER PRIMARY KEY DEFAULT nextval('supplier_mail_seq'),
            direction       VARCHAR NOT NULL,
            kind            VARCHAR NOT NULL,
            supplier        VARCHAR,
            to_addr         VARCHAR,
            from_addr       VARCHAR,
            subject         VARCHAR,
            body            VARCHAR,
            ref_kind        VARCHAR,
            ref_id          INTEGER,
            thread_id       INTEGER,
            state           VARCHAR NOT NULL,
            error           VARCHAR,
            message_id      VARCHAR,
            created_at      TIMESTAMP NOT NULL DEFAULT current_timestamp,
            created_by      VARCHAR,
            sent_at         TIMESTAMP,
            confirmed_at    TIMESTAMP,
            eta             DATE,
            chase_count     INTEGER NOT NULL DEFAULT 0,
            last_chased_at  TIMESTAMP
        );
        """,
    ),
    (
        21,
        "working calendar, supplier terms, exchange rates, pack sizes",
        """
        CREATE TABLE IF NOT EXISTS calendar_days (
            day   DATE PRIMARY KEY,
            kind  VARCHAR NOT NULL DEFAULT 'closed',
            label VARCHAR
        );
        CREATE TABLE IF NOT EXISTS supplier_terms (
            supplier       VARCHAR PRIMARY KEY,
            cutoff         VARCHAR,
            closed_days    VARCHAR,
            payment_days   INTEGER,
            currency       VARCHAR,
            note           VARCHAR
        );
        CREATE TABLE IF NOT EXISTS fx_rates (
            day      DATE NOT NULL,
            currency VARCHAR NOT NULL,
            per_eur  DOUBLE NOT NULL,
            PRIMARY KEY (day, currency)
        );
        CREATE TABLE IF NOT EXISTS article_units (
            article    VARCHAR NOT NULL,
            supplier   VARCHAR NOT NULL DEFAULT '',
            base_unit  VARCHAR NOT NULL DEFAULT 'piece',
            pack_qty   DOUBLE NOT NULL,
            pack_label VARCHAR,
            moq_packs  INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (article, supplier)
        );
        """,
    ),
    (
        22,
        "what suppliers answered to a quote, and the prices that were awarded",
        """
        CREATE SEQUENCE IF NOT EXISTS offer_seq START 1;
        CREATE TABLE IF NOT EXISTS quote_offers (
            offer_id    INTEGER PRIMARY KEY DEFAULT nextval('offer_seq'),
            quote_id    INTEGER NOT NULL,
            line_no     INTEGER NOT NULL,
            supplier    VARCHAR,
            unit_price  DOUBLE NOT NULL,
            lead_days   INTEGER,
            valid_until DATE,
            note        VARCHAR,
            received_on DATE NOT NULL DEFAULT current_date,
            created_by  VARCHAR,
            UNIQUE (quote_id, line_no)
        );
        CREATE SEQUENCE IF NOT EXISTS agreement_seq START 1;
        CREATE TABLE IF NOT EXISTS agreements (
            agreement_id INTEGER PRIMARY KEY DEFAULT nextval('agreement_seq'),
            article      VARCHAR NOT NULL,
            supplier     VARCHAR NOT NULL,
            unit_price   DOUBLE NOT NULL,
            lead_days    INTEGER,
            valid_from   DATE NOT NULL DEFAULT current_date,
            valid_to     DATE,
            quote_id     INTEGER,
            state        VARCHAR NOT NULL DEFAULT 'active',
            created_by   VARCHAR,
            created_at   TIMESTAMP NOT NULL DEFAULT current_timestamp
        );
        """,
    ),
    (
        23,
        "bill disputes and the credit notes that settle them",
        """
        CREATE SEQUENCE IF NOT EXISTS dispute_seq START 1;
        CREATE TABLE IF NOT EXISTS disputes (
            dispute_id  INTEGER PRIMARY KEY DEFAULT nextval('dispute_seq'),
            bill_id     INTEGER NOT NULL,
            supplier    VARCHAR,
            reason      VARCHAR,
            claimed     DOUBLE,
            state       VARCHAR NOT NULL DEFAULT 'open',
            opened_on   DATE NOT NULL DEFAULT current_date,
            credit_ref  VARCHAR,
            credited    DOUBLE,
            closed_on   DATE,
            mail_id     INTEGER,
            opened_by   VARCHAR
        );
        """,
    ),
    (
        24,
        "deliveries to sites, with proof of delivery",
        """
        CREATE SEQUENCE IF NOT EXISTS dispatch_seq START 1;
        CREATE TABLE IF NOT EXISTS dispatches (
            dispatch_id  INTEGER PRIMARY KEY DEFAULT nextval('dispatch_seq'),
            list_id      INTEGER,
            site         VARCHAR,
            work_item    VARCHAR,
            vehicle_id   INTEGER,
            planned_on   DATE,
            state        VARCHAR NOT NULL DEFAULT 'planned',
            loaded_at    TIMESTAMP,
            delivered_at TIMESTAMP,
            received_by  VARCHAR,
            note         VARCHAR,
            created_at   TIMESTAMP NOT NULL DEFAULT current_timestamp,
            created_by   VARCHAR
        );
        CREATE TABLE IF NOT EXISTS dispatch_lines (
            dispatch_id INTEGER NOT NULL,
            line_no     INTEGER NOT NULL,
            article     VARCHAR NOT NULL,
            designation VARCHAR,
            quantity    DOUBLE NOT NULL,
            delivered   DOUBLE,
            PRIMARY KEY (dispatch_id, line_no)
        );
        """,
    ),
    (
        25,
        "photos kept as packed arrays, things that need a person, what a draft was before it was edited",
        """
        CREATE TABLE IF NOT EXISTS photo_archive (
            sha256      VARCHAR PRIMARY KEY,
            width       INTEGER NOT NULL,
            height      INTEGER NOT NULL,
            data        BLOB NOT NULL,
            bytes_from  BIGINT,
            archived_on DATE NOT NULL DEFAULT current_date
        );
        CREATE SEQUENCE IF NOT EXISTS notice_seq START 1;
        CREATE TABLE IF NOT EXISTS notices (
            notice_id   INTEGER PRIMARY KEY DEFAULT nextval('notice_seq'),
            key         VARCHAR NOT NULL UNIQUE,
            kind        VARCHAR NOT NULL,
            severity    VARCHAR NOT NULL DEFAULT 'normal',
            title       VARCHAR NOT NULL,
            detail      VARCHAR,
            first_seen  TIMESTAMP NOT NULL DEFAULT current_timestamp,
            last_seen   TIMESTAMP NOT NULL DEFAULT current_timestamp,
            resolved_at TIMESTAMP,
            pushed_at   TIMESTAMP
        );
        ALTER TABLE supplier_mail ADD COLUMN IF NOT EXISTS orig_body VARCHAR;
        """,
    ),
    (
        26,
        "what the app learns from corrections and refusals, and who an order is billed to",
        """
        CREATE TABLE IF NOT EXISTS tuning (
            article       VARCHAR PRIMARY KEY,
            qty_factor    DOUBLE NOT NULL DEFAULT 1.0,
            safety_factor DOUBLE NOT NULL DEFAULT 1.0,
            samples       INTEGER NOT NULL DEFAULT 0,
            last_reason   VARCHAR,
            updated_at    TIMESTAMP NOT NULL DEFAULT current_timestamp
        );
        CREATE SEQUENCE IF NOT EXISTS feedback_seq START 1;
        CREATE TABLE IF NOT EXISTS feedback (
            feedback_id INTEGER PRIMARY KEY DEFAULT nextval('feedback_seq'),
            article     VARCHAR NOT NULL,
            kind        VARCHAR NOT NULL,
            proposed    DOUBLE,
            final       DOUBLE,
            detail      VARCHAR,
            happened_at TIMESTAMP NOT NULL DEFAULT current_timestamp,
            done_by     VARCHAR
        );
        CREATE TABLE IF NOT EXISTS bill_to_default (
            id      INTEGER PRIMARY KEY,
            company VARCHAR,
            contact VARCHAR,
            address VARCHAR
        );
        ALTER TABLE purchase_orders ADD COLUMN IF NOT EXISTS bill_company VARCHAR;
        ALTER TABLE purchase_orders ADD COLUMN IF NOT EXISTS bill_contact VARCHAR;
        ALTER TABLE purchase_orders ADD COLUMN IF NOT EXISTS bill_address VARCHAR;
        """,
    ),
    (
        27,
        "policies that let routine work go through alone, what they did, and the bots' limits",
        """
        CREATE SEQUENCE IF NOT EXISTS policy_seq START 1;
        CREATE TABLE IF NOT EXISTS policies (
            policy_id        INTEGER PRIMARY KEY DEFAULT nextval('policy_seq'),
            name             VARCHAR NOT NULL,
            action           VARCHAR NOT NULL,
            active           BOOLEAN NOT NULL DEFAULT FALSE,
            mode             VARCHAR NOT NULL DEFAULT 'dry_run',
            supplier         VARCHAR NOT NULL DEFAULT '*',
            max_total        DOUBLE NOT NULL,
            per_day          DOUBLE,
            per_month        DOUBLE,
            price_band       DOUBLE NOT NULL DEFAULT 0.10,
            min_deliveries   INTEGER NOT NULL DEFAULT 3,
            allowed_reasons  VARCHAR NOT NULL DEFAULT '',
            terminus_same    BOOLEAN NOT NULL DEFAULT TRUE,
            hours_from       INTEGER NOT NULL DEFAULT 8,
            hours_to         INTEGER NOT NULL DEFAULT 17,
            per_supplier_day INTEGER NOT NULL DEFAULT 2,
            max_chases       INTEGER NOT NULL DEFAULT 2,
            created_by       VARCHAR,
            created_at       TIMESTAMP NOT NULL DEFAULT current_timestamp
        );
        CREATE TABLE IF NOT EXISTS policy_settings (
            key   VARCHAR PRIMARY KEY,
            value VARCHAR
        );
        CREATE SEQUENCE IF NOT EXISTS policy_log_seq START 1;
        CREATE TABLE IF NOT EXISTS policy_log (
            log_id      INTEGER PRIMARY KEY DEFAULT nextval('policy_log_seq'),
            happened_at TIMESTAMP NOT NULL DEFAULT current_timestamp,
            policy_id   INTEGER,
            policy_name VARCHAR,
            action      VARCHAR NOT NULL,
            subject     VARCHAR NOT NULL,
            supplier    VARCHAR,
            amount      DOUBLE,
            decision    VARCHAR NOT NULL,
            reasons     VARCHAR,
            mode        VARCHAR,
            acted       BOOLEAN NOT NULL DEFAULT FALSE
        );
        CREATE TABLE IF NOT EXISTS bot_limits (
            bot   VARCHAR NOT NULL,
            key   VARCHAR NOT NULL,
            value VARCHAR NOT NULL,
            PRIMARY KEY (bot, key)
        );
        """,
    ),
    (
        28,
        "a wallet Plutus spends from, what is committed to orders, and the payments it instructs",
        """
        CREATE SEQUENCE IF NOT EXISTS wallet_seq START 1;
        CREATE TABLE IF NOT EXISTS wallet_entries (
            entry_id        INTEGER PRIMARY KEY DEFAULT nextval('wallet_seq'),
            happened_at     TIMESTAMP NOT NULL DEFAULT current_timestamp,
            kind            VARCHAR NOT NULL,
            amount          DOUBLE NOT NULL,
            balance_after   DOUBLE NOT NULL,
            committed_after DOUBLE NOT NULL,
            ref_kind        VARCHAR,
            ref_id          INTEGER,
            supplier        VARCHAR,
            note            VARCHAR,
            done_by         VARCHAR
        );
        CREATE SEQUENCE IF NOT EXISTS commitment_seq START 1;
        CREATE TABLE IF NOT EXISTS commitments (
            commit_id   INTEGER PRIMARY KEY DEFAULT nextval('commitment_seq'),
            supplier    VARCHAR,
            amount      DOUBLE NOT NULL,
            ref_kind    VARCHAR,
            ref_id      INTEGER,
            made_on     DATE NOT NULL DEFAULT current_date,
            state       VARCHAR NOT NULL DEFAULT 'open',
            released_on DATE
        );
        CREATE TABLE IF NOT EXISTS supplier_bank (
            supplier VARCHAR PRIMARY KEY,
            iban     VARCHAR NOT NULL,
            bic      VARCHAR,
            holder   VARCHAR
        );
        CREATE SEQUENCE IF NOT EXISTS batch_seq START 1;
        CREATE TABLE IF NOT EXISTS payment_batches (
            batch_id    INTEGER PRIMARY KEY DEFAULT nextval('batch_seq'),
            created_at  TIMESTAMP NOT NULL DEFAULT current_timestamp,
            state       VARCHAR NOT NULL DEFAULT 'open',
            exported_at TIMESTAMP,
            executed_at TIMESTAMP
        );
        CREATE SEQUENCE IF NOT EXISTS payment_seq START 1;
        CREATE TABLE IF NOT EXISTS payments (
            payment_id    INTEGER PRIMARY KEY DEFAULT nextval('payment_seq'),
            bill_id       INTEGER NOT NULL,
            supplier      VARCHAR,
            amount        DOUBLE NOT NULL,
            currency      VARCHAR NOT NULL DEFAULT 'EUR',
            iban          VARCHAR NOT NULL,
            batch_id      INTEGER,
            state         VARCHAR NOT NULL DEFAULT 'instructed',
            reference     VARCHAR,
            instructed_at TIMESTAMP NOT NULL DEFAULT current_timestamp,
            executed_at   TIMESTAMP,
            done_by       VARCHAR
        );
        """,
    ),
    (
        29,
        "supplier replies read, order webhooks, the bank drop, and site requisitions",
        """
        ALTER TABLE supplier_mail ADD COLUMN IF NOT EXISTS read_json VARCHAR;
        CREATE TABLE IF NOT EXISTS supplier_channels (
            supplier    VARCHAR PRIMARY KEY,
            kind        VARCHAR NOT NULL DEFAULT 'webhook',
            url         VARCHAR,
            token       VARCHAR,
            updated_at  TIMESTAMP NOT NULL DEFAULT current_timestamp
        );
        CREATE SEQUENCE IF NOT EXISTS order_delivery_seq START 1;
        CREATE TABLE IF NOT EXISTS order_deliveries (
            delivery_id  INTEGER PRIMARY KEY DEFAULT nextval('order_delivery_seq'),
            mail_id      INTEGER,
            supplier     VARCHAR,
            url          VARCHAR,
            status       INTEGER,
            ok           BOOLEAN,
            answer       VARCHAR,
            sent_at      TIMESTAMP NOT NULL DEFAULT current_timestamp
        );
        ALTER TABLE payment_batches ADD COLUMN IF NOT EXISTS msg_id VARCHAR;
        ALTER TABLE payment_batches ADD COLUMN IF NOT EXISTS uploaded_at TIMESTAMP;
        ALTER TABLE payment_batches ADD COLUMN IF NOT EXISTS bank_status VARCHAR;
        CREATE SEQUENCE IF NOT EXISTS requisition_seq START 1;
        CREATE TABLE IF NOT EXISTS requisitions (
            req_id          INTEGER PRIMARY KEY DEFAULT nextval('requisition_seq'),
            requester       VARCHAR,
            site            VARCHAR,
            work_item       VARCHAR,
            needed_by       DATE,
            state           VARCHAR NOT NULL DEFAULT 'open',
            note            VARCHAR,
            gate_request_id INTEGER,
            created_at      TIMESTAMP NOT NULL DEFAULT current_timestamp,
            closed_at       TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS requisition_lines (
            req_id          INTEGER NOT NULL,
            line_no         INTEGER NOT NULL,
            article         VARCHAR NOT NULL,
            quantity        DOUBLE NOT NULL,
            reserved        DOUBLE NOT NULL DEFAULT 0,
            reservation_id  INTEGER,
            to_order        DOUBLE NOT NULL DEFAULT 0,
            PRIMARY KEY (req_id, line_no)
        );
        """,
    ),
    (
        30,
        "carriers and their shipments, put-away locations, and the two-count rule",
        """
        CREATE TABLE IF NOT EXISTS carriers (
            name        VARCHAR PRIMARY KEY,
            book_url    VARCHAR,
            track_url   VARCHAR,
            token       VARCHAR,
            is_default  BOOLEAN NOT NULL DEFAULT false,
            updated_at  TIMESTAMP NOT NULL DEFAULT current_timestamp
        );
        CREATE SEQUENCE IF NOT EXISTS shipment_seq START 1;
        CREATE TABLE IF NOT EXISTS shipments (
            shipment_id  INTEGER PRIMARY KEY DEFAULT nextval('shipment_seq'),
            dispatch_id  INTEGER NOT NULL,
            carrier      VARCHAR NOT NULL,
            tracking     VARCHAR,
            label_url    VARCHAR,
            state        VARCHAR NOT NULL DEFAULT 'booked',
            booked_at    TIMESTAMP NOT NULL DEFAULT current_timestamp,
            last_checked TIMESTAMP,
            eta          DATE,
            signed_by    VARCHAR,
            detail       VARCHAR,
            error        VARCHAR
        );
        CREATE SEQUENCE IF NOT EXISTS putaway_seq START 1;
        CREATE TABLE IF NOT EXISTS putaways (
            putaway_id    INTEGER PRIMARY KEY DEFAULT nextval('putaway_seq'),
            note_id       INTEGER NOT NULL,
            line_no       INTEGER NOT NULL,
            article       VARCHAR NOT NULL,
            site          VARCHAR,
            supplier      VARCHAR,
            quantity      DOUBLE,
            suggested     VARCHAR,
            location      VARCHAR,
            state         VARCHAR NOT NULL DEFAULT 'to put away',
            check_word    VARCHAR,
            check_detail  VARCHAR,
            created_at    TIMESTAMP NOT NULL DEFAULT current_timestamp,
            done_at       TIMESTAMP,
            done_by       VARCHAR
        );
        CREATE TABLE IF NOT EXISTS article_homes (
            article    VARCHAR NOT NULL,
            site       VARCHAR NOT NULL DEFAULT '',
            location   VARCHAR NOT NULL,
            last_used  TIMESTAMP NOT NULL DEFAULT current_timestamp,
            PRIMARY KEY (article, site)
        );
        CREATE SEQUENCE IF NOT EXISTS count_check_seq START 1;
        CREATE TABLE IF NOT EXISTS count_checks (
            check_id      INTEGER PRIMARY KEY DEFAULT nextval('count_check_seq'),
            article       VARCHAR NOT NULL,
            expected      DOUBLE NOT NULL,
            first_count   DOUBLE NOT NULL,
            counted_by    VARCHAR,
            asked_at      TIMESTAMP NOT NULL DEFAULT current_timestamp,
            state         VARCHAR NOT NULL DEFAULT 'recount',
            second_count  DOUBLE,
            explanation   VARCHAR,
            closed_at     TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS count_tasks (
            article  VARCHAR NOT NULL,
            due_on   DATE NOT NULL,
            state    VARCHAR NOT NULL DEFAULT 'open',
            PRIMARY KEY (article, due_on)
        );
        """,
    ),
    (
        31,
        "carriers and shipments are dropped: the dispatcher and the carrier booking were taken out again",
        """
        DROP TABLE IF EXISTS shipments;
        DROP TABLE IF EXISTS carriers;
        DROP SEQUENCE IF EXISTS shipment_seq;
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
