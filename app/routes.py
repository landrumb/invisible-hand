import base64
import io
from datetime import datetime

import qrcode
from flask import (
    Blueprint,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from . import db
from .models import (
    FutureHolding,
    FutureListing,
    OptionHolding,
    OptionListing,
    OptionType,
    PriceHistory,
    PrisonersMatch,
    Product,
    QueueEntry,
    Role,
    Security,
    SecurityHolding,
    SecurityPriceHistory,
    Transaction,
    User,
    AppSetting,
)
from .securities import (
    execute_equity_trade,
    execute_future_trade,
    execute_option_trade,
    get_simulator,
)


bp = Blueprint("main", __name__)


def record_transaction(user, amount, description, counterparty=None, type_="game", commit=True):
    user.balance += amount
    txn = Transaction(
        user=user,
        amount=amount,
        description=description,
        counterparty=counterparty,
        type=type_,
    )
    db.session.add(txn)
    # If this is a positive game reward (player earned credits), reduce future game rewards via per-game multiplier
    if type_ == "game" and amount > 0:
        try:
            game_key = AppSetting.get("current_game_context", None)
            if game_key:
                dec_pct = float(AppSetting.get(f"game:{game_key}:decrease_pct", AppSetting.get("game_reward_decrease_pct", "5.0") or "5.0"))
                current_mult = float(AppSetting.get(f"game:{game_key}:multiplier", "1.0") or "1.0")
                new_mult = max(0.0, current_mult * (1.0 - dec_pct / 100.0))
                AppSetting.set(f"game:{game_key}:multiplier", f"{new_mult}")
        except Exception:
            pass
    if commit:
        db.session.commit()
    return txn


@bp.route("/")
def index():
    return render_template("index.html")


@bp.route("/dashboard")
@login_required
def dashboard():
    latest_transactions = (
        Transaction.query.filter_by(user_id=current_user.id)
        .order_by(Transaction.created_at.desc())
        .limit(10)
        .all()
    )
    active_match = (
        PrisonersMatch.query.filter(
            PrisonersMatch.status != "completed",
            ((PrisonersMatch.player1_id == current_user.id) | (PrisonersMatch.player2_id == current_user.id)),
        )
        .order_by(PrisonersMatch.created_at.desc())
        .first()
    )
    return render_template(
        "dashboard.html",
        balance=current_user.balance,
        transactions=latest_transactions,
        active_match=active_match,
    )


@bp.route("/securities")
@login_required
def securities_hub():
    simulator = get_simulator()
    securities = Security.query.order_by(Security.symbol.asc()).all()
    security_positions = {
        holding.security_symbol: holding
        for holding in SecurityHolding.query.filter_by(user_id=current_user.id).all()
    }
    option_positions = {
        holding.listing_id: holding
        for holding in OptionHolding.query.filter_by(user_id=current_user.id).all()
    }
    future_positions = {
        holding.listing_id: holding
        for holding in FutureHolding.query.filter_by(user_id=current_user.id).all()
    }

    now = datetime.utcnow()
    active_options = (
        OptionListing.query.filter(OptionListing.expiration > datetime.utcnow())
        .order_by(OptionListing.security_symbol.asc(), OptionListing.expiration.asc())
        .all()
    )
    option_quotes = []
    for listing in active_options:
        seconds_left = max(0.0, (listing.expiration - now).total_seconds())
        minutes_left = int((seconds_left + 59) // 60)
        option_quotes.append(
            {
                "listing": listing,
                "premium": simulator.price_option(listing),
                "holding": option_positions.get(listing.id),
                "minutes_left": minutes_left,
                "expiration_str": listing.expiration.strftime('%Y-%m-%d %H:%M:%S UTC'),
            }
        )

    active_futures = (
        FutureListing.query.filter(FutureListing.delivery_date > datetime.utcnow())
        .order_by(FutureListing.security_symbol.asc(), FutureListing.delivery_date.asc())
        .all()
    )
    future_quotes = []
    for listing in active_futures:
        seconds_left = max(0.0, (listing.delivery_date - now).total_seconds())
        minutes_left = int((seconds_left + 59) // 60)
        future_quotes.append(
            {
                "listing": listing,
                "forward": simulator.price_future(listing),
                "holding": future_positions.get(listing.id),
                "minutes_left": minutes_left,
                "delivery_str": listing.delivery_date.strftime('%Y-%m-%d %H:%M:%S UTC'),
            }
        )

    return render_template(
        "securities.html",
        securities=securities,
        security_positions=security_positions,
        option_quotes=option_quotes,
        future_quotes=future_quotes,
        update_interval=simulator.interval,
        risk_free_rate=simulator.risk_free_rate,
    )


@bp.route("/api/securities")
@login_required
def securities_snapshot():
    securities = Security.query.order_by(Security.symbol.asc()).all()
    payload = []
    for security in securities:
        latest_history = (
            SecurityPriceHistory.query.filter_by(security_symbol=security.symbol)
            .order_by(SecurityPriceHistory.timestamp.desc())
            .limit(2)
            .all()
        )
        change = 0.0
        if len(latest_history) >= 2:
            change = latest_history[0].price - latest_history[1].price
        payload.append(
            {
                "symbol": security.symbol,
                "name": security.name,
                "price": security.last_price,
                "updated_at": security.updated_at.isoformat(),
                "description": security.description,
                "change": change,
            }
        )
    return jsonify(payload)


@bp.route("/securities/trade", methods=["POST"])
@login_required
def trade_security():
    symbol = request.form.get("symbol")
    side = request.form.get("side")
    quantity = request.form.get("quantity", type=float)
    if not symbol or not quantity or side not in {"buy", "sell"}:
        flash("Please choose a security and quantity.", "error")
        return redirect(url_for("main.securities_hub"))
    quantity = abs(quantity)
    signed_quantity = quantity if side == "buy" else -quantity
    try:
        result = execute_equity_trade(current_user, symbol, signed_quantity)
        amount = result.cash_delta
        record_transaction(
            current_user,
            amount,
            result.description,
            type_="securities",
            commit=False,
        )
        db.session.commit()
        flash(f"{result.description} at {result.price:.2f}.", "success")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
    return redirect(url_for("main.securities_hub"))


@bp.route("/securities/options/trade", methods=["POST"])
@login_required
def trade_option():
    listing_id = request.form.get("listing_id", type=int)
    side = request.form.get("side")
    quantity = request.form.get("quantity", type=int)
    if not listing_id or not quantity or side not in {"buy", "sell"}:
        flash("Select an option and quantity.", "error")
        return redirect(url_for("main.securities_hub"))
    quantity = abs(quantity)
    signed_quantity = quantity if side == "buy" else -quantity
    try:
        result = execute_option_trade(current_user, listing_id, signed_quantity)
        amount = result.cash_delta
        record_transaction(
            current_user,
            amount,
            result.description,
            type_="options",
            commit=False,
        )
        db.session.commit()
        flash(f"{result.description} for {result.price:.2f} premium.", "success")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
    return redirect(url_for("main.securities_hub"))


@bp.route("/securities/futures/trade", methods=["POST"])
@login_required
def trade_future():
    listing_id = request.form.get("listing_id", type=int)
    side = request.form.get("side")
    quantity = request.form.get("quantity", type=int)
    if not listing_id or not quantity or side not in {"long", "short"}:
        flash("Select a future and quantity.", "error")
        return redirect(url_for("main.securities_hub"))
    quantity = abs(quantity)
    signed_quantity = quantity if side == "long" else -quantity
    try:
        result = execute_future_trade(current_user, listing_id, signed_quantity)
        amount = result.cash_delta
        record_transaction(
            current_user,
            amount,
            result.description,
            type_="futures",
            commit=False,
        )
        db.session.commit()
        if result.cash_delta < 0:
            message = f"{result.description}. Margin posted {abs(result.cash_delta):.2f}."
        elif result.cash_delta > 0:
            message = f"{result.description}. Margin released {result.cash_delta:.2f}."
        else:
            message = f"{result.description}."
        flash(message, "success")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
    return redirect(url_for("main.securities_hub"))


@bp.route("/single-player", methods=["GET", "POST"])
@login_required
def single_player():
    base_reward = 5.0
    # Use per-game multiplier keyed by 'single_player'
    AppSetting.set("current_game_context", "single_player")
    try:
        mult = float(AppSetting.get("game:single_player:multiplier", "1.0") or "1.0")
    except Exception:
        mult = 1.0
    reward = round(base_reward * mult, 2)
    if request.method == "POST":
        record_transaction(current_user, reward, "Won the solo clicker game")
        flash(f"You earned {reward:.2f} credits!", "success")
        return redirect(url_for("main.single_player"))
    return render_template("single_player.html", reward=reward)


@bp.route("/prisoners", methods=["GET", "POST"])
@login_required
def prisoners_dilemma():
    match = get_active_match_for_user(current_user)
    waiting_entry = QueueEntry.query.filter_by(user_id=current_user.id).first()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "join_queue":
            if match:
                flash("You are already in a match.", "warning")
            else:
                join_queue(current_user)
        elif action == "leave_queue":
            leave_queue(current_user)
        elif action in {"cooperate", "defect"}:
            if not match:
                flash("You are not currently in a match.", "warning")
            else:
                submit_choice(match, current_user, action)
        return redirect(url_for("main.prisoners_dilemma"))

    match = get_active_match_for_user(current_user)
    waiting_entry = QueueEntry.query.filter_by(user_id=current_user.id).first()
    return render_template(
        "prisoners_dilemma.html",
        match=match,
        waiting=waiting_entry is not None,
        opponent=get_opponent(match, current_user) if match else None,
    )


def get_active_match_for_user(user):
    return (
        PrisonersMatch.query.filter(
            PrisonersMatch.status != "completed",
            ((PrisonersMatch.player1_id == user.id) | (PrisonersMatch.player2_id == user.id)),
        )
        .order_by(PrisonersMatch.created_at.desc())
        .first()
    )


def get_opponent(match, user):
    if match is None:
        return None
    if match.player1_id == user.id:
        return match.player2
    return match.player1


def join_queue(user):
    waiting_entry = QueueEntry.query.order_by(QueueEntry.created_at.asc()).first()
    if waiting_entry and waiting_entry.user_id != user.id:
        match = PrisonersMatch(
            player1_id=waiting_entry.user_id,
            player2_id=user.id,
            status="in_progress",
        )
        db.session.add(match)
        db.session.delete(waiting_entry)
        db.session.commit()
        flash("You have been matched!", "success")
        return match
    elif waiting_entry and waiting_entry.user_id == user.id:
        flash("You are already waiting for an opponent.", "info")
    else:
        db.session.add(QueueEntry(user_id=user.id))
        db.session.commit()
        flash("Joined the queue. Waiting for an opponent.", "info")
    return None


def leave_queue(user):
    waiting_entry = QueueEntry.query.filter_by(user_id=user.id).first()
    if waiting_entry:
        db.session.delete(waiting_entry)
        db.session.commit()
        flash("You left the queue.", "info")
    else:
        flash("You are not currently in the queue.", "warning")


PAYOFFS = {
    ("cooperate", "cooperate"): (5, 5),
    ("cooperate", "defect"): (-10, 15),
    ("defect", "cooperate"): (15, -10),
    ("defect", "defect"): (-5, -5),
}


def submit_choice(match, user, choice):
    if match.status != "in_progress":
        flash("This match is no longer active.", "warning")
        return
    match.record_choice(user, choice)
    db.session.commit()
    if match.both_choices_made():
        resolve_match(match)


def resolve_match(match):
    payoff = PAYOFFS[(match.player1_choice, match.player2_choice)]
    # Use per-game multiplier keyed by 'prisoners'
    AppSetting.set("current_game_context", "prisoners")
    try:
        mult = float(AppSetting.get("game:prisoners:multiplier", "1.0") or "1.0")
    except Exception:
        mult = 1.0
    # Scale only positive rewards by multiplier; penalties remain unchanged
    p1_amount = payoff[0] * mult if payoff[0] > 0 else payoff[0]
    p2_amount = payoff[1] * mult if payoff[1] > 0 else payoff[1]
    player1 = match.player1
    player2 = match.player2
    record_transaction(player1, p1_amount, f"Prisoner's dilemma: {match.player1_choice}", counterparty=player2)
    record_transaction(player2, p2_amount, f"Prisoner's dilemma: {match.player2_choice}", counterparty=player1)
    match.status = "completed"
    match.resolved_at = datetime.utcnow()
    db.session.commit()
    flash(
        f"Match resolved! Results: {player1.name} chose {match.player1_choice}, {player2.name} chose {match.player2_choice}.",
        "info",
    )


@bp.route("/merchant", methods=["GET", "POST"])
@login_required
def merchant_portal():
    products = Product.query.order_by(Product.name.asc()).all()
    selected_product = None
    qr_data_uri = None
    buyer_id = current_user.id

    if request.method == "POST":
        action = request.form.get("action")
        product_id = request.form.get("product_id")
        if action == "select" and product_id:
            selected_product = Product.query.get(int(product_id))
            if selected_product:
                qr_data_uri = build_qr_for_product(selected_product, buyer_id)
        elif action in {"update_price", "update_stock"} and product_id:
            product = Product.query.get(int(product_id))
            if not product:
                flash("Product not found.", "error")
            elif not current_user.is_merchant:
                flash("Only merchants can update inventory.", "error")
            else:
                if action == "update_price":
                    new_price = float(request.form.get("price", product.price))
                    update_price(product, new_price)
                else:
                    new_stock = int(request.form.get("stock", product.stock))
                    product.stock = max(0, new_stock)
                    product.updated_at = datetime.utcnow()
                    db.session.commit()
                    flash("Stock updated.", "success")
                return redirect(url_for("main.merchant_portal"))
        elif action == "add_product" and current_user.is_merchant:
            name = request.form.get("name")
            price = float(request.form.get("price", 0))
            stock = int(request.form.get("stock", 0))
            description = request.form.get("description")
            if name:
                product = Product(name=name, price=price, stock=stock, description=description)
                db.session.add(product)
                db.session.commit()
                update_price(product, price)
                flash("Product created.", "success")
            return redirect(url_for("main.merchant_portal"))
        elif action == "update_product_increase_pct" and current_user.is_merchant and product_id:
            try:
                pct = float(request.form.get("increase_pct", ""))
                if pct < 0:
                    raise ValueError("Percentage must be non-negative")
                AppSetting.set(f"product:{int(product_id)}:increase_pct", str(pct))
                flash("Per-product sensitivity saved.", "success")
            except Exception as exc:
                flash(f"Failed to save per-product sensitivity: {exc}", "error")
            return redirect(url_for("main.merchant_portal"))
        else:
            return redirect(url_for("main.merchant_portal"))

    return render_template(
        "merchant.html",
        products=products,
        selected_product=selected_product,
        qr_data_uri=qr_data_uri,
    )


def update_price(product, new_price):
    product.price = max(0, new_price)
    product.updated_at = datetime.utcnow()
    db.session.add(PriceHistory(product=product, price=product.price))
    db.session.commit()
    flash("Price updated.", "success")


def build_qr_for_product(product, buyer_id):
    payload = {
        "product_id": product.id,
        "buyer_id": buyer_id,
        "price": product.price,
    }
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(str(payload))
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    data = base64.b64encode(buf.getvalue()).decode("utf-8")
    return f"data:image/png;base64,{data}"


@bp.route("/merchant/process/<int:product_id>", methods=["GET", "POST"])
@login_required
def process_sale(product_id):
    product = Product.query.get_or_404(product_id)
    buyer_id = request.args.get("buyer_id", type=int)
    buyer = User.query.get(buyer_id) if buyer_id else None
    if not current_user.is_merchant:
        abort(403)

    if request.method == "POST":
        if not buyer:
            flash("Buyer information missing.", "error")
            return redirect(url_for("main.merchant_portal"))
        if product.stock <= 0:
            flash("Product out of stock.", "error")
            return redirect(url_for("main.merchant_portal"))
        product.stock -= 1
        product.updated_at = datetime.utcnow()
        record_transaction(
            buyer,
            -product.price,
            f"Purchased {product.name}",
            counterparty=current_user,
            type_="purchase",
            commit=False,
        )
        record_transaction(
            current_user,
            product.price,
            f"Sold {product.name}",
            counterparty=buyer,
            type_="sale",
            commit=False,
        )
        # Increase price on purchase using admin-controlled sensitivity
        try:
            # Per-product override: product:{id}:increase_pct falls back to global price_increase_pct
            inc_pct = float(AppSetting.get(f"product:{product.id}:increase_pct", AppSetting.get("price_increase_pct", "5.0") or "5.0"))
            new_price = product.price * (1 + inc_pct / 100.0)
            update_price(product, new_price)
            flash("Sale completed.", "success")
        except Exception as exc:
            db.session.commit()
            flash(f"Sale completed, but price update failed: {exc}", "warning")
        return redirect(url_for("main.merchant_portal"))

    qr_data_uri = build_qr_for_product(product, buyer_id or current_user.id)
    return render_template(
        "process_sale.html",
        product=product,
        buyer=buyer,
        qr_data_uri=qr_data_uri,
    )


@bp.route("/admin")
@login_required
def admin_dashboard():
    if not current_user.is_admin:
        abort(403)
    products = Product.query.order_by(Product.name).all()
    users = User.query.order_by(User.name).all()
    price_history = PriceHistory.query.order_by(PriceHistory.timestamp.desc()).limit(50).all()
    recent_transactions = Transaction.query.order_by(Transaction.created_at.desc()).limit(20).all()
    stats = build_price_stats(products)
    default_increase = AppSetting.get("price_increase_pct", "5.0")
    # Defaults for per-game settings
    sp_dec = AppSetting.get("game:single_player:decrease_pct", AppSetting.get("game_reward_decrease_pct", "5.0"))
    sp_mult = AppSetting.get("game:single_player:multiplier", "1.0")
    pd_dec = AppSetting.get("game:prisoners:decrease_pct", AppSetting.get("game_reward_decrease_pct", "5.0"))
    pd_mult = AppSetting.get("game:prisoners:multiplier", "1.0")
    return render_template(
        "admin.html",
        products=products,
        price_history=price_history,
        transactions=recent_transactions,
        stats=stats,
        users=users,
        price_increase_pct=default_increase,
        sp_dec=sp_dec,
        sp_mult=sp_mult,
        pd_dec=pd_dec,
        pd_mult=pd_mult,
    )


@bp.route("/admin/assign-role", methods=["POST"])
@login_required
def assign_role():
    if not current_user.is_admin:
        abort(403)
    user_id = request.form.get("user_id", type=int)
    role_name = request.form.get("role")
    user = User.query.get_or_404(user_id)
    if role_name not in Role._value2member_map_:
        flash("Invalid role.", "error")
    else:
        user.role = Role(role_name)
        db.session.commit()
        flash("Role updated.", "success")
    return redirect(url_for("main.admin_dashboard"))


@bp.route("/admin/transactions")
@login_required
def admin_transactions_feed():
    if not current_user.is_admin:
        abort(403)
    transactions = (
        Transaction.query.order_by(Transaction.created_at.desc())
        .limit(20)
        .all()
    )
    data = [
        {
            "id": txn.id,
            "user": txn.user.name,
            "amount": txn.amount,
            "description": txn.description,
            "timestamp": txn.created_at.isoformat(),
        }
        for txn in transactions
    ]
    return jsonify(data)


def build_price_stats(products):
    stats = []
    for product in products:
        history = (
            PriceHistory.query.filter_by(product_id=product.id)
            .order_by(PriceHistory.timestamp.asc())
            .all()
        )
        if not history:
            continue
        first = history[0].price
        latest = history[-1].price
        change = latest - first
        percent = (change / first * 100) if first else 0
        stats.append(
            {
                "product": product,
                "initial_price": first,
                "latest_price": latest,
                "change": change,
                "percent_change": percent,
            }
        )
    return stats


@bp.route("/admin/settings/pricing", methods=["POST"])
@login_required
def update_pricing_settings():
    if not current_user.is_admin:
        abort(403)
    try:
        inc = float(request.form.get("price_increase_pct", "5"))
        sp_dec = float(request.form.get("sp_dec", "5"))
        pd_dec = float(request.form.get("pd_dec", "5"))
        if inc < 0 or sp_dec < 0 or pd_dec < 0:
            raise ValueError("Sensitivities must be non-negative")
        AppSetting.set("price_increase_pct", str(inc))
        AppSetting.set("game:single_player:decrease_pct", str(sp_dec))
        AppSetting.set("game:prisoners:decrease_pct", str(pd_dec))
        flash("Sensitivities updated.", "success")
    except Exception as exc:
        flash(f"Failed to update settings: {exc}", "error")
    return redirect(url_for("main.admin_dashboard"))
