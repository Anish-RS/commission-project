# ============================================================
#  Commission Application — PostgreSQL (Neon) Edition
#  Fully fixed + enhanced for Vercel deployment
# ============================================================

from flask import Flask, jsonify, render_template, request, redirect, url_for, flash,session
from db import get_connection
from psycopg2.extras import RealDictCursor
from datetime import date, timedelta, datetime, time, timezone
from decimal import Decimal, ROUND_HALF_UP
import os
import re
import traceback

from whatsapp_service import (
    send_purchase_statement,
    update_delivery_status,
    get_customers_for_date,
    get_purchase_summary,
    has_statement_been_sent,
    get_in_progress_customer_ids,
    get_whatsapp_blocked_customer_ids,
    unblock_customer_whatsapp
)


app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get(
    "SECRET_KEY",
    "6f2c8d9e1a4b7c0d3f8a5e9b2c1d7f4g8h6j3k9m1n5p2q7r4t8u0v6w9x3y1z"
)

@app.before_request
def require_login():
    # Pages that do NOT require a password (login page, static files, and the WhatsApp webhook)
    allowed_routes = ['login', 'static', 'verify_whatsapp_webhook', 'receive_whatsapp_webhook','view_statement']
    
    if request.endpoint not in allowed_routes and not session.get('logged_in'):
        return redirect(url_for('login'))
# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
    
def to_decimal(x):
    return Decimal(x or 0).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

@app.context_processor
def inject_now():
    return {"now": datetime.now}

# ---------------------------------------------------------------------------
# Timezone helper (zoneinfo with IST fallback)
# ---------------------------------------------------------------------------
try:
    from zoneinfo import ZoneInfo
    def get_zone(tzname):
        return ZoneInfo(tzname)
except Exception:
    class _FixedOffset(timezone):
        """Minimal fixed-offset tzinfo for IST (+05:30)."""
        def __repr__(self): return "FixedOffset(+05:30)"

    def get_zone(tzname):
        if tzname == "Asia/Kolkata":
            return timezone(timedelta(minutes=330))
        raise RuntimeError(f"zoneinfo unavailable and tzname '{tzname}' != Asia/Kolkata")

IST = get_zone("Asia/Kolkata")

def ist_today():
    return datetime.now(IST).date()

def ist_now():
    return datetime.now(IST)

UUID_RE = re.compile(
    r'([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$'
)

@app.route("/s/<path:token>")
def view_statement(token):

    match = UUID_RE.search(token)
    if not match:
        return "Invalid or Expired Link", 404
    token = match.group(1)

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    cursor.execute("""
        SELECT
            customer_id,
            statement_date
        FROM statement_tokens
        WHERE token = %s
          AND expires_at > NOW()
    """, (token,))

    row = cursor.fetchone()

    if not row:
        cursor.close()
        conn.close()
        return "Invalid or Expired Link", 404

    customer_id = row["customer_id"]
    period = row["statement_date"]
    cursor.execute("""
        SELECT name, balance FROM customers WHERE customer_id=%s
    """, (customer_id,))

    cust = cursor.fetchone()

    if not cust:
        cursor.close()
        conn.close()
        return "Customer not found",404

    customer_name = cust["name"]
    current_balance = float(cust["balance"])
    cursor.execute("""
        SELECT bill_id, customer_id, bill_date, quantity, rate, amount,
               supplier_bill_id, old_balance, final_balance
        FROM customer_bills
        WHERE customer_id=%s AND bill_date=%s
        ORDER BY bill_date, bill_id
    """, (customer_id, period))

    purchases = cursor.fetchall() or []

    opening = (
        float(purchases[0]["old_balance"])
        if purchases
        else current_balance
    )

    net = sum(
        float(x["amount"])
        for x in purchases
    )
    cursor.execute("""
        SELECT COALESCE(SUM(amount),0) AS total_receipts
        FROM transactions
        WHERE entity_type='customer' AND entity_id=%s
          AND tx_type='receipt' AND DATE(tx_date)=%s
    """, (customer_id, period))

    row = cursor.fetchone()

    sum_receipts = float(row["total_receipts"])
    cursor.close()
    conn.close()

    return render_template(
        "customer_statement_view.html",
        customer_id=customer_id,
        customer_name=customer_name,
        from_date=period,
        to_date=period,
        purchases=purchases,
        opening=opening,
        net=net,
        current_balance=current_balance,
        sum_receipts=sum_receipts
    )
# ---------------------------------------------------------------------------
# Cash-in-hand recomputation (PostgreSQL)
# ---------------------------------------------------------------------------

def recompute_cash_in_hand_for_date(target_date):
    """
    Recompute opening/receipts/payments/closing for target_date
    and upsert the result into cash_in_hand (Postgres ON CONFLICT).
    Returns a dict with Decimal values.
    """
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    start = datetime.combine(target_date, time.min)
    end   = start + timedelta(days=1)

    cursor.execute(
        "SELECT COALESCE(t.amount,0) AS amount FROM transactions t "
        "WHERE t.tx_type='receipt' AND t.tx_date >= %s AND t.tx_date < %s",
        (start, end)
    )
    total_receipts = sum(to_decimal(r["amount"]) for r in cursor.fetchall())

    cursor.execute(
        "SELECT COALESCE(t.amount,0) AS amount FROM transactions t "
        "WHERE t.tx_type='payment' AND t.tx_date >= %s AND t.tx_date < %s",
        (start, end)
    )
    total_payments = sum(to_decimal(p["amount"]) for p in cursor.fetchall())

    cursor.execute(
        "SELECT COALESCE(SUM(b.transport),0) AS transport_sum, "
        "COALESCE(SUM(b.paid),0) AS paid_sum "
        "FROM supplier_bills b WHERE b.bill_date = %s",
        (target_date,)
    )
    srow = cursor.fetchone() or {}
    total_payments += to_decimal(srow.get("transport_sum") or 0)
    total_payments += to_decimal(srow.get("paid_sum") or 0)

    cursor.execute(
        "SELECT COALESCE(SUM(amount),0) AS labour_sum "
        "FROM labour_entries WHERE entry_date = %s",
        (target_date,)
    )
    lr = cursor.fetchone() or {}
    total_payments += to_decimal(lr.get("labour_sum") or 0)

    yesterday = target_date - timedelta(days=1)
    cursor.execute(
    """
    SELECT closing
    FROM cash_in_hand
    WHERE cdate < %s
    ORDER BY cdate DESC
    LIMIT 1
    """,
    (target_date,))
    r = cursor.fetchone()
    opening = to_decimal(r["closing"]) if r and r.get("closing") is not None else to_decimal(0)
    closing = opening + total_receipts - total_payments
    
    print("Total Receipts:", total_receipts)
    print("Total Payments:", total_payments)
    print("Opening:", opening)
    print("Closing:", closing)

    cursor.execute(
        """
        INSERT INTO cash_in_hand (cdate, opening, total_receipts, total_payments, closing)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (cdate) DO UPDATE SET
            opening        = EXCLUDED.opening,
            total_receipts = EXCLUDED.total_receipts,
            total_payments = EXCLUDED.total_payments,
            closing        = EXCLUDED.closing
        """,
        (target_date, opening, total_receipts, total_payments, closing)
    )
    conn.commit()
    cursor.close()
    conn.close()
    print("RECOMPUTE CALLED FOR:", target_date)
    return {"opening": opening, "receipts": total_receipts,
            "payments": total_payments, "closing": closing}

def recompute_cash_in_hand_from_date(start_date):
    current = start_date

    while current <= ist_today():
        recompute_cash_in_hand_for_date(current)
        current += timedelta(days=1)


# ---------------------------------------------------------------------------
# Ledger resync
#
# customer_bills.old_balance / final_balance used to be a one-time snapshot
# taken from customers.balance at INSERT time. That's only correct if every
# bill, adjustment, and receipt/payment for a customer is always entered in
# true date order. The moment something is backdated (an adjustment entered
# after later bills already exist, an old bill edited after newer activity,
# etc.) those stored snapshots go stale and the printed "Opening Balance"
# stops matching reality — even though customers.balance itself stays
# correct throughout.
#
# This function rebuilds old_balance/final_balance for a customer's
# customer_bills rows from scratch, by replaying EVERY dated event for that
# customer (bills + account_adjustments + transactions) in true
# chronological order, anchored on customers.opening_balance. Call it after
# ANY operation that touches a customer's balance (bill create/edit/delete,
# receipt, payment, manual adjustment) and the printed statements will
# always be self-consistent — no more manual SQL repairs needed.
# ---------------------------------------------------------------------------

def resync_customer_ledger(cursor, customer_ids):
    ids = list({int(cid) for cid in customer_ids if cid is not None})
    if not ids:
        return

    cursor.execute(
        """
        WITH events AS (
            SELECT cb.customer_id, cb.bill_date AS event_date, 1 AS kind_rank,
                   cb.bill_id AS seq, cb.amount AS delta, cb.bill_id AS bill_id
            FROM customer_bills cb
            WHERE cb.customer_id = ANY(%(ids)s)

            UNION ALL

            SELECT aa.entity_id, aa.adjustment_date, 0,
                   aa.adjustment_id, aa.amount, NULL
            FROM account_adjustments aa
            WHERE aa.entity_type = 'customer' AND aa.entity_id = ANY(%(ids)s)

            UNION ALL

            SELECT t.entity_id, t.tx_date::date, 2,
                   t.tx_id,
                   CASE WHEN t.tx_type = 'receipt' THEN -t.amount ELSE t.amount END,
                   NULL
            FROM transactions t
            WHERE t.entity_type = 'customer' AND t.entity_id = ANY(%(ids)s)
        ),
        running AS (
            SELECT e.*,
                COALESCE(c.opening_balance, 0) + COALESCE(SUM(e.delta) OVER (
                    PARTITION BY e.customer_id ORDER BY e.event_date, e.kind_rank, e.seq
                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW), 0) AS balance_after,
                COALESCE(c.opening_balance, 0) + COALESCE(SUM(e.delta) OVER (
                    PARTITION BY e.customer_id ORDER BY e.event_date, e.kind_rank, e.seq
                    ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 0) AS balance_before
            FROM events e
            JOIN customers c ON c.customer_id = e.customer_id
        )
        UPDATE customer_bills cb
        SET old_balance   = ROUND(r.balance_before, 2),
            final_balance = ROUND(r.balance_after, 2)
        FROM running r
        WHERE r.bill_id = cb.bill_id
        """,
        {"ids": ids}
    )


# ---------------------------------------------------------------------------
# Supplier ledger resync
#
# Mirrors resync_customer_ledger() above. Previously, supplier_bills.
# old_balance / final_balance and suppliers.balance were maintained purely
# by incremental "delta" patches applied on every create/edit/delete. That
# only stays correct if every single patch in the supplier's history was
# applied perfectly and in true date order — one bad edit (or two requests
# racing on the same bill) permanently drifts suppliers.balance forever
# after, since nothing ever re-derives it from source data. This is why an
# edited "paid" amount could silently fail to "stick": the delta was
# computed against an already-drifted baseline.
#
# This function rebuilds old_balance/final_balance on every supplier_bills
# row AND suppliers.balance itself from scratch, by replaying every dated
# event for that supplier (bills + account_adjustments + transactions) in
# true chronological order, anchored on suppliers.opening_balance. Call it
# after ANY operation that touches a supplier's balance (bill create/edit/
# delete, receipt, payment, manual adjustment).
# ---------------------------------------------------------------------------

def resync_supplier_ledger(cursor, supplier_ids):
    ids = list({int(sid) for sid in supplier_ids if sid is not None})
    if not ids:
        return

    events_cte = """
        WITH events AS (
            SELECT sb.supplier_id, sb.bill_date AS event_date, 1 AS kind_rank,
                   sb.bill_id AS seq,
                   -(COALESCE(sb.total_amount,0) - COALESCE(sb.commission,0)
                     - COALESCE(sb.labour,0) - COALESCE(sb.transport,0)
                     - COALESCE(sb.paid,0)) AS delta,
                   sb.bill_id AS bill_id
            FROM supplier_bills sb
            WHERE sb.supplier_id = ANY(%(ids)s)

            UNION ALL

            SELECT aa.entity_id, aa.adjustment_date, 0,
                   aa.adjustment_id, aa.amount, NULL
            FROM account_adjustments aa
            WHERE aa.entity_type = 'supplier' AND aa.entity_id = ANY(%(ids)s)

            UNION ALL

            SELECT t.entity_id, t.tx_date::date, 2,
                   t.tx_id,
                   CASE WHEN t.tx_type = 'receipt' THEN -t.amount ELSE t.amount END,
                   NULL
            FROM transactions t
            WHERE t.entity_type = 'supplier' AND t.entity_id = ANY(%(ids)s)
        )
    """

    # 1) Rebuild the per-bill old_balance/final_balance snapshots.
    cursor.execute(
        events_cte + """
        , running AS (
            SELECT e.*,
                COALESCE(s.opening_balance, 0) + COALESCE(SUM(e.delta) OVER (
                    PARTITION BY e.supplier_id ORDER BY e.event_date, e.kind_rank, e.seq
                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW), 0) AS balance_after,
                COALESCE(s.opening_balance, 0) + COALESCE(SUM(e.delta) OVER (
                    PARTITION BY e.supplier_id ORDER BY e.event_date, e.kind_rank, e.seq
                    ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 0) AS balance_before
            FROM events e
            JOIN suppliers s ON s.supplier_id = e.supplier_id
        )
        UPDATE supplier_bills sb
        SET old_balance   = ROUND(r.balance_before, 2),
            final_balance = ROUND(r.balance_after, 2)
        FROM running r
        WHERE r.bill_id = sb.bill_id
        """,
        {"ids": ids}
    )

    # 2) Rebuild suppliers.balance itself as opening_balance + sum of every
    #    event, so it can never silently drift away from the truth again.
    cursor.execute(
        events_cte + """
        , totals AS (
            SELECT e.supplier_id,
                   COALESCE(s.opening_balance, 0) + COALESCE(SUM(e.delta), 0) AS total_balance
            FROM events e
            JOIN suppliers s ON s.supplier_id = e.supplier_id
            GROUP BY e.supplier_id, s.opening_balance
        )
        UPDATE suppliers s
        SET balance = ROUND(t.total_balance, 2)
        FROM totals t
        WHERE t.supplier_id = s.supplier_id
        """,
        {"ids": ids}
    )


# ---------------------------------------------------------------------------
# Home
# ---------------------------------------------------------------------------



@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        password = request.form.get('password')
        app_password = os.environ.get('APP_ACCESS_PASSWORD')
        if app_password and password == app_password:
            # This tells Flask to apply the 10-minute timer
             
            session['logged_in'] = True
            return redirect(url_for('home'))
        else:
            flash('Incorrect password. Please try again.', 'danger')
            return redirect(url_for('login'))
            
    return render_template('login.html')

@app.route('/')

def home():
    # Check if the user is logged in
    if not session.get('logged_in'):
        return redirect(url_for('login'))
        
    return render_template('home.html')



# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------
@app.route("/customers/add", methods=["GET", "POST"])
def add_customer():
    if request.method == "POST":
        name = request.form["name"].strip().title()
        phone = request.form.get("phone", "")
        phone = re.sub(r"\D", "", phone)
        address = request.form.get("address")
        opening_balance = float(request.form.get("opening_balance", 0) or 0)

        conn = get_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        # Check duplicate customer
        cursor.execute("""
            SELECT customer_id
            FROM customers
            WHERE LOWER(TRIM(name)) = LOWER(TRIM(%s))
        """, (name,))

        existing = cursor.fetchone()

        if existing:
            cursor.close()
            conn.close()
            flash("Customer already present. Cannot add duplicate customer.", "danger")
            return redirect(url_for("add_customer"))

        # Create customer with opening balance as current balance
        cursor.execute("""
            INSERT INTO customers
            (name, phone, address, opening_balance, balance)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING customer_id
        """, (
            name,
            phone,
            address,
            opening_balance,
            opening_balance
        ))

        customer_id = cursor.fetchone()["customer_id"]

        # Save opening balance in adjustment history
        if opening_balance > 0:
            cursor.execute("""
                INSERT INTO account_adjustments
                (entity_type, entity_id, amount, adjustment_date)
                VALUES (%s, %s, %s, CURRENT_DATE)
            """, (
                "customer",
                customer_id,
                opening_balance
            ))

        conn.commit()
        cursor.close()
        conn.close()

        flash("Customer added successfully!", "success")
        return redirect(url_for("add_customer"))

    return render_template("add_customer.html")
# ---------------------------------------------------------------------------
# Suppliers
# ---------------------------------------------------------------------------
@app.route("/suppliers/add", methods=["GET", "POST"])
def add_supplier():
    if request.method == "POST":
        name = request.form["name"].strip().title()
        phone = request.form.get("phone")
        address = request.form.get("address")
        opening_balance = float(request.form.get("opening_balance", 0) or 0)

        conn = get_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        # Check duplicate supplier
        cursor.execute("""
            SELECT supplier_id
            FROM suppliers
            WHERE LOWER(TRIM(name)) = LOWER(TRIM(%s))
        """, (name,))

        existing = cursor.fetchone()

        if existing:
            cursor.close()
            conn.close()
            flash("Supplier already present. Cannot add duplicate supplier.", "danger")
            return redirect(url_for("add_supplier"))

        # Create supplier with opening balance as current balance
        cursor.execute("""
            INSERT INTO suppliers
            (name, phone, address, opening_balance, balance)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING supplier_id
        """, (
            name,
            phone,
            address,
            opening_balance,
            opening_balance
        ))

        supplier_id = cursor.fetchone()["supplier_id"]

        # Store opening balance in adjustment history
        if opening_balance > 0:
            cursor.execute("""
                INSERT INTO account_adjustments
                (entity_type, entity_id, amount, adjustment_date)
                VALUES (%s, %s, %s, CURRENT_DATE)
            """, (
                "supplier",
                supplier_id,
                opening_balance
            ))

        conn.commit()
        cursor.close()
        conn.close()

        flash("Supplier added successfully!", "success")
        return redirect(url_for("add_supplier"))

    return render_template("add_supplier.html")
@app.route("/api/check_customer")

def check_customer():

    name = request.args.get("name", "").strip().title()

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    cursor.execute("""
        SELECT customer_id
        FROM customers
        WHERE LOWER(TRIM(name)) = LOWER(TRIM(%s))
    """, (name,))

    exists = cursor.fetchone() is not None

    cursor.close()
    conn.close()

    return jsonify({"exists": exists})

@app.route("/api/check_supplier")

def check_supplier():

    name = request.args.get("name", "").strip().title()

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    cursor.execute("""
        SELECT supplier_id
        FROM suppliers
        WHERE LOWER(TRIM(name)) = LOWER(TRIM(%s))
    """, (name,))

    exists = cursor.fetchone() is not None

    cursor.close()
    conn.close()

    return jsonify({"exists": exists})
@app.route("/api/supplier/<int:supplier_id>/balance")

def get_supplier_balance(supplier_id):
    conn   = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT balance FROM suppliers WHERE supplier_id = %s", (supplier_id,))
    row    = cursor.fetchone()
    cursor.close()
    conn.close()
    return jsonify({"old_balance": float(row[0]) if row else 0.0})

@app.route("/api/customer/<int:customer_id>")

def get_customer(customer_id):

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    cursor.execute("""
        SELECT customer_id, name
        FROM customers
        WHERE customer_id = %s
    """, (customer_id,))

    row = cursor.fetchone()

    cursor.close()
    conn.close()

    if not row:
        return jsonify({"success": False})

    return jsonify({
        "success": True,
        "customer_id": row["customer_id"],
        "name": row["name"]
    })


# ---------------------------------------------------------------------------
# Supplier Bills — Create
# ---------------------------------------------------------------------------

@app.route("/bills/supplier/add", methods=["GET", "POST"])

def add_supplier_bill():
    if request.method == "POST":
        raw_supplier_id = request.form.get("supplier_id")
        if not raw_supplier_id:
            flash("Please select a supplier before creating the bill.", "danger")
            return redirect(url_for("add_supplier_bill"))

        try:
            supplier_id = int(raw_supplier_id)
        except ValueError:
            flash("Invalid supplier id.", "danger")
            return redirect(url_for("add_supplier_bill"))

        bill_date = request.form.get("bill_date") or ist_today().isoformat()

        # Mandatory fields check
        if not request.form.get("commission", "").strip():
            flash("Commission is required.", "danger")
            return redirect(url_for("add_supplier_bill"))
        
        if not request.form.get("labour", "").strip():
            flash("Labour is required.", "danger")
            return redirect(url_for("add_supplier_bill"))
        
        if not request.form.get("transport", "").strip():
            flash("Transport is required.", "danger")
            return redirect(url_for("add_supplier_bill"))
        
        commission_raw = request.form.get("commission", "").strip()

        try:
            labour     = float(request.form.get("labour"))
            transport  = float(request.form.get("transport"))
            paid       = float(request.form.get("paid") or 0)
        except ValueError:
            flash("Commission, Labour and Transport must be valid numbers.", "danger")
            return redirect(url_for("add_supplier_bill"))
        
        customer_ids = request.form.getlist("customer_id[]")
        quantities   = request.form.getlist("quantity[]")
        rates        = request.form.getlist("rate[]")

        items        = []
        total_amount = 0.0

        for cid, q, r in zip(customer_ids, quantities, rates):
            if not cid:
                continue
            try:
                qty  = float(q or 0)
                rate = float(r or 0)
            except ValueError:
                qty, rate = 0.0, 0.0
            if qty == 0 and rate == 0:
                continue
            amount        = qty * rate
            total_amount += amount
            items.append({"customer_id": int(cid), "quantity": qty, "rate": rate, "amount": amount})

        pct = float(commission_raw.replace("%", "")) if commission_raw else 0.0

        commission = total_amount * pct / 100

        bill_balance = total_amount - commission - labour - transport - paid

        try:
            bd = bill_date if isinstance(bill_date, date) else datetime.strptime(str(bill_date), "%Y-%m-%d").date()
            recompute_cash_in_hand_from_date(bd)
        except Exception:
            pass

        conn   = get_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        try:
            # Lock the supplier row for the rest of this transaction. This
            # is what actually stops a double-submit / concurrent edit from
            # racing: whichever request gets here second simply waits until
            # the first one commits, instead of both reading the same
            # "old" balance and one of their updates getting lost.
            cursor.execute(
                "SELECT balance FROM suppliers WHERE supplier_id = %s FOR UPDATE",
                (supplier_id,)
            )
            row = cursor.fetchone()
            old_supplier_balance = float(row["balance"]) if row and row["balance"] is not None else 0.0

            # old_balance/final_balance are written as placeholders here —
            # resync_supplier_ledger() below is the single source of truth
            # for these and for suppliers.balance, so we never again rely
            # on a snapshot that can silently drift.
            cursor.execute(
                """
                INSERT INTO supplier_bills
                    (supplier_id,bill_date,total_amount,commission,commission_text,transport,labour,paid,balance,old_balance,final_balance)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING bill_id
                """,
                (
                    supplier_id,
                    bill_date,
                    total_amount,
                    commission,
                    commission_raw,
                    transport,
                    labour,
                    paid,
                    bill_balance,
                    0,
                    0
                )
            )
            bill_id = cursor.fetchone()["bill_id"]

            for it in items:
                cid    = it["customer_id"]
                qty    = it["quantity"]
                rate   = it["rate"]
                amount = it["amount"]

                cursor.execute(
                    "INSERT INTO supplier_bill_items (bill_id, customer_id, quantity, rate, amount) VALUES (%s, %s, %s, %s, %s)",
                    (bill_id, cid, qty, rate, amount)
                )

                cursor.execute("SELECT balance FROM customers WHERE customer_id = %s", (cid,))
                crow     = cursor.fetchone()
                cust_old = float(crow["balance"]) if crow and crow["balance"] is not None else 0.0
                cust_new = cust_old + amount

                cursor.execute(
                    """
                    INSERT INTO customer_bills
                        (customer_id, bill_date, quantity, rate, amount, commission, net_amount, supplier_bill_id, old_balance, final_balance)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (cid, bill_date, qty, rate, amount, 0, amount, bill_id, cust_old, cust_new)
                )
                cursor.execute(
                    "UPDATE customers SET balance = %s WHERE customer_id = %s",
                    (cust_new, cid)
                )

            resync_customer_ledger(cursor, [it["customer_id"] for it in items])
            resync_supplier_ledger(cursor, [supplier_id])

            cursor.execute("SELECT balance FROM suppliers WHERE supplier_id = %s", (supplier_id,))
            row = cursor.fetchone()
            new_supplier_balance = float(row["balance"]) if row and row["balance"] is not None else 0.0

            previous_owed  = abs(old_supplier_balance) if old_supplier_balance < 0 else 0.0
            resulting_owed = abs(new_supplier_balance)  if new_supplier_balance < 0 else 0.0

            conn.commit()

        except Exception as e:
            conn.rollback()
            app.logger.exception("Failed creating supplier bill")
            flash(f"Database error: {e}", "danger")
            cursor.close()
            conn.close()
            return redirect(url_for("add_supplier_bill"))

        finally:
            try:
                cursor.close()
                conn.close()
            except Exception:
                pass

        if new_supplier_balance < 0:
            flash(
                f"Supplier bill #{bill_id} created. Previously owed ₹{previous_owed:.2f}. "
                f"Bill: ₹{bill_balance:.2f}. Now owe ₹{resulting_owed:.2f}.",
                "success"
            )
        else:
            flash(
                f"Supplier bill #{bill_id} created. Supplier previously owed us ₹{abs(old_supplier_balance):.2f}. "
                f"Bill: ₹{bill_balance:.2f}. Now supplier owes us ₹{new_supplier_balance:.2f}.",
                "success"
            )
        return redirect(
            url_for(
                "supplier_bill_print",
                bill_id=bill_id
            )
        )

    # GET
    conn   = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT supplier_id AS id, name, balance FROM suppliers ORDER BY name ASC")
    suppliers = cursor.fetchall()
    cursor.execute("SELECT customer_id AS id, name FROM customers ORDER BY name ASC")
    customers = cursor.fetchall()
    cursor.execute("""
        SELECT COALESCE(MAX(bill_id),0) + 1 AS next_bill_no
        FROM supplier_bills
    """)
    next_bill_no = cursor.fetchone()["next_bill_no"]
    cursor.close()
    conn.close()
    return render_template("supplier_bill.html", today=ist_today().isoformat(),
                           suppliers=suppliers, customers=customers,next_bill_no=next_bill_no)


# ---------------------------------------------------------------------------
# Supplier Bills — Search
# ---------------------------------------------------------------------------

@app.route("/bills/supplier/search", methods=["GET", "POST"])

def supplier_bill_search():
    name = ""
    bill_date = ""
    bill_no = ""
    results = []

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        bill_date = request.form.get("bill_date", "").strip()
        bill_no = request.form.get("bill_no", "").strip()
    else:
        name = request.args.get("name", "").strip()
        bill_date = request.args.get("bill_date", "").strip()
        bill_no = request.args.get("bill_no", "").strip()

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    if bill_no:
        try:
            cursor.execute(
                """
                SELECT b.*, s.name AS supplier_name
                FROM supplier_bills b
                JOIN suppliers s
                    ON b.supplier_id = s.supplier_id
                WHERE b.bill_id = %s
                """,
                (int(bill_no),)
            )

            row = cursor.fetchone()

            if row:
                results = [row]

        except ValueError:
            pass

    else:
        sql = """
            SELECT b.*, s.name AS supplier_name
            FROM supplier_bills b
            JOIN suppliers s
                ON b.supplier_id = s.supplier_id
            WHERE 1=1
        """

        params = []

        if name:
            sql += " AND s.name ILIKE %s"
            params.append(f"%{name}%")

        if bill_date:
            sql += " AND b.bill_date = %s"
            params.append(bill_date)

        sql += " ORDER BY b.bill_date DESC, b.bill_id DESC"

        cursor.execute(sql, params)
        results = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template(
        "supplier_bill_search.html",
        results=results,
        name=name,
        bill_date=bill_date,
        bill_no=bill_no,
        today=ist_today()
    )
# ---------------------------------------------------------------------------
# Supplier Bills — Edit
# ---------------------------------------------------------------------------
@app.route("/bills/supplier/edit/<int:bill_id>", methods=["GET", "POST"])
def supplier_bill_edit(bill_id):
    conn   = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    cursor.execute("""
        SELECT
            sb.*,
            s.name AS supplier_name
        FROM supplier_bills sb
        LEFT JOIN suppliers s
            ON sb.supplier_id = s.supplier_id
        WHERE sb.bill_id = %s
    """, (bill_id,))
    bill = cursor.fetchone()
    if not bill:
        cursor.close(); conn.close()
        flash("Bill not found", "danger")
        return redirect(url_for("supplier_bill_search"))

    bill_date_obj = (bill["bill_date"] if isinstance(bill["bill_date"], date)
                      else datetime.strptime(str(bill["bill_date"]), "%Y-%m-%d").date())

    if (ist_today() - bill_date_obj).days > 30:
        cursor.close(); conn.close()
        flash("This bill is older than 30 days and can no longer be edited.", "danger")
        return redirect(url_for("supplier_bill_search"))

    cursor.execute("""
        SELECT
            sbi.*,
            c.name AS customer_name
        FROM supplier_bill_items sbi
        LEFT JOIN customers c
            ON sbi.customer_id = c.customer_id
        WHERE sbi.bill_id = %s
    """, (bill_id,))
    items = cursor.fetchall()

    cursor.execute("SELECT * FROM customer_bills WHERE supplier_bill_id = %s", (bill_id,))
    cust_bills = cursor.fetchall()

    cursor.execute("""
        SELECT customer_id, name
        FROM customers
        ORDER BY name
    """)
    customers = cursor.fetchall()

    if request.method == "POST":
        try:
            commission_raw = (
                request.form.get("commission") or "0"
            ).strip()
            labour        = to_decimal(request.form.get("labour")     or 0)
            transport     = to_decimal(request.form.get("transport")  or 0)
            paid          = to_decimal(request.form.get("paid")       or 0)
            bill_date_str = request.form.get("bill_date") or bill["bill_date"]

            new_bill_date = (datetime.strptime(bill_date_str, "%Y-%m-%d").date()
                             if isinstance(bill_date_str, str)
                             else bill_date_str)
            old_bill_date = (bill["bill_date"] if isinstance(bill["bill_date"], date)
                             else datetime.strptime(str(bill["bill_date"]), "%Y-%m-%d").date())

            customer_ids = request.form.getlist("customer_id[]")
            quantities   = request.form.getlist("quantity[]")
            rates_list   = request.form.getlist("rate[]")

            new_items        = []
            new_total_amount = to_decimal(0)

            for cid, q, r in zip(customer_ids, quantities, rates_list):
                if not cid:
                    continue
                qty    = to_decimal(q or 0)
                rate   = to_decimal(r or 0)
                amount = (qty * rate).quantize(Decimal("0.01"))
                new_items.append({"customer_id": int(cid), "quantity": qty, "rate": rate, "amount": amount})
                new_total_amount += amount

            # ------------------------------------------------------------
            # FIX: aggregate new_items by customer_id BEFORE computing any
            # per-customer deltas. Without this, a customer appearing on
            # multiple line items in the edited bill gets old_amt (the full
            # old aggregate) subtracted once per row instead of once total,
            # corrupting customers.balance.
            # ------------------------------------------------------------
            new_customer_sums = {}
            for it in new_items:
                cid = it["customer_id"]
                new_customer_sums[cid] = new_customer_sums.get(cid, to_decimal(0)) + it["amount"]

            pct = to_decimal(commission_raw.replace("%", "")) if commission_raw else to_decimal(0)
            commission = (
                new_total_amount * pct / Decimal("100")
            ).quantize(Decimal("0.01"))

            new_bill_balance = (new_total_amount - commission - labour - transport - paid).quantize(Decimal("0.01"))

            # Lock the supplier row for the rest of this transaction so a
            # double-submit or a concurrent edit on another bill for the
            # same supplier can't race with this one.
            cursor.execute(
                "SELECT balance FROM suppliers WHERE supplier_id = %s FOR UPDATE",
                (bill["supplier_id"],)
            )

            # old_balance/final_balance are no longer computed here from the
            # bill's own previously-stored snapshot (bill.get("old_balance")
            # / bill.get("final_balance")) — that snapshot can already have
            # drifted from a prior edit or an out-of-order bill, and basing
            # this edit's delta on a drifted number is exactly what let a
            # paid-amount edit fail to "stick". resync_supplier_ledger()
            # below rebuilds this bill's balance columns (and every other
            # bill's, and suppliers.balance) from the full event history
            # instead, so placeholders are fine here.
            cursor.execute(
                """
                UPDATE supplier_bills
                SET bill_date=%s,
                    total_amount=%s,
                    commission=%s,
                    commission_text=%s,
                    transport=%s,
                    labour=%s,
                    paid=%s,
                    balance=%s
                WHERE bill_id=%s
                """,
                (
                    new_bill_date,
                    float(new_total_amount),
                    float(commission),
                    commission_raw,
                    float(transport),
                    float(labour),
                    float(paid),
                    float(new_bill_balance),
                    bill_id
                )
            )

            cursor.execute(
                "SELECT customer_id, SUM(amount) AS total_amount FROM supplier_bill_items "
                "WHERE bill_id = %s GROUP BY customer_id",
                (bill_id,)
            )
            old_customer_sums = {row["customer_id"]: to_decimal(row["total_amount"]) for row in cursor.fetchall()}

            cursor.execute("DELETE FROM supplier_bill_items WHERE bill_id = %s",        (bill_id,))
            cursor.execute("DELETE FROM customer_bills WHERE supplier_bill_id = %s",    (bill_id,))

            # ------------------------------------------------------------
            # old_balance/final_balance on each re-inserted customer_bills
            # row can't be derived from the customer's CURRENT balance —
            # if this bill is dated earlier than other activity the
            # customer has had since, "current balance minus this bill's
            # old amount" pulls in stuff that happened after this bill's
            # date and corrupts its opening-balance snapshot.
            #
            # Insert placeholder values here (they only need to be roughly
            # right so nothing downstream chokes on NULLs) and let
            # resync_customer_ledger() below rebuild the real values by
            # replaying every dated event for these customers in true
            # chronological order.
            # ------------------------------------------------------------
            all_customer_ids = set(old_customer_sums.keys()) | set(new_customer_sums.keys())

            for it in new_items:
                cid    = it["customer_id"]
                qty    = it["quantity"]
                rate   = it["rate"]
                amount = it["amount"]

                cursor.execute(
                    "INSERT INTO supplier_bill_items (bill_id, customer_id, quantity, rate, amount) "
                    "VALUES (%s,%s,%s,%s,%s)",
                    (bill_id, cid, float(qty), float(rate), float(amount))
                )

                cursor.execute(
                    """
                    INSERT INTO customer_bills
                        (customer_id, bill_date, quantity, rate, amount, commission, net_amount, supplier_bill_id, old_balance, final_balance)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (cid, new_bill_date, float(qty), float(rate), float(amount), 0.0, float(amount),
                     bill_id, 0.0, float(amount))
                )

            # ------------------------------------------------------------
            # Apply customer balance deltas exactly once per customer,
            # comparing aggregated old sum vs aggregated new sum. Covers
            # customers removed entirely (new_amt = 0), customers newly
            # added (old_amt = 0), and customers with duplicate rows in
            # either the old or new item set.
            # ------------------------------------------------------------
            for cid in all_customer_ids:
                old_amt = old_customer_sums.get(cid, to_decimal(0))
                new_amt = new_customer_sums.get(cid, to_decimal(0))
                delta   = new_amt - old_amt
                if delta != 0:
                    cursor.execute(
                        "UPDATE customers SET balance = balance + %s WHERE customer_id = %s",
                        (float(delta), cid)
                    )

            resync_customer_ledger(cursor, all_customer_ids)
            resync_supplier_ledger(cursor, [bill["supplier_id"]])

            conn.commit()

        except Exception as e:
            conn.rollback()
            cursor.close(); conn.close()
            flash("Error updating supplier bill: " + str(e), "danger")
            return redirect(url_for("supplier_bill_edit", bill_id=bill_id))

        finally:
            try:
                cursor.close(); conn.close()
            except Exception:
                pass

        try:
            recompute_cash_in_hand_for_date(old_bill_date)
        except Exception:
            pass
        try:
            recompute_cash_in_hand_for_date(new_bill_date)
        except Exception:
            pass
        if new_bill_date != ist_today():
            recompute_cash_in_hand_for_date(ist_today())

        flash("Supplier bill updated successfully", "success")
        return redirect(url_for("supplier_bill_search"))

    cursor.close(); conn.close()
    return render_template(
        "supplier_bill_edit.html",
        bill=bill,
        items=items,
        cust_bills=cust_bills,
        customers=customers
    )
# ---------------------------------------------------------------------------
# Supplier Bills — Delete
# ---------------------------------------------------------------------------

@app.route("/bills/supplier/delete/<int:bill_id>", methods=["POST"])

def supplier_bill_delete(bill_id):
    conn   = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    bill_date = None

    try:
        cursor.execute("SELECT * FROM supplier_bills WHERE bill_id = %s", (bill_id,))
        bill = cursor.fetchone()
        if not bill:
            conn.rollback()
            cursor.close(); conn.close()
            flash("Supplier bill not found", "danger")
            return redirect(url_for("supplier_bill_search"))

        supplier_id = bill["supplier_id"]
        bill_date   = bill["bill_date"]

        # Lock the supplier row for the rest of this transaction — same
        # reasoning as add/edit: prevents a delete racing with another
        # create/edit/delete on the same supplier.
        cursor.execute(
            "SELECT balance FROM suppliers WHERE supplier_id = %s FOR UPDATE",
            (supplier_id,)
        )

        cursor.execute(
            "SELECT customer_id, SUM(amount) AS total_amount FROM supplier_bill_items "
            "WHERE bill_id = %s GROUP BY customer_id",
            (bill_id,)
        )
        affected_customer_ids = []
        for row in cursor.fetchall():
            affected_customer_ids.append(row["customer_id"])
            cursor.execute(
                "UPDATE customers SET balance = balance - %s WHERE customer_id = %s",
                (float(to_decimal(row["total_amount"] or 0)), row["customer_id"])
            )

        cursor.execute("DELETE FROM supplier_bill_items WHERE bill_id = %s",     (bill_id,))
        cursor.execute("DELETE FROM customer_bills WHERE supplier_bill_id = %s", (bill_id,))
        cursor.execute("DELETE FROM supplier_bills WHERE bill_id = %s",          (bill_id,))

        resync_customer_ledger(cursor, affected_customer_ids)
        # bill_id no longer exists in supplier_bills, so this correctly
        # rebuilds the supplier's balance and every remaining bill's
        # snapshot as if the deleted bill had never happened.
        resync_supplier_ledger(cursor, [supplier_id])

        conn.commit()

    except Exception as e:
        conn.rollback()
        cursor.close(); conn.close()
        flash("Error deleting supplier bill: " + str(e), "danger")
        return redirect(url_for("supplier_bill_search"))

    finally:
        try:
            cursor.close(); conn.close()
        except Exception:
            pass

    if bill_date:
        try:
            bd = bill_date if isinstance(bill_date, date) else datetime.strptime(str(bill_date), "%Y-%m-%d").date()
            recompute_cash_in_hand_for_date(bd)
        except Exception:
            pass
    recompute_cash_in_hand_for_date(ist_today())

    flash(f"Supplier bill #{bill_id} deleted and balances adjusted.", "success")
    return redirect(url_for("supplier_bill_search"))


# ---------------------------------------------------------------------------
# Supplier Bills — Print (single)
# ---------------------------------------------------------------------------

@app.route("/bills/supplier/print/<int:bill_id>")

def supplier_bill_print(bill_id):
    conn   = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    cursor.execute(
        "SELECT b.*, s.name AS supplier_name FROM supplier_bills b "
        "JOIN suppliers s ON b.supplier_id = s.supplier_id WHERE b.bill_id = %s",
        (bill_id,)
    )
    bill = cursor.fetchone()
    if not bill:
        cursor.close(); conn.close()
        flash("Supplier bill not found", "danger")
        return redirect(url_for("supplier_bill_search"))

    for fld in ("total_amount", "commission", "transport", "labour", "paid",
                "balance", "old_balance", "final_balance"):
        try:
            bill[fld] = float(bill.get(fld) or 0.0)
        except Exception:
            bill[fld] = 0.0

    cursor.execute(
        "SELECT it.*, c.name AS customer_name FROM supplier_bill_items it "
        "LEFT JOIN customers c ON it.customer_id = c.customer_id "
        "WHERE it.bill_id = %s ORDER BY COALESCE(c.name, CAST(it.customer_id AS VARCHAR))",
        (bill_id,)
    )
    items = cursor.fetchall() or []
    for it in items:
        for f in ("quantity", "rate", "amount"):
            try:
                it[f] = float(it.get(f) or 0.0)
            except Exception:
                it[f] = 0.0

    cursor.execute("SELECT balance FROM suppliers WHERE supplier_id = %s", (bill["supplier_id"],))
    sb              = cursor.fetchone()
    current_balance = float(sb["balance"]) if sb and sb["balance"] is not None else 0.0

    cursor.close(); conn.close()
    return render_template("supplier_bill_print.html", bill=bill, items=items, current_balance=current_balance)


# ---------------------------------------------------------------------------
# Supplier Bills — Print (search / multiple)
# ---------------------------------------------------------------------------

@app.route("/bills/supplier/print_search", methods=["POST"])

def supplier_bill_print_search():
    bill_no   = request.form.get("bill_no")
    name      = request.form.get("name")
    bill_date = request.form.get("bill_date")

    conn   = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    results = []

    if bill_no:
        try:
            cursor.execute(
                "SELECT b.*, s.name AS supplier_name FROM supplier_bills b "
                "JOIN suppliers s ON b.supplier_id = s.supplier_id WHERE b.bill_id = %s",
                (int(bill_no),)
            )
            row = cursor.fetchone()
            if row:
                results = [row]
        except ValueError:
            pass
    else:
        sql    = ("SELECT b.*, s.name AS supplier_name FROM supplier_bills b "
                  "JOIN suppliers s ON b.supplier_id = s.supplier_id WHERE 1=1")
        params = []
        if name:
            sql    += " AND s.name ILIKE %s"
            params.append(f"%{name}%")
        if bill_date:
            sql    += " AND b.bill_date = %s"
            params.append(bill_date)
        cursor.execute(sql, params)
        results = cursor.fetchall()

    printable = []
    for r in results:
        cursor.execute(
            "SELECT it.*, c.name AS customer_name FROM supplier_bill_items it "
            "LEFT JOIN customers c ON it.customer_id = c.customer_id "
            "WHERE it.bill_id = %s ORDER BY COALESCE(c.name, CAST(it.customer_id AS VARCHAR))",
            (r["bill_id"],)
        )
        items = cursor.fetchall() or []
        for it in items:
            for f in ("quantity", "rate", "amount"):
                try:
                    it[f] = float(it.get(f) or 0.0)
                except Exception:
                    it[f] = 0.0

        cursor.execute("SELECT balance FROM suppliers WHERE supplier_id = %s", (r["supplier_id"],))
        sb              = cursor.fetchone()
        current_balance = float(sb["balance"]) if sb and sb["balance"] is not None else 0.0
        printable.append({"bill": r, "items": items, "current_balance": current_balance})

    cursor.close(); conn.close()
    return render_template("supplier_bill_print_many.html", printable=printable)

# ---------------------------------------------------------------------------
# Customer Bills — Search
# ---------------------------------------------------------------------------

@app.route("/bills/customer/search", methods=["GET", "POST"])

def customer_bill_search():
    results = []
    name = ""
    bill_date = ""
    bill_no = ""

    if request.method == "POST":
        bill_no = request.form.get("bill_no", "").strip()
        name = request.form.get("name", "").strip()
        bill_date = request.form.get("bill_date", "").strip()
    else:
        bill_no = request.args.get("bill_no", "").strip()
        name = request.args.get("name", "").strip()
        bill_date = request.args.get("bill_date", "").strip()

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    if not bill_no and not name and not bill_date:

        bill_date = ist_today().strftime("%Y-%m-%d")

        cursor.execute("""
            SELECT cb.*, c.customer_id, c.name AS customer_name
            FROM customer_bills cb
            JOIN customers c
                ON cb.customer_id = c.customer_id
            WHERE cb.bill_date = %s
            ORDER BY cb.bill_id DESC
        """, (bill_date,))

        results = cursor.fetchall()

    else:

        if bill_no:
            try:
                cursor.execute("""
                    SELECT cb.*, c.customer_id, c.name AS customer_name
                    FROM customer_bills cb
                    JOIN customers c
                        ON cb.customer_id = c.customer_id
                    WHERE cb.bill_id = %s
                """, (int(bill_no),))

                row = cursor.fetchone()

                if row:
                    results = [row]

            except ValueError:
                pass

        else:

            sql = """
                SELECT cb.*, c.customer_id, c.name AS customer_name
                FROM customer_bills cb
                JOIN customers c
                    ON cb.customer_id = c.customer_id
                WHERE 1=1
            """

            params = []

            if name:
                sql += """
                    AND (
                        c.name ILIKE %s
                        OR CAST(c.customer_id AS TEXT) = %s
                    )
                """
                params.extend([f"%{name}%", name])

            if bill_date:
                sql += " AND cb.bill_date = %s"
                params.append(bill_date)

            sql += " ORDER BY cb.bill_date DESC, cb.bill_id DESC LIMIT 200"

            cursor.execute(sql, params)
            results = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template(
        "customer_bill_search.html",
        results=results,
        name=name,
        bill_date=bill_date,
        bill_no=bill_no
    )

@app.route("/send_customer_statement_whatsapp", methods=["POST"])
def send_customer_statement_whatsapp():

    customer_id = request.form.get("customer_id")
    statement_date = request.form.get("statement_date")

    if not customer_id:
        flash("Please select a customer.", "danger")
        return redirect(url_for("customer_bill_search"))

    try:
        customer_id = int(customer_id)
        statement_date = datetime.strptime(
            statement_date,
            "%Y-%m-%d"
        ).date()
    except Exception:
        flash("Invalid request.", "danger")
        return redirect(url_for("customer_bill_search"))

    result = send_purchase_statement(
        customer_id,
        statement_date
    )

    if result["success"]:
        flash("WhatsApp message sent successfully.", "success")
    else:
        flash(result["reason"], "danger")

    return redirect(url_for("customer_bill_search"))


@app.route("/preview_bulk_customer_statements_whatsapp", methods=["POST"])
def preview_bulk_customer_statements_whatsapp():

    bill_date_str = request.form.get("bill_date", "").strip()

    if not bill_date_str:
        bill_date_str = ist_today().strftime("%Y-%m-%d")

    try:
        statement_date = datetime.strptime(bill_date_str, "%Y-%m-%d").date()
    except Exception:
        return jsonify({"success": False, "error": "Invalid date."}), 400

    customers = get_customers_for_date(statement_date)

    # Batched once for all customers on this date, rather than one
    # query per customer, so this in-progress check stays cheap even
    # when someone else's bulk send for this date is currently running.
    in_progress_ids = get_in_progress_customer_ids(
        [c["customer_id"] for c in customers],
        statement_date
    )

    blocked_map = get_whatsapp_blocked_customer_ids(
        [c["customer_id"] for c in customers]
    )

    preview = []

    for cust in customers:
        cid = cust["customer_id"]
        cname = cust["name"]
        phone = cust.get("phone")

        summary = get_purchase_summary(cid, statement_date)
        already_sent = has_statement_been_sent(cid, statement_date)
        in_progress = cid in in_progress_ids
        blocked_reason = blocked_map.get(cid)

        preview.append({
            "customer_id": cid,
            "customer_name": cname,
            "phone": phone,
            "purchase_total": summary["purchase_total"],
            "purchase_entries": summary["purchase_entries"],
            "has_phone": bool(phone),
            "already_sent": already_sent,
            "in_progress": in_progress,
            "blocked": blocked_reason is not None,
            "blocked_reason": blocked_reason
        })

    return jsonify({
        "success": True,
        "date": bill_date_str,
        "total": len(preview),
        "customers": preview
    })


@app.route("/unblock_customer_whatsapp", methods=["POST"])
def unblock_customer_whatsapp_route():

    customer_id = request.form.get("customer_id")

    try:
        customer_id = int(customer_id)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Invalid customer id."}), 400

    unblock_customer_whatsapp(customer_id)

    return jsonify({"success": True})


@app.route("/send_bulk_customer_statements_whatsapp", methods=["POST"])
def send_bulk_customer_statements_whatsapp():

    bill_date_str = request.form.get("bill_date", "").strip()

    if not bill_date_str:
        bill_date_str = ist_today().strftime("%Y-%m-%d")

    try:
        statement_date = datetime.strptime(bill_date_str, "%Y-%m-%d").date()
    except Exception:
        return jsonify({"success": False, "error": "Invalid date."}), 400

    selected_ids_raw = request.form.get("customer_ids", "").strip()

    customers = get_customers_for_date(statement_date)

    if selected_ids_raw:
        try:
            selected_ids = {
                int(x) for x in selected_ids_raw.split(",") if x.strip()
            }
            customers = [
                c for c in customers if c["customer_id"] in selected_ids
            ]
        except ValueError:
            return jsonify({"success": False, "error": "Invalid customer selection."}), 400

    if not customers:
        return jsonify({
            "success": True,
            "date": bill_date_str,
            "total": 0,
            "sent": 0,
            "failed": 0,
            "results": []
        })

    # Skip anyone another request is actively sending to right now,
    # instead of blocking on their advisory lock and then reporting a
    # confusing "already sent" once it releases. The lock inside
    # send_purchase_statement() is still what actually prevents any
    # duplicate message - this is just so the caller gets an honest,
    # immediate "in progress" reason rather than waiting on it.
    in_progress_ids = get_in_progress_customer_ids(
        [c["customer_id"] for c in customers],
        statement_date
    )

    blocked_map = get_whatsapp_blocked_customer_ids(
        [c["customer_id"] for c in customers]
    )

    results = []
    sent_count = 0
    failed_count = 0

    for cust in customers:
        cid = cust["customer_id"]
        cname = cust["name"]

        if not cust.get("phone"):
            results.append({
                "customer_id": cid,
                "customer_name": cname,
                "status": "failed",
                "reason": "No phone number on file"
            })
            failed_count += 1
            continue

        if cid in blocked_map:
            results.append({
                "customer_id": cid,
                "customer_name": cname,
                "status": "failed",
                "reason": f"Blocked - {blocked_map[cid]}"
            })
            failed_count += 1
            continue

        if cid in in_progress_ids:
            results.append({
                "customer_id": cid,
                "customer_name": cname,
                "status": "failed",
                "reason": "Already being sent by another request right now"
            })
            failed_count += 1
            continue

        outcome = send_purchase_statement(cid, statement_date)

        if outcome.get("success"):
            results.append({
                "customer_id": cid,
                "customer_name": cname,
                "status": "sent"
            })
            sent_count += 1
        else:
            results.append({
                "customer_id": cid,
                "customer_name": cname,
                "status": "failed",
                "reason": str(outcome.get("reason", "Unknown error"))
            })
            failed_count += 1

    return jsonify({
        "success": True,
        "date": bill_date_str,
        "total": len(customers),
        "sent": sent_count,
        "failed": failed_count,
        "results": results
    })


@app.route("/send-statement/<int:bill_id>", methods=["POST"])
def send_statement(bill_id):
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    try:
        # Get customer and bill date from the selected bill
        cursor.execute("""
            SELECT
                customer_id,
                bill_date
            FROM customer_bills
            WHERE bill_id = %s
        """, (bill_id,))

        bill = cursor.fetchone()

        if not bill:
            flash("Bill not found.", "danger")
            return redirect(url_for("customer_bill_search"))

        result = send_purchase_statement(
            customer_id=bill["customer_id"],
            statement_date=bill["bill_date"]
        )

        if result["success"]:
            flash(result["message"], "success")
        else:
            flash(result["message"], "danger")

    except Exception as e:
        flash(f"Error sending WhatsApp statement: {str(e)}", "danger")

    finally:
        cursor.close()
        conn.close()

    return redirect(url_for("customer_bill_search"))


            
# ---------------------------------------------------------------------------
# Customer Bills — Edit
# ---------------------------------------------------------------------------

@app.route("/bills/customer/edit/<int:bill_id>", methods=["GET", "POST"])

def customer_bill_edit(bill_id):
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    cursor.execute(
        "SELECT * FROM customer_bills WHERE bill_id = %s",
        (bill_id,)
    )
    bill = cursor.fetchone()

    if not bill:
        cursor.close()
        conn.close()
        flash("Customer bill not found", "danger")
        return redirect(url_for("customer_bill_search"))

    if request.method == "POST":
        try:
            qty = to_decimal(request.form.get("quantity") or 0)
            rate = to_decimal(request.form.get("rate") or 0)

            new_amount = (qty * rate).quantize(Decimal("0.01"))
            old_amount = to_decimal(bill["amount"])
            delta = new_amount - old_amount

            cursor.execute(
                """
                UPDATE customer_bills
                SET quantity = %s,
                    rate = %s,
                    amount = %s
                WHERE bill_id = %s
                """,
                (
                    float(qty),
                    float(rate),
                    float(new_amount),
                    bill_id
                )
            )

            cursor.execute(
                """
                UPDATE customers
                SET balance = balance + %s
                WHERE customer_id = %s
                """,
                (
                    float(delta),
                    bill["customer_id"]
                )
            )

            resync_customer_ledger(cursor, [bill["customer_id"]])

            conn.commit()

        except Exception as e:
            conn.rollback()

            cursor.close()
            conn.close()

            flash(f"Error updating customer bill: {e}", "danger")

            return redirect(
                url_for(
                    "customer_bill_edit",
                    bill_id=bill_id
                )
            )

        finally:
            try:
                cursor.close()
                conn.close()
            except:
                pass

        bill_date_val = bill["bill_date"]

        if isinstance(bill_date_val, str):
            bill_date_val = datetime.strptime(
                bill_date_val,
                "%Y-%m-%d"
            ).date()

        recompute_cash_in_hand_for_date(bill_date_val)

        if bill_date_val != ist_today():
            recompute_cash_in_hand_for_date(ist_today())

        flash("Customer bill updated successfully", "success")
        return redirect(url_for("customer_bill_search"))

    cursor.close()
    conn.close()

    return render_template(
        "customer_bill_edit.html",
        bill=bill
    )

@app.route("/search-customers")
def search_customers():

    q = request.args.get("q", "").strip()

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    cursor.execute("""
        SELECT
            customer_id,
            name
        FROM customers
        WHERE
            name ILIKE %s
            OR CAST(customer_id AS TEXT) ILIKE %s
        ORDER BY name
        LIMIT 10
    """, (
        f"%{q}%",
        f"%{q}%"
    ))

    customers = cursor.fetchall()

    cursor.close()
    conn.close()

    return jsonify(customers)
# ---------------------------------------------------------------------------
# Customer Bills — Delete
# ---------------------------------------------------------------------------

@app.route("/bills/customer/delete/<int:bill_id>", methods=["POST"])

def customer_bill_delete(bill_id):
    conn   = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    bill_date = None

    try:
        cursor.execute("SELECT * FROM customer_bills WHERE bill_id = %s", (bill_id,))
        cb = cursor.fetchone()
        if not cb:
            conn.rollback(); cursor.close(); conn.close()
            flash("Customer bill not found", "danger")
            return redirect(url_for("customer_bill_search"))

        cust_id          = cb["customer_id"]
        bill_date        = cb["bill_date"]
        amount           = to_decimal(cb["amount"] or 0)
        supplier_bill_id = cb.get("supplier_bill_id")

        cursor.execute(
            "UPDATE customers SET balance = balance - %s WHERE customer_id = %s",
            (float(amount), cust_id)
        )

        affected_supplier_id = None

        if supplier_bill_id:
            # Lock the supplier row before touching anything of theirs —
            # same race protection as the supplier-bill routes.
            cursor.execute(
                "SELECT balance FROM suppliers WHERE supplier_id = "
                "(SELECT supplier_id FROM supplier_bills WHERE bill_id = %s) FOR UPDATE",
                (supplier_bill_id,)
            )

            cursor.execute("SELECT * FROM supplier_bills WHERE bill_id = %s", (supplier_bill_id,))
            sb = cursor.fetchone()

            old_total_amount = to_decimal(sb["total_amount"])
            new_total_amount = old_total_amount - amount
            commission       = to_decimal(sb["commission"])
            labour           = to_decimal(sb["labour"])
            transport        = to_decimal(sb["transport"])
            paid             = to_decimal(sb["paid"])
            new_bill_balance = new_total_amount - commission - labour - transport - paid

            affected_supplier_id = sb["supplier_id"]

            # final_balance is left for resync_supplier_ledger() below to
            # rebuild from the full event history — no longer computed
            # from this bill's own (possibly already drifted) old_balance
            # snapshot, and suppliers.balance is no longer hand-patched
            # here either.
            cursor.execute(
                """
                UPDATE supplier_bills
                SET total_amount=%s, balance=%s
                WHERE bill_id=%s
                """,
                (float(new_total_amount), float(new_bill_balance), supplier_bill_id)
            )
            cursor.execute(
                "DELETE FROM supplier_bill_items WHERE bill_id=%s AND customer_id=%s",
                (supplier_bill_id, cust_id)
            )

        cursor.execute("DELETE FROM customer_bills WHERE bill_id = %s", (bill_id,))

        resync_customer_ledger(cursor, [cust_id])
        if affected_supplier_id is not None:
            resync_supplier_ledger(cursor, [affected_supplier_id])

        conn.commit()

    except Exception as e:
        conn.rollback()
        flash("Error deleting customer bill: " + str(e), "danger")
        return redirect(url_for("customer_bill_search"))

    finally:
        try:
            cursor.close(); conn.close()
        except Exception:
            pass

    if bill_date:
        try:
            bd = bill_date if isinstance(bill_date, date) else datetime.strptime(str(bill_date), "%Y-%m-%d").date()
            recompute_cash_in_hand_for_date(bd)
        except Exception:
            pass
    recompute_cash_in_hand_for_date(ist_today())

    flash(f"Customer bill #{bill_id} deleted successfully.", "success")
    return redirect(url_for("customer_bill_search"))


# ---------------------------------------------------------------------------
# Customer Bills — Print (single)
# ---------------------------------------------------------------------------

@app.route("/bills/customer/print/<int:bill_id>")

def customer_bill_print(bill_id):
    conn   = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    cursor.execute(
        "SELECT cb.*, c.name AS customer_name, c.phone AS customer_phone FROM customer_bills cb "
        "JOIN customers c ON cb.customer_id = c.customer_id WHERE cb.bill_id = %s",
        (bill_id,)
    )
    bill = cursor.fetchone()
    if not bill:
        cursor.close(); conn.close()
        flash("Customer bill not found", "danger")
        return redirect(url_for("customer_bill_search"))

    cursor.execute("SELECT balance FROM customers WHERE customer_id = %s", (bill["customer_id"],))
    cb              = cursor.fetchone()
    current_balance = float(cb["balance"]) if cb and cb.get("balance") is not None else 0.0
    cursor.close(); conn.close()

    return render_template("customer_bill_print.html", bill=bill, current_balance=current_balance)


# ---------------------------------------------------------------------------
# Customer Bills — Print (search / multiple)
# ---------------------------------------------------------------------------

@app.route("/bills/customer/print_search", methods=["POST"])

def customer_bill_print_search():
    bill_no     = request.form.get("bill_no",     "").strip()
    name        = request.form.get("name",        "").strip()
    customer_id_in = request.form.get("customer_id", "").strip()
    bill_date   = request.form.get("bill_date",   "").strip()

    conn   = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    def parse_date_or_none(d):
        try:
            return datetime.strptime(d, "%Y-%m-%d").date() if d else None
        except Exception:
            return None

    # 1) Single bill by ID
    if bill_no:
        try:
            bid = int(bill_no)
            cursor.execute(
                "SELECT cb.*, c.name AS customer_name, c.phone AS customer_phone FROM customer_bills cb "
                "JOIN customers c ON cb.customer_id = c.customer_id WHERE cb.bill_id = %s",
                (bid,)
            )
            single = cursor.fetchone()
            if single:
                cursor.execute("SELECT balance FROM customers WHERE customer_id = %s", (single["customer_id"],))
                row             = cursor.fetchone()
                current_balance = float(row["balance"]) if row and row.get("balance") is not None else 0.0
                cursor.close(); conn.close()
                return render_template("customer_bill_print.html", bill=single, current_balance=current_balance)
        except ValueError:
            pass

    parsed_date = parse_date_or_none(bill_date)

    # 2) Single customer consolidated
    if customer_id_in or name:
        customer_id    = None
        customer_name  = None
        current_balance = 0.0

        # Prefer the exact customer_id sent from the autocomplete selection —
        # falling back to name-based lookup only when it's missing/invalid.
        if customer_id_in:
            try:
                cid_val = int(customer_id_in)
                cursor.execute("SELECT customer_id, name, balance FROM customers WHERE customer_id = %s", (cid_val,))
                row = cursor.fetchone()
                if row:
                    customer_id     = row["customer_id"]
                    customer_name   = row["name"]
                    current_balance = float(row["balance"])
            except (ValueError, TypeError):
                pass

        if not customer_id and name:
            try:
                maybe_id = int(name)
                cursor.execute("SELECT customer_id, name, balance FROM customers WHERE customer_id = %s", (maybe_id,))
                row = cursor.fetchone()
                if row:
                    customer_id    = row["customer_id"]
                    customer_name  = row["name"]
                    current_balance = float(row["balance"])
            except (ValueError, TypeError):
                cursor.execute(
                    "SELECT customer_id, name, balance FROM customers WHERE name ILIKE %s ORDER BY name LIMIT 1",
                    (f"%{name}%",)
                )
                row = cursor.fetchone()
                if row:
                    customer_id    = row["customer_id"]
                    customer_name  = row["name"]
                    current_balance = float(row["balance"])

        if not customer_id:
            cursor.close(); conn.close()
            flash("Customer not found", "danger")
            return redirect(url_for("customer_bill_search"))

        from_d = parsed_date or ist_today()
        to_d   = from_d

        cursor.execute(
            """
            SELECT 
                cb.bill_id,
                cb.customer_id,
                cb.bill_date,
                cb.quantity,
                cb.rate,
                cb.amount,
                cb.supplier_bill_id,
                cb.old_balance,
                cb.final_balance,
                s.name AS supplier_name
            FROM customer_bills cb
            LEFT JOIN supplier_bills sb
                ON cb.supplier_bill_id = sb.bill_id
            LEFT JOIN suppliers s
                ON sb.supplier_id = s.supplier_id
            WHERE cb.customer_id = %s
              AND cb.bill_date BETWEEN %s AND %s
            ORDER BY cb.bill_date, cb.bill_id
            """,
            (customer_id, from_d, to_d)
        )
        purchases = cursor.fetchall() or []
        opening   = float(purchases[0].get("old_balance") or 0.0) if purchases else current_balance
        net       = sum(float(p.get("amount") or 0.0) for p in purchases)

        cursor.execute(
            "SELECT COALESCE(SUM(amount),0) AS total_receipts FROM transactions "
            "WHERE entity_type='customer' AND entity_id=%s AND tx_type='receipt' AND DATE(tx_date) BETWEEN %s AND %s",
            (customer_id, from_d, to_d)
        )
        row          = cursor.fetchone()
        sum_receipts = float(row["total_receipts"]) if row else 0.0

        cursor.close(); conn.close()
        return render_template(
            "customer_bill_consolidated_print.html",
            customer_id=customer_id, customer_name=customer_name,
            from_date=from_d, to_date=to_d,
            purchases=purchases, opening=opening, net=net,
            current_balance=current_balance, sum_receipts=sum_receipts
        )

    # 3) Date only → all customers on that date
    if parsed_date:
        period = parsed_date
        cursor.execute(
            "SELECT DISTINCT c.customer_id, c.name AS customer_name, c.balance, c.phone "
            "FROM customer_bills cb JOIN customers c ON cb.customer_id = c.customer_id "
            "WHERE cb.bill_date=%s ORDER BY c.name",
            (period,)
        )
        custs     = cursor.fetchall() or []
        printable = []

        for cust in custs:
            cursor.execute(
                """
                SELECT 
                    cb.bill_id,
                    cb.customer_id,
                    cb.bill_date,
                    cb.quantity,
                    cb.rate,
                    cb.amount,
                    cb.supplier_bill_id,
                    cb.old_balance,
                    cb.final_balance,
                    s.name AS supplier_name
                FROM customer_bills cb
                LEFT JOIN supplier_bills sb
                    ON cb.supplier_bill_id = sb.bill_id
                LEFT JOIN suppliers s
                    ON sb.supplier_id = s.supplier_id
                WHERE cb.customer_id = %s
                  AND cb.bill_date = %s
                ORDER BY cb.bill_date, cb.bill_id
                """,
                (cid, period)
            )
            purchases       = cursor.fetchall() or []
            opening         = float(purchases[0].get("old_balance") or 0.0) if purchases else float(cust["balance"])
            net             = sum(float(p.get("amount") or 0.0) for p in purchases)

            cursor.execute(
                "SELECT COALESCE(SUM(amount),0) AS total_receipts FROM transactions "
                "WHERE entity_type='customer' AND entity_id=%s AND tx_type='receipt' AND DATE(tx_date)=%s",
                (cid, period)
            )
            row      = cursor.fetchone()
            receipts = float(row["total_receipts"]) if row else 0.0

            printable.append({
                "customer_id":    cid,
                "customer_name":  cust.get("customer_name") or f"ID {cid}",
                "customer_phone": cust.get("phone"),
                "purchases":      purchases,
                "opening":        opening,
                "net":            net,
                "sum_receipts":   receipts,
                "current_balance": float(cust["balance"]),
                "date":           period
            })

        cursor.close(); conn.close()
        if not printable:
            flash("No customer bills found.", "info")
            return redirect(url_for("customer_bill_search"))
        return render_template("customer_bill_consolidated_many.html", printable=printable, now=datetime.now)

    cursor.close(); conn.close()
    flash("Provide Bill No, Name, or Date.", "warning")
    return redirect(url_for("customer_bill_search"))


# ---------------------------------------------------------------------------
# Transactions — apply helper
# ---------------------------------------------------------------------------

def apply_transaction(entity_type, entity_id, tx_type, amount, note, tx_date):
    conn   = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    try:
        if entity_type == "customer":
            cursor.execute("SELECT customer_id, name, balance FROM customers WHERE customer_id = %s", (entity_id,))
            row = cursor.fetchone()
            if not row:
                return False, "Customer not found", None
            old_bal = float(row["balance"] or 0.0)
            new_bal = old_bal - amount if tx_type == "receipt" else old_bal + amount
            cursor.execute("UPDATE customers SET balance = %s WHERE customer_id = %s", (new_bal, entity_id))

        elif entity_type == "supplier":
            cursor.execute("SELECT supplier_id, name, balance FROM suppliers WHERE supplier_id = %s", (entity_id,))
            row = cursor.fetchone()
            if not row:
                return False, "Supplier not found", None
            old_bal = float(row["balance"] or 0.0)
            new_bal = old_bal - amount if tx_type == "receipt" else old_bal + amount
            cursor.execute("UPDATE suppliers SET balance = %s WHERE supplier_id = %s", (new_bal, entity_id))
        else:
            return False, "Invalid entity type", None

        cursor.execute(
            "INSERT INTO transactions (entity_type, entity_id, tx_type, amount, note, tx_date) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (entity_type, entity_id, tx_type, amount, note, tx_date)
        )

        if entity_type == "customer":
            resync_customer_ledger(cursor, [entity_id])

        conn.commit()

        # Recompute cash in hand automatically for the transaction date
        try:
            if isinstance(tx_date, str):
                bd = datetime.strptime(tx_date, "%Y-%m-%d").date()
            elif isinstance(tx_date, datetime):
                bd = tx_date.date()
            else:
                bd = tx_date
            recompute_cash_in_hand_from_date(bd)
        except Exception:
            pass

        return True, "Transaction applied", {"old_balance": old_bal, "new_balance": new_bal, "entity": row}

    except Exception as e:
        conn.rollback()
        return False, str(e), None

    finally:
        cursor.close()
        conn.close()


# ---------------------------------------------------------------------------
# Transactions — entity resolver
# ---------------------------------------------------------------------------

def get_entity_id_from_raw(conn, raw_value, entity_type):
    raw = (raw_value or "").strip()
    if not raw:
        return None, None

    try:
        eid = int(raw)
        if eid > 0:
            return eid, None
    except (ValueError, TypeError):
        pass

    m = re.search(r'\b(\d+)\b', raw)
    if m:
        try:
            return int(m.group(1)), None
        except Exception:
            pass

    cur = conn.cursor(cursor_factory=RealDictCursor)
    if entity_type == "customer":
        cur.execute("SELECT customer_id AS id, name FROM customers WHERE name ILIKE %s ORDER BY name LIMIT 1", (f"%{raw}%",))
    else:
        cur.execute("SELECT supplier_id AS id, name FROM suppliers WHERE name ILIKE %s ORDER BY name LIMIT 1", (f"%{raw}%",))
    row = cur.fetchone()
    cur.close()
    if row:
        return int(row["id"]), row["name"]
    return None, None


# ---------------------------------------------------------------------------
# Transactions — Receipt
# ---------------------------------------------------------------------------

@app.route("/transactions/receipt", methods=["GET", "POST"])

def receipt_page():
    conn   = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    if request.method == "POST":
        entity_type = request.form.get("entity_type")
        raw_input   = (request.form.get("entity_id") or request.form.get("entity_name") or "").strip()
        entity_id, _ = get_entity_id_from_raw(conn, raw_input, entity_type)

        if not entity_id:
            flash("Entity not found.", "warning")
            cursor.close(); conn.close()
            return redirect(url_for("receipt_page"))

        amount   = float(request.form.get("amount") or 0)
        note     = request.form.get("note")
        tx_date  = request.form.get("tx_date")

        ok, msg, data = apply_transaction(entity_type, entity_id, "receipt", amount, note, tx_date)
        if not ok:
            flash(f"Error: {msg}", "danger")
        else:
           recompute_cash_in_hand_from_date(
                datetime.strptime(tx_date, "%Y-%m-%d").date()
            )
        
           flash(
                f"Receipt saved. Old balance: {data['old_balance']:.2f}, New balance: {data['new_balance']:.2f}",
                "success"
            )
        cursor.close(); conn.close()
        return redirect(url_for("receipt_page"))

    cursor.execute("SELECT customer_id AS id, name FROM customers ORDER BY name ASC")
    customers = cursor.fetchall()
    cursor.execute("SELECT supplier_id AS id, name FROM suppliers ORDER BY name ASC")
    suppliers = cursor.fetchall()
    cursor.close(); conn.close()
    return render_template("receipt.html", customers=customers, suppliers=suppliers)


# ---------------------------------------------------------------------------
# Transactions — Payment
# ---------------------------------------------------------------------------

@app.route("/transactions/payment", methods=["GET", "POST"])

def payment_page():
    conn   = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    if request.method == "POST":
        entity_type = request.form.get("entity_type")
        raw_input   = (request.form.get("entity_id") or request.form.get("entity_name") or "").strip()
        entity_id, _ = get_entity_id_from_raw(conn, raw_input, entity_type)

        if not entity_id:
            flash("Entity not found.", "warning")
            cursor.close(); conn.close()
            return redirect(url_for("payment_page"))

        amount  = float(request.form.get("amount") or 0)
        note    = request.form.get("note")
        tx_date = request.form.get("tx_date")

        ok, msg, data = apply_transaction(entity_type, entity_id, "payment", amount, note, tx_date)
        if not ok:
            flash(f"Error: {msg}", "danger")
        else:
           recompute_cash_in_hand_from_date(
                datetime.strptime(tx_date, "%Y-%m-%d").date()
            )
        
           flash(
                f"Receipt saved. Old balance: {data['old_balance']:.2f}, New balance: {data['new_balance']:.2f}",
                "success"
            )
        cursor.close(); conn.close()
        return redirect(url_for("payment_page"))

    cursor.execute("SELECT customer_id AS id, name FROM customers ORDER BY name ASC")
    customers = cursor.fetchall()
    cursor.execute("SELECT supplier_id AS id, name FROM suppliers ORDER BY name ASC")
    suppliers = cursor.fetchall()
    cursor.close(); conn.close()
    return render_template("payment.html", customers=customers, suppliers=suppliers)


# ---------------------------------------------------------------------------
# Cash-in-hand dashboard
# ---------------------------------------------------------------------------

@app.route("/cash_in_hand")

def cash_in_hand():
    today          = ist_today()
    today_start    = datetime.combine(today, time.min)
    tomorrow_start = today_start + timedelta(days=1)
    yesterday      = today - timedelta(days=1)

    conn   = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    cursor.execute(
        """
        SELECT t.amount, COALESCE(c.name, s.name) AS name
        FROM transactions t
        LEFT JOIN customers c ON (t.entity_type='customer' AND t.entity_id=c.customer_id)
        LEFT JOIN suppliers s ON (t.entity_type='supplier' AND t.entity_id=s.supplier_id)
        WHERE t.tx_type='receipt' AND t.tx_date >= %s AND t.tx_date < %s
        ORDER BY t.tx_id DESC
        """,
        (today_start, tomorrow_start)
    )
    todays_receipts = cursor.fetchall()

    cursor.execute(
        """
        SELECT t.amount, COALESCE(c.name, s.name) AS name
        FROM transactions t
        LEFT JOIN customers c ON (t.entity_type='customer' AND t.entity_id=c.customer_id)
        LEFT JOIN suppliers s ON (t.entity_type='supplier' AND t.entity_id=s.supplier_id)
        WHERE t.tx_type='payment' AND t.tx_date >= %s AND t.tx_date < %s
        ORDER BY t.tx_id DESC
        """,
        (today_start, tomorrow_start)
    )
    todays_payments = cursor.fetchall()

    cursor.execute(
        "SELECT s.name, b.transport, b.paid FROM supplier_bills b "
        "JOIN suppliers s ON b.supplier_id = s.supplier_id WHERE b.bill_date = %s",
        (today,)
    )
    supplier_extra = []
    for row in cursor.fetchall():
        tr = row.get("transport") or 0
        pd = row.get("paid")      or 0
        if float(tr) != 0:
            supplier_extra.append({"name": f"{row['name']} (transport)", "amount": float(tr)})
        if float(pd) != 0:
            supplier_extra.append({"name": f"{row['name']} (paid)",      "amount": float(pd)})

    cursor.execute("SELECT amount, note FROM labour_entries WHERE entry_date = %s", (today,))
    labour_entries = [
        {"name": "Labour" + (f" - {lr['note']}" if lr["note"] else ""), "amount": float(lr["amount"] or 0)}
        for lr in cursor.fetchall()
    ]

    all_payments   = todays_payments + supplier_extra + labour_entries
    total_receipts = sum(float(r.get("amount") or 0) for r in todays_receipts)
    total_payments = sum(float(p.get("amount") or 0) for p in all_payments)

    cursor.execute(
        """
        SELECT closing
        FROM cash_in_hand
        WHERE cdate < %s
        ORDER BY cdate DESC
        LIMIT 1
        """,
        (today,))

    row = cursor.fetchone()
    
    opening = float(row["closing"]) if row and row.get("closing") is not None else 0.0
    closing = opening + total_receipts - total_payments

    cursor.execute(
        """
        INSERT INTO cash_in_hand (cdate, opening, total_receipts, total_payments, closing)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (cdate) DO UPDATE SET
            opening=%s, total_receipts=%s, total_payments=%s, closing=%s
        """,
        (today, opening, total_receipts, total_payments, closing,
         opening, total_receipts, total_payments, closing)
    )
    conn.commit()
    cursor.close(); conn.close()

    print("TODAY =", today)
    print("ROW =", row)
    print("OPENING =", opening)

    return render_template(
        "cash_in_hand.html",
        todays_receipts=todays_receipts,
        all_payments=all_payments,
        total_receipts=total_receipts,
        total_payments=total_payments,
        opening=opening,
        closing=closing
    )


# ---------------------------------------------------------------------------
# Labour entries
# ---------------------------------------------------------------------------

@app.route("/labour/add", methods=["GET", "POST"])

def add_labour():
    today = ist_today()

    if request.method == "POST":
        amount = float(request.form.get("amount") or 0)
        note   = request.form.get("note") or ""

        conn   = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO labour_entries (entry_date, amount, note) VALUES (%s, %s, %s)",
            (today, amount, note)
        )
        conn.commit()
        cursor.close(); conn.close()

        try:
            recompute_cash_in_hand_for_date(today)
        except Exception:
            pass

        flash("Labour entry saved for today", "success")
        return redirect(url_for("add_labour"))

    return render_template("labour_add.html", today=today.isoformat())

@app.route("/labour/history")
def labour_history():
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    cursor.execute("""
        SELECT
            labour_id,
            entry_date,
            amount,
            note
        FROM labour_entries
        ORDER BY entry_date DESC, labour_id DESC
        LIMIT 20
    """)

    entries = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template(
        "labour_history.html",
        entries=entries,
        today=ist_today()
    )

@app.route("/labour/edit/<int:labour_id>", methods=["GET", "POST"])
def edit_labour(labour_id):

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    cursor.execute("""
        SELECT *
        FROM labour_entries
        WHERE labour_id = %s
    """, (labour_id,))

    labour = cursor.fetchone()

    if not labour:
        flash("Entry not found.", "danger")
        cursor.close()
        conn.close()
        return redirect(url_for("labour_history"))

    if labour["entry_date"] != ist_today():
        flash("Only today's labour entries can be edited.", "danger")
        cursor.close()
        conn.close()
        return redirect(url_for("labour_history"))

    if request.method == "POST":

        try:
            amount = float(request.form.get("amount") or 0)
        except ValueError:
            flash("Amount must be a valid number.", "danger")
            return redirect(url_for("edit_labour", labour_id=labour_id))
        
        if amount < 0:
            flash("Amount cannot be negative.", "danger")
            return redirect(url_for("edit_labour", labour_id=labour_id))
        
        note = request.form.get("note", "").strip()

        cursor.execute("""
            UPDATE labour_entries
            SET amount=%s,
                note=%s
            WHERE labour_id=%s
        """, (amount, note, labour_id))

        conn.commit()

        cursor.close()
        conn.close()

        recompute_cash_in_hand_for_date(ist_today())

        flash("Labour entry updated successfully.", "success")
        return redirect(url_for("labour_history"))

    cursor.close()
    conn.close()

    return render_template(
        "labour_edit.html",
        labour=labour
    )
# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

@app.route("/ledger")

def ledger():
    conn   = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT supplier_id, name, balance FROM suppliers WHERE balance <> 0 ORDER BY name")
    suppliers = cursor.fetchall()
    cursor.execute("SELECT customer_id, name, balance FROM customers WHERE balance <> 0 ORDER BY name")
    customers = cursor.fetchall()
    cursor.close(); conn.close()

    supplier_credit, supplier_debit       = [], []
    supplier_credit_total, supplier_debit_total = 0.0, 0.0
    for s in suppliers:
        bal = float(s["balance"] or 0.0)
        if bal > 0:
            supplier_credit.append({"id": s["supplier_id"], "name": s["name"], "amount": bal})
            supplier_credit_total += bal
        else:
            supplier_debit.append({"id": s["supplier_id"], "name": s["name"], "amount": abs(bal)})
            supplier_debit_total  += abs(bal)

    customer_credit, customer_debit       = [], []
    customer_credit_total, customer_debit_total = 0.0, 0.0
    for c in customers:
        bal = float(c["balance"] or 0.0)
        if bal > 0:
            customer_credit.append({"id": c["customer_id"], "name": c["name"], "amount": bal})
            customer_credit_total += bal
        else:
            customer_debit.append({"id": c["customer_id"], "name": c["name"], "amount": abs(bal)})
            customer_debit_total  += abs(bal)

    return render_template(
        "ledger.html",
        supplier_credit=supplier_credit, supplier_debit=supplier_debit,
        supplier_credit_total=supplier_credit_total, supplier_debit_total=supplier_debit_total,
        customer_credit=customer_credit, customer_debit=customer_debit,
        customer_credit_total=customer_credit_total, customer_debit_total=customer_debit_total
    )

@app.route("/ledger/print")

def ledger_print():

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    # Supplier Credit
    cursor.execute("""
        SELECT supplier_id AS id,
               name,
               balance AS amount
        FROM suppliers
        WHERE balance > 0
        ORDER BY name
    """)
    supplier_credit = cursor.fetchall()

    supplier_credit_total = sum(
        float(x["amount"]) for x in supplier_credit
    )

    # Supplier Debit
    cursor.execute("""
        SELECT supplier_id AS id,
               name,
               ABS(balance) AS amount
        FROM suppliers
        WHERE balance < 0
        ORDER BY name
    """)
    supplier_debit = cursor.fetchall()

    supplier_debit_total = sum(
        float(x["amount"]) for x in supplier_debit
    )

    # Customer Credit
    cursor.execute("""
        SELECT customer_id AS id,
               name,
               balance AS amount
        FROM customers
        WHERE balance > 0
        ORDER BY name
    """)
    customer_credit = cursor.fetchall()

    customer_credit_total = sum(
        float(x["amount"]) for x in customer_credit
    )

    # Customer Debit
    cursor.execute("""
        SELECT customer_id AS id,
               name,
               ABS(balance) AS amount
        FROM customers
        WHERE balance < 0
        ORDER BY name
    """)
    customer_debit = cursor.fetchall()

    customer_debit_total = sum(
        float(x["amount"]) for x in customer_debit
    )

    cursor.close()
    conn.close()

    return render_template(
        "ledger_print.html",

        supplier_credit=supplier_credit,
        supplier_credit_total=supplier_credit_total,

        supplier_debit=supplier_debit,
        supplier_debit_total=supplier_debit_total,

        customer_credit=customer_credit,
        customer_credit_total=customer_credit_total,

        customer_debit=customer_debit,
        customer_debit_total=customer_debit_total
    )
# ---------------------------------------------------------------------------
# Find (search customers / suppliers)
# ---------------------------------------------------------------------------
@app.route("/find", methods=["GET"])
def find_page():

    q = request.args.get("q", "").strip()
    mode = request.args.get("mode", "supplier")

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    if mode == "supplier":

        if q:
            cursor.execute("""
                SELECT
                    supplier_id AS id,
                    name,
                    balance
                FROM suppliers
                WHERE
                    name ILIKE %s
                    OR CAST(supplier_id AS TEXT) ILIKE %s
                ORDER BY name ASC
            """, (f"%{q}%", f"%{q}%"))

        else:
            cursor.execute("""
                SELECT
                    supplier_id AS id,
                    name,
                    balance
                FROM suppliers
                ORDER BY name ASC
            """)

    else:

        if q:
            cursor.execute("""
                SELECT
                    customer_id AS id,
                    name,
                    balance
                FROM customers
                WHERE
                    name ILIKE %s
                    OR CAST(customer_id AS TEXT) ILIKE %s
                ORDER BY name ASC
            """, (f"%{q}%", f"%{q}%"))

        else:
            cursor.execute("""
                SELECT
                    customer_id AS id,
                    name,
                    balance
                FROM customers
                ORDER BY name ASC
            """)

    results = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template(
        "find.html",
        mode=mode,
        q=q,
        results=results
    )
# ---------------------------------------------------------------------------
# Supplier Edit
# ---------------------------------------------------------------------------

@app.route("/supplier/edit/<int:supplier_id>", methods=["GET", "POST"])

def edit_supplier(supplier_id):

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    if request.method == "POST":

        name = request.form.get("name", "").strip().title()
        phone = request.form.get("phone", "").strip()

        cursor.execute("""
            UPDATE suppliers
            SET name = %s,
                phone = %s
            WHERE supplier_id = %s
        """, (name, phone, supplier_id))

        conn.commit()

        cursor.close()
        conn.close()

        flash("Supplier updated successfully!", "success")
        return redirect(url_for("find_page", mode="supplier"))

    cursor.execute("""
        SELECT supplier_id, name, phone, balance
        FROM suppliers
        WHERE supplier_id = %s
    """, (supplier_id,))

    supplier = cursor.fetchone()

    cursor.close()
    conn.close()

    return render_template(
        "supplier_edit_simple.html",
        supplier=supplier
    )

@app.route("/customer/edit/<int:customer_id>", methods=["GET", "POST"])

def edit_customer(customer_id):

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    if request.method == "POST":

        name = request.form.get("name", "").strip().title()

        phone = request.form.get("phone", "")
        phone = re.sub(r"\D", "", phone)

        cursor.execute("""
            UPDATE customers
            SET name = %s,
                phone = %s
            WHERE customer_id = %s
        """, (name, phone, customer_id))

        conn.commit()

        cursor.close()
        conn.close()

        flash("Customer updated successfully!", "success")
        return redirect(url_for("find_page", mode="customer"))

    cursor.execute("""
        SELECT customer_id, name, phone, balance
        FROM customers
        WHERE customer_id = %s
    """, (customer_id,))

    customer = cursor.fetchone()

    cursor.close()
    conn.close()

    return render_template(
        "customer_edit_simple.html",
        customer=customer
    )
    
# ---------------------------------------------------------------------------
# Account adjustment
# ---------------------------------------------------------------------------

@app.route("/accounts/adjust", methods=["GET", "POST"])

def adjust_account():
    conn   = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    if request.method == "POST":
        entity_type = request.form.get("entity_type")
        entity_id   = int(request.form.get("entity_id") or 0)

        try:
            amount = float(request.form.get("amount") or 0)
        except ValueError:
            amount = -1

        if amount < 0:
            flash("Amount must be zero or positive.", "danger")
            cursor.close(); conn.close()
            return redirect(url_for("adjust_account"))

        if entity_type == "supplier":
            cursor.execute("SELECT supplier_id, name, balance FROM suppliers WHERE supplier_id = %s", (entity_id,))
            row = cursor.fetchone()
            if not row:
                flash("Supplier not found.", "danger")
                cursor.close(); conn.close()
                return redirect(url_for("adjust_account"))
            cursor.execute("UPDATE suppliers SET balance = balance + %s WHERE supplier_id = %s", (amount, entity_id))

        elif entity_type == "customer":
            cursor.execute("SELECT customer_id, name, balance FROM customers WHERE customer_id = %s", (entity_id,))
            row = cursor.fetchone()
            if not row:
                flash("Customer not found.", "danger")
                cursor.close(); conn.close()
                return redirect(url_for("adjust_account"))
            cursor.execute("UPDATE customers SET balance = balance + %s WHERE customer_id = %s", (amount, entity_id))

        else:
            flash("Invalid entity type.", "danger")
            cursor.close(); conn.close()
            return redirect(url_for("adjust_account"))
        cursor.execute("""
            INSERT INTO account_adjustments
            (entity_type, entity_id, amount, adjustment_date)
            VALUES (%s, %s, %s, CURRENT_DATE)
        """, (
            entity_type,
            entity_id,
            amount
        ))

        if entity_type == "customer":
            resync_customer_ledger(cursor, [entity_id])

        conn.commit()
        cursor.close(); conn.close()
        flash(f"Account updated for {entity_type} #{entity_id} (₹{amount:.2f}).", "success")
        return redirect(url_for("adjust_account"))

    cursor.execute("SELECT supplier_id AS id, name, balance FROM suppliers ORDER BY name ASC")
    suppliers = cursor.fetchall()
    cursor.execute("SELECT customer_id AS id, name, balance FROM customers ORDER BY name ASC")
    customers = cursor.fetchall()
    cursor.close(); conn.close()

    return render_template("adjust_account.html", suppliers=suppliers, customers=customers)

@app.route("/account-adjustment-history")

def account_adjustment_history():

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    selected_date = request.args.get("date")
    from_date = request.args.get("from_date")
    to_date = request.args.get("to_date")

    query = """
        SELECT
            adjustment_id,
            adjustment_date,
            entity_type,
            entity_id,
            amount,
            created_at
        FROM account_adjustments
    """
    conditions = []
    params = []

    if selected_date:
        conditions.append("adjustment_date = %s")
        params.append(selected_date)
    elif from_date and to_date:
        conditions.append("adjustment_date BETWEEN %s AND %s")
        params.extend([from_date, to_date])
    elif from_date:
        conditions.append("adjustment_date >= %s")
        params.append(from_date)
    elif to_date:
        conditions.append("adjustment_date <= %s")
        params.append(to_date)

    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY created_at"

    cursor.execute(query, tuple(params))

    rows = cursor.fetchall()

    for row in rows:
        if row["entity_type"] == "supplier":
            cursor.execute(
                "SELECT name FROM suppliers WHERE supplier_id=%s",
                (row["entity_id"],)
            )
        else:
            cursor.execute(
                "SELECT name FROM customers WHERE customer_id=%s",
                (row["entity_id"],)
            )

        x = cursor.fetchone()
        row["name"] = x["name"] if x else "Unknown"

    cursor.close()
    conn.close()

    return render_template(
        "account_adjustment_history.html",
        adjustments=rows,
        selected_date=selected_date,
        from_date=from_date,
        to_date=to_date
    )
# ---------------------------------------------------------------------------
# Account summary (customer statement / supplier statement)
#
# One page, one route, toggled by ?entity_type=customer|supplier. Pulls the
# customer's purchases (customer_bills) + receipts (transactions, tx_type=
# 'receipt'), or the supplier's bills (supplier_bills) + payments
# (transactions, tx_type='payment'), depending on the toggle.
# ---------------------------------------------------------------------------

@app.route("/account-summary", methods=["GET"])

def account_summary():
    conn   = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT customer_id AS id, name FROM customers ORDER BY name ASC")
    customers = cursor.fetchall()
    cursor.execute("SELECT supplier_id AS id, name FROM suppliers ORDER BY name ASC")
    suppliers = cursor.fetchall()

    entity_type = (request.args.get("entity_type") or "customer").strip().lower()
    if entity_type not in ("customer", "supplier"):
        entity_type = "customer"

    raw_id   = (request.args.get("entity_id") or "").strip()
    from_str = request.args.get("from_date")
    to_str   = request.args.get("to_date")

    today = ist_today()
    try:
        from_date = datetime.strptime(from_str, "%Y-%m-%d").date() if from_str else today
    except Exception:
        from_date = today
    try:
        to_date = datetime.strptime(to_str, "%Y-%m-%d").date() if to_str else today
    except Exception:
        to_date = today

    if from_date > to_date:
        from_date, to_date = to_date, from_date

    primary_rows    = []   # purchases (customer) or bills (supplier)
    secondary_rows  = []   # receipts (customer) or payments (supplier)
    sum_primary     = 0.0
    sum_secondary   = 0.0
    current_balance = None
    entity_id       = None
    entity_name     = None

    table  = "customers" if entity_type == "customer" else "suppliers"
    id_col = "customer_id" if entity_type == "customer" else "supplier_id"

    if raw_id:
        # 1) direct integer
        try:
            entity_id = int(raw_id)
        except (ValueError, TypeError):
            entity_id = None

        # 2) extract first digits
        if entity_id is None:
            m = re.search(r'\b(\d+)\b', raw_id)
            if m:
                try:
                    entity_id = int(m.group(1))
                except Exception:
                    entity_id = None

        # 3) name search (Postgres: ILIKE)
        if entity_id is None:
            cursor.execute(
                f"SELECT {id_col}, name, balance FROM {table} WHERE name ILIKE %s ORDER BY name LIMIT 1",
                (f"%{raw_id}%",)
            )
            er = cursor.fetchone()
            if er:
                entity_id   = er[id_col]
                entity_name = er["name"]

        # 4) canonical fetch
        if entity_id and not entity_name:
            cursor.execute(f"SELECT {id_col}, name, balance FROM {table} WHERE {id_col} = %s", (entity_id,))
            er = cursor.fetchone()
            if er:
                entity_name = er["name"]
            else:
                entity_id = None
                flash(f"{entity_type.capitalize()} not found. Please select a valid {entity_type}.", "warning")

    if entity_id:
        if entity_type == "customer":
            cursor.execute(
                """
                SELECT bill_id,
                       bill_date,
                       quantity,
                       rate,
                       amount,
                       supplier_bill_id
                FROM customer_bills
                WHERE customer_id = %s
                AND bill_date BETWEEN %s AND %s
                ORDER BY bill_date ASC
                """,
                (entity_id, from_date, to_date)
            )
            for r in cursor.fetchall():
                amt = float(r.get("amount") or 0.0)
                sb_id = r.get("supplier_bill_id")
                primary_rows.append({
                    "id":        r.get("bill_id"),
                    "date":      r.get("bill_date"),
                    "amount":    amt,
                    "ref_extra": f" — SB#{sb_id}" if sb_id else ""
                })
                sum_primary += amt

            # NOTE: wrapping tx_date in DATE(...) previously forced a full table
            # scan. A half-open range on the raw column lets the index be used.
            cursor.execute(
                "SELECT tx_id, tx_date, amount, note, tx_type FROM transactions "
                "WHERE entity_type='customer' AND entity_id=%s AND tx_type='receipt' "
                "AND tx_date >= %s AND tx_date < %s ORDER BY tx_date ASC",
                (entity_id, from_date, to_date + timedelta(days=1))
            )
            for r in cursor.fetchall():
                amt = float(r.get("amount") or 0.0)
                secondary_rows.append({
                    "id":     r.get("tx_id"),
                    "date":   r.get("tx_date"),
                    "amount": amt,
                    "note":   r.get("note")
                })
                sum_secondary += amt

            cursor.execute("SELECT balance FROM customers WHERE customer_id = %s", (entity_id,))
            crow            = cursor.fetchone()
            current_balance = float(crow.get("balance")) if crow and crow.get("balance") is not None else 0.0

        else:  # supplier
            cursor.execute(
                """
                SELECT bill_id,
                       bill_date,
                       total_amount,
                       commission,
                       transport,
                       labour,
                       paid,
                       balance
                FROM supplier_bills
                WHERE supplier_id = %s
                AND bill_date BETWEEN %s AND %s
                ORDER BY bill_date ASC
                """,
                (entity_id, from_date, to_date)
            )
            for r in cursor.fetchall():
                amt = float(r.get("balance") or 0.0)
                primary_rows.append({
                    "id":        r.get("bill_id"),
                    "date":      r.get("bill_date"),
                    "amount":    amt,
                    "ref_extra": ""
                })
                sum_primary += amt

            cursor.execute(
                "SELECT tx_id, tx_date, amount, note, tx_type FROM transactions "
                "WHERE entity_type='supplier' AND entity_id=%s AND tx_type='payment' "
                "AND tx_date >= %s AND tx_date < %s ORDER BY tx_date ASC",
                (entity_id, from_date, to_date + timedelta(days=1))
            )
            for r in cursor.fetchall():
                amt = float(r.get("amount") or 0.0)
                secondary_rows.append({
                    "id":     r.get("tx_id"),
                    "date":   r.get("tx_date"),
                    "amount": amt,
                    "note":   r.get("note")
                })
                sum_secondary += amt

            cursor.execute("SELECT balance FROM suppliers WHERE supplier_id = %s", (entity_id,))
            srow            = cursor.fetchone()
            current_balance = float(srow.get("balance")) if srow and srow.get("balance") is not None else 0.0

    cursor.close(); conn.close()

    net            = sum_secondary - sum_primary
    selected_entity = entity_id if entity_id is not None else raw_id

    return render_template(
        "account_summary.html",
        customers=customers,
        suppliers=suppliers,
        entity_type=entity_type,
        selected_entity=selected_entity,
        from_date=from_date,
        to_date=to_date,
        primary_rows=primary_rows,
        secondary_rows=secondary_rows,
        sum_primary=sum_primary,
        sum_secondary=sum_secondary,
        net=net,
        current_balance=current_balance
    )


# ---------------------------------------------------------------------------
# Tally
# ---------------------------------------------------------------------------

@app.route("/tally", methods=["GET", "POST"])

def tally():
    supplier_data, customer_data           = [], []
    total_supplier, total_customer, difference = 0, 0, 0

    if request.method == "POST":
        from_date = request.form.get("from_date")
        to_date   = request.form.get("to_date")

        if not from_date or not to_date:
            flash("Please select both FROM and TO dates.", "danger")
            today = date.today().isoformat()
            return render_template("tally.html", from_date=today, to_date=today)
    else:
        # First load (GET) — default to today's tally
        from_date = to_date = date.today().isoformat()

    from_dt = datetime.strptime(from_date, "%Y-%m-%d")
    to_dt   = datetime.strptime(to_date,   "%Y-%m-%d") + timedelta(days=1)

    conn = get_connection()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT
                sb.supplier_id,
                s.name AS supplier_name,
                sb.total_amount AS amount
            FROM supplier_bills sb
            LEFT JOIN suppliers s
                ON sb.supplier_id = s.supplier_id
            WHERE sb.bill_date >= %s
              AND sb.bill_date < %s
        """, (from_dt, to_dt))
        supplier_data = cursor.fetchall()

        cursor.execute("""
            SELECT
                cb.customer_id,
                c.name AS customer_name,
                cb.amount
            FROM customer_bills cb
            LEFT JOIN customers c
                ON cb.customer_id = c.customer_id
            WHERE cb.bill_date >= %s
              AND cb.bill_date < %s
        """, (from_dt, to_dt))
        customer_data = cursor.fetchall()
        cursor.close()
    finally:
        conn.close()

    total_supplier = sum(x["amount"] for x in supplier_data)
    total_customer = sum(x["amount"] for x in customer_data)
    difference     = total_customer - total_supplier

    return render_template(
        "tally.html",
        supplierData=supplier_data, customerData=customer_data,
        totalSupplier=total_supplier, totalCustomer=total_customer, difference=difference,
        from_date=from_date, to_date=to_date
    )


# ---------------------------------------------------------------------------
# Profit & Loss
# ---------------------------------------------------------------------------

@app.route("/profit-loss", methods=["GET"])

def profit_loss():
    from_str = request.args.get("from_date")
    to_str   = request.args.get("to_date")
    today    = ist_today()

    try:
        from_date = datetime.strptime(from_str, "%Y-%m-%d").date() if from_str else today
    except Exception:
        from_date = today
    try:
        to_date = datetime.strptime(to_str, "%Y-%m-%d").date() if to_str else today
    except Exception:
        to_date = today

    if from_date > to_date:
        from_date, to_date = to_date, from_date

    conn   = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    cursor.execute(
        "SELECT COALESCE(SUM(commission),0) AS total_comm FROM supplier_bills WHERE bill_date BETWEEN %s AND %s",
        (from_date, to_date)
    )
    row              = cursor.fetchone()
    total_commission = float(row["total_comm"]) if row and row.get("total_comm") is not None else 0.0

    cursor.execute(
        "SELECT COALESCE(SUM(amount),0) AS total_labour FROM labour_entries WHERE entry_date BETWEEN %s AND %s",
        (from_date, to_date)
    )
    row           = cursor.fetchone()
    total_labour  = float(row["total_labour"]) if row and row.get("total_labour") is not None else 0.0

    cursor.execute(
        "SELECT COALESCE(SUM(labour),0) AS supplier_labour FROM supplier_bills WHERE bill_date BETWEEN %s AND %s",
        (from_date, to_date)
    )
    row2             = cursor.fetchone()
    supplier_labour  = float(row2["supplier_labour"]) if row2 and row2.get("supplier_labour") is not None else 0.0

    profit = total_commission + supplier_labour
    net    = profit - total_labour

    cursor.close(); conn.close()
    return render_template(
        "profit_loss.html",
        from_date=from_date, to_date=to_date,
        total_commission=total_commission, total_labour=total_labour,
        supplier_labour=supplier_labour, profit=profit, net=net
    )


# ---------------------------------------------------------------------------
# Transactions — Edit page
# ---------------------------------------------------------------------------

@app.route("/transactions/edit", methods=["GET"])

def transactions_edit_page():
    conn   = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT customer_id AS id, name FROM customers ORDER BY name")
    customers = cursor.fetchall()
    cursor.execute("SELECT supplier_id AS id, name FROM suppliers ORDER BY name")
    suppliers = cursor.fetchall()
    cursor.close(); conn.close()
    return render_template("edit_transaction.html", customers=customers, suppliers=suppliers)


# ---------------------------------------------------------------------------
# Transactions — List API
# ---------------------------------------------------------------------------

@app.route("/transactions/list", methods=["GET"])

def transactions_list():
    entity_type = request.args.get("entity_type")
    entity_id   = request.args.get("entity_id")
    tx_type     = request.args.get("tx_type")

    if not entity_type or not entity_id or not tx_type:
        return jsonify({"error": "missing_parameters"}), 400

    try:
        entity_id = int(entity_id)
    except ValueError:
        return jsonify({"error": "invalid_entity_id"}), 400

    try:
        ist       = get_zone("Asia/Kolkata")
        today_ist = datetime.now(ist).date()
        start_ist = datetime.combine(today_ist, time.min).replace(tzinfo=ist)
        end_ist   = datetime.combine(today_ist, time.max).replace(tzinfo=ist)

        start_utc = start_ist.astimezone(timezone.utc)
        end_utc   = end_ist.astimezone(timezone.utc)

        conn   = get_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """
            SELECT tx_id, tx_type, entity_type, entity_id, amount, note, tx_date
            FROM transactions
            WHERE entity_type = %s
              AND entity_id = %s
              AND tx_type = %s
            ORDER BY tx_date DESC, tx_id DESC
            LIMIT 500
            """,
            (entity_type, entity_id, tx_type)
        )
        rows = cursor.fetchall()
        cursor.close(); conn.close()

        for r in rows:
            dt = r.get("tx_date")
            if isinstance(dt, datetime):
                r["tx_date"] = (dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None
                                else dt.astimezone(timezone.utc)).isoformat()
            elif isinstance(dt, date):
                r["tx_date"] = datetime.combine(dt, time.min).isoformat()
            else:
                r["tx_date"] = None
            r["amount"] = float(r.get("amount") or 0.0)

        return jsonify({"transactions": rows})

    except Exception as exc:
        print("ERROR in /transactions/list:", traceback.format_exc(), flush=True)
        return jsonify({"error": "internal_server_error", "message": str(exc)}), 500


# ---------------------------------------------------------------------------
# Transactions — Edit single
# ---------------------------------------------------------------------------

@app.route("/transactions/edit/<int:tx_id>", methods=["GET", "POST"])

def transactions_edit_one(tx_id):
    conn   = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    cursor.execute("SELECT * FROM transactions WHERE tx_id = %s", (tx_id,))
    tx = cursor.fetchone()
    if not tx:
        cursor.close(); conn.close()
        return jsonify({"error": "tx_not_found"}), 404

    if request.method == "GET":
        out = dict(tx)
        try:
            if isinstance(out.get("tx_date"), datetime):
                out["tx_date"] = out["tx_date"].isoformat()
        except Exception:
            pass
        out["amount"] = float(out.get("amount") or 0.0)
        cursor.close(); conn.close()
        return jsonify({"tx": out})

    # POST → update
    try:
        data       = request.get_json() or {}
        new_amount = float(data.get("amount") or 0.0)
        new_date   = data.get("tx_date")
        new_note   = data.get("note") or None

        old_amount  = float(tx.get("amount") or 0.0)
        entity_type = tx.get("entity_type")
        entity_id   = int(tx.get("entity_id"))
        tx_type     = tx.get("tx_type")

        delta_amount = new_amount - old_amount

        if entity_type == "customer":
            balance_delta = -delta_amount if tx_type == "receipt" else delta_amount
            cursor.execute(
                "UPDATE customers SET balance = balance + %s WHERE customer_id = %s",
                (balance_delta, entity_id)
            )
        elif entity_type == "supplier":
            balance_delta = -delta_amount if tx_type == "receipt" else delta_amount
            cursor.execute(
                "UPDATE suppliers SET balance = balance + %s WHERE supplier_id = %s",
                (balance_delta, entity_id)
            )
        else:
            cursor.close(); conn.close()
            return jsonify({"error": "invalid_entity_type"}), 400

        parsed_for_db = None
        if new_date:
            try:
                ist = get_zone("Asia/Kolkata")
                if isinstance(new_date, str) and len(new_date) == 10 and new_date.count('-') == 2:
                    orig_dt = tx.get("tx_date")
                    tpart   = orig_dt.time() if isinstance(orig_dt, datetime) else time(12, 0, 0)
                    dt_ist  = datetime.strptime(new_date, "%Y-%m-%d").replace(
                        hour=tpart.hour, minute=tpart.minute, second=tpart.second, tzinfo=ist
                    )
                else:
                    dt = datetime.fromisoformat(new_date)
                    dt_ist = dt.replace(tzinfo=ist) if dt.tzinfo is None else dt.astimezone(ist)
                parsed_for_db = dt_ist.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                parsed_for_db = None

        if parsed_for_db:
            cursor.execute(
                "UPDATE transactions SET amount=%s, note=%s, tx_date=%s WHERE tx_id=%s",
                (new_amount, new_note, parsed_for_db, tx_id)
            )
        else:
            cursor.execute(
                "UPDATE transactions SET amount=%s, note=%s WHERE tx_id=%s",
                (new_amount, new_note, tx_id)
            )

        conn.commit()
        cursor.close(); conn.close()
        return jsonify({"status": "ok", "balance_delta": balance_delta})

    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        print("ERROR in transactions_edit_one:", traceback.format_exc(), flush=True)
        cursor.close(); conn.close()
        return jsonify({"error": "exception", "message": str(e)}), 500


# ---------------------------------------------------------------------------
# Debug helper (disable in production by removing the route)
# ---------------------------------------------------------------------------

@app.route("/debug-routes")

def debug_routes():
    return "<br>".join(sorted([r.endpoint for r in app.url_map.iter_rules()]))

@app.route("/webhook/whatsapp", methods=["GET"])
def verify_whatsapp_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == os.getenv("WHATSAPP_VERIFY_TOKEN"):
        return challenge, 200
    return "Verification failed", 403


@app.route("/webhook/whatsapp", methods=["POST"])
def receive_whatsapp_webhook():
    data = request.get_json(force=True, silent=True) or {}
    print("WEBHOOK EVENT:", data)

    try:
        entry = data["entry"][0]
        change = entry["changes"][0]["value"]

        if "statuses" in change:
            for status_update in change["statuses"]:
                wamid = status_update.get("id")
                status = status_update.get("status")
                errors = status_update.get("errors")
                error_msg = str(errors) if errors else None

                update_delivery_status(wamid, status.upper(), error_message=error_msg)
    except Exception as e:
        print("Webhook processing error:", e)

    return jsonify({"status": "received"}), 200


if __name__ == "__main__":
    app.run(debug=True, use_reloader=False)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(debug=True, use_reloader=False)
