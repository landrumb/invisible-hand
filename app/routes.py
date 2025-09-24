import base64
import hashlib
import io
import json
import mimetypes
import random
import secrets
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Set, Tuple

import tomllib

import qrcode
from PIL import Image
import pillow_heif
from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from flask_login import current_user, login_required
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from . import db
from .models import (
    FutureHolding,
    FutureListing,
    MerchantOrder,
    MerchantOrderItem,
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
    MoneyRequest,
    Alert,
    AlertReceipt,
    ShareholderVote,
    ShareholderVoteBallot,
    ShareholderVoteOption,
    ShareholderVoteParticipant,
    TelestrationEntry,
    TelestrationGame,
    TelestrationUpvote,
)
from .securities import (
    execute_equity_trade,
    execute_future_trade,
    execute_option_trade,
    get_simulator,
)
from itsdangerous import BadSignature

from .casino import get_casino_manager
from .games import TriviaQuestion, get_games_manager
from .telestrations import extract_seed_prompts

# Register HEIF opener for HEIC image support
pillow_heif.register_heif_opener()

bp = Blueprint("main", __name__)


def _slugify(value: str) -> str:
    normalized = value.strip().lower()
    cleaned = [
        ch if ch.isalnum() else "-"
        for ch in normalized
    ]
    slug = "".join(cleaned).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.replace("-", "_") or normalized.replace(" ", "_")


def _stock_config_path() -> Path:
    override = current_app.config.get("MERCHANT_STOCK_PATH")
    if override:
        return Path(override)
    return Path(current_app.root_path) / "config" / "stock.toml"


def _load_stock_catalog() -> list[dict]:
    path = _stock_config_path()
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        return []
    except tomllib.TOMLDecodeError as exc:
        current_app.logger.error("Failed to parse stock file %s: %s", path, exc)
        return []
    items = data.get("items", [])
    if not isinstance(items, list):
        current_app.logger.warning("Stock file %s has invalid items format", path)
        return []
    normalized_items: list[dict] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        entry = raw.copy()
        key = entry.get("key") or entry.get("name", "")
        if not key:
            continue
        entry["key"] = _slugify(str(key))
        normalized_items.append(entry)
    return normalized_items


def sync_products_from_stock() -> List[Product]:
    catalog = _load_stock_catalog()
    dirty = False
    for entry in catalog:
        key = entry["key"]
        product = Product.query.filter_by(catalog_key=key).first()
        created = False
        if not product:
            product = Product(catalog_key=key)
            db.session.add(product)
            created = True
            dirty = True

        name = entry.get("name") or product.name or key.replace("_", " ").title()
        if product.name != name:
            product.name = name
            dirty = True

        description = entry.get("description")
        if description != product.description:
            product.description = description
            dirty = True

        image_url = entry.get("image")
        if image_url != product.image_url:
            product.image_url = image_url
            dirty = True

        enabled = bool(entry.get("enabled", True))
        if product.enabled != enabled:
            product.enabled = enabled
            dirty = True

        if "price" in entry:
            try:
                base_price = max(0.0, float(entry["price"]))
            except (TypeError, ValueError):
                base_price = None
            if base_price is not None:
                if product.base_price is None or abs(product.base_price - base_price) > 1e-6:
                    product.base_price = base_price
                    dirty = True
                if created or abs((product.price or 0.0) - (product.base_price or 0.0)) < 1e-6:
                    product.price = base_price
                    product.updated_at = datetime.utcnow()
                    dirty = True

        if "stock" in entry:
            try:
                base_stock = max(0, int(entry["stock"]))
            except (TypeError, ValueError):
                base_stock = None
            if base_stock is not None:
                if product.base_stock is None or product.base_stock != base_stock:
                    product.base_stock = base_stock
                    dirty = True
                if created:
                    product.stock = base_stock
                    product.updated_at = datetime.utcnow()
                    dirty = True
                elif base_stock > (product.stock or 0):
                    product.stock = base_stock
                    product.updated_at = datetime.utcnow()
                    dirty = True

    if dirty:
        db.session.commit()

    return Product.query.order_by(Product.name.asc()).all()


def _merchant_sender() -> User:
    if current_user.is_authenticated and current_user.is_merchant:
        return current_user
    candidate = (
        User.query.filter(User.role.in_([Role.MERCHANT, Role.ADMIN]))
        .order_by(User.role.desc())
        .first()
    )
    return candidate or current_user


def _format_order_lines(order: MerchantOrder) -> str:
    lines = [f"Order #{order.id}"]
    for item in order.items:
        lines.append(
            f"- {item.quantity} × {item.product.name} — {item.subtotal:.2f} credits"
        )
    lines.append("")
    lines.append(f"Total: {order.total_price:.2f} credits")
    return "\n".join(lines)


def find_user_by_handle(handle: str):
    if not handle:
        return None
    normalized = handle.strip().lower()
    if not normalized:
        return None
    matches = User.query.filter(User.email.ilike(f"{normalized}@%"))
    users = matches.all()
    if not users:
        return None
    if len(users) > 1:
        raise ValueError("multiple")
    return users[0]


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


def _activate_game_context(game_key: str) -> float:
    AppSetting.set("current_game_context", game_key)
    try:
        multiplier = float(AppSetting.get(f"game:{game_key}:multiplier", "1.0") or "1.0")
    except Exception:
        multiplier = 1.0
    return multiplier if multiplier >= 0 else 0.0


def _create_alert(creator, recipients: List[User], message: str, *, title: str | None = None, category: str = "message", payload: dict | None = None, vote: ShareholderVote | None = None):
    clean_recipients = [user for user in recipients if user is not None]
    if not clean_recipients:
        return None
    alert = Alert(
        creator=creator,
        title=title,
        message=message,
        category=category,
        payload=dict(payload or {}),
    )
    if vote is not None:
        alert.vote = vote
    db.session.add(alert)
    db.session.flush()
    for user in clean_recipients:
        receipt = AlertReceipt(alert=alert, user=user)
        db.session.add(receipt)
    return alert


def _current_share_map(vote: ShareholderVote, user_ids: List[int]) -> dict[int, float]:
    if not user_ids:
        return {}
    holdings = (
        SecurityHolding.query.filter(
            SecurityHolding.security_symbol == vote.security_symbol,
            SecurityHolding.user_id.in_(user_ids),
        )
        .all()
    )
    share_map = {uid: 0.0 for uid in user_ids}
    for holding in holdings:
        share_map[holding.user_id] = float(max(0.0, holding.quantity or 0.0))
    return share_map


def _compute_vote_snapshot(vote: ShareholderVote) -> dict:
    user_ids = [participant.user_id for participant in vote.participants]
    share_map = _current_share_map(vote, user_ids)
    option_totals = {option.id: 0.0 for option in vote.options}
    ballots = {ballot.user_id: ballot for ballot in vote.ballots}
    for user_id, ballot in ballots.items():
        weight = share_map.get(user_id, 0.0)
        option_totals[ballot.option_id] = option_totals.get(ballot.option_id, 0.0) + weight
    total_shares = sum(share_map.values())
    voted_shares = sum(option_totals.values())
    unvoted = max(0.0, total_shares - voted_shares)
    return {
        "options": [
            {
                "id": option.id,
                "label": option.label,
                "shares": option_totals.get(option.id, 0.0),
            }
            for option in vote.options
        ],
        "unvoted_shares": unvoted,
        "total_shares": total_shares,
    }


def finalize_due_votes():
    now = datetime.utcnow()
    pending_votes = (
        ShareholderVote.query.filter(
            ShareholderVote.deadline <= now,
            ShareholderVote.finalized_at.is_(None),
        )
        .all()
    )
    for vote in pending_votes:
        snapshot = _compute_vote_snapshot(vote)
        vote.final_results = {
            "calculated_at": now.isoformat(),
            "deadline": vote.deadline.isoformat(),
            **snapshot,
        }
        vote.finalized_at = now
        recipients = [participant.user for participant in vote.participants]
        winning_option = max(
            snapshot["options"],
            key=lambda item: item["shares"],
            default=None,
        )
        if winning_option and snapshot["total_shares"]:
            pct = (winning_option["shares"] / snapshot["total_shares"]) * 100.0
            summary = f"{winning_option['label']} received {pct:.1f}% of eligible shares"
        else:
            summary = "No shares were cast in this vote"
        message = (
            f"Shareholder vote for {vote.security_symbol} has closed. {summary}."
        )
        payload = {
            "vote_id": vote.id,
            "title": vote.title,
            "results": snapshot,
        }
        _create_alert(
            vote.creator,
            recipients,
            message,
            title=f"Vote results: {vote.title}",
            category="vote_result",
            payload=payload,
            vote=vote,
        )
    if pending_votes:
        db.session.commit()


def ensure_vote_alerts_for_user(user: User, symbol: str):
    holdings = SecurityHolding.query.filter_by(user_id=user.id, security_symbol=symbol).first()
    if not holdings or not holdings.quantity or holdings.quantity <= 0:
        return
    open_votes = (
        ShareholderVote.query.filter_by(security_symbol=symbol)
        .filter(ShareholderVote.deadline > datetime.utcnow())
        .all()
    )
    for vote in open_votes:
        participant = ShareholderVoteParticipant.query.filter_by(
            vote_id=vote.id, user_id=user.id
        ).first()
        if not participant:
            participant = ShareholderVoteParticipant(vote=vote, user=user)
            db.session.add(participant)
        if participant.alerted_at is None:
            payload = {
                "vote_id": vote.id,
                "deadline": vote.deadline.isoformat(),
                "security_symbol": vote.security_symbol,
            }
            alert = _create_alert(
                vote.creator,
                [user],
                vote.message,
                title=f"Shareholder vote: {vote.title}",
                category="vote_invite",
                payload=payload,
                vote=vote,
            )
            if alert:
                participant.alerted_at = datetime.utcnow()

def _price_at_or_before(symbol: str, target: datetime):
    return (
        SecurityPriceHistory.query.filter_by(security_symbol=symbol)
        .filter(SecurityPriceHistory.timestamp <= target)
        .order_by(SecurityPriceHistory.timestamp.desc())
        .first()
    )


def _earliest_price(symbol: str):
    return (
        SecurityPriceHistory.query.filter_by(security_symbol=symbol)
        .order_by(SecurityPriceHistory.timestamp.asc())
        .first()
    )


def _delta_over_window(security: Security, window: timedelta = timedelta(minutes=10)) -> float:
    if not security:
        return 0.0
    target = datetime.utcnow() - window
    baseline_entry = _price_at_or_before(security.symbol, target)
    if not baseline_entry:
        baseline = security.last_price
        earliest = _earliest_price(security.symbol)
        if earliest:
            baseline = earliest.price
    else:
        baseline = baseline_entry.price
    return security.last_price - baseline


def _build_candles(symbol: str, window: timedelta = timedelta(hours=2)):
    now = datetime.utcnow()
    start = now - window
    history = (
        SecurityPriceHistory.query.filter_by(security_symbol=symbol)
        .filter(SecurityPriceHistory.timestamp >= start)
        .order_by(SecurityPriceHistory.timestamp.asc())
        .all()
    )
    if not history:
        fallback = (
            SecurityPriceHistory.query.filter_by(security_symbol=symbol)
            .order_by(SecurityPriceHistory.timestamp.desc())
            .limit(180)
            .all()
        )
        history = list(reversed(fallback))

    buckets = {}
    for entry in history:
        bucket_start = entry.timestamp.replace(second=0, microsecond=0)
        bucket = buckets.get(bucket_start)
        if not bucket:
            buckets[bucket_start] = {
                "open": entry.price,
                "high": entry.price,
                "low": entry.price,
                "close": entry.price,
            }
        else:
            bucket["high"] = max(bucket["high"], entry.price)
            bucket["low"] = min(bucket["low"], entry.price)
            bucket["close"] = entry.price

    ordered = []
    for ts in sorted(buckets.keys()):
        data = buckets[ts]
        ordered.append(
            {
                "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "open": data["open"],
                "high": data["high"],
                "low": data["low"],
                "close": data["close"],
            }
        )

    if not ordered and history:
        entry = history[-1]
        ordered.append(
            {
                "timestamp": entry.timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "open": entry.price,
                "high": entry.price,
                "low": entry.price,
                "close": entry.price,
            }
        )

    return ordered[-180:]


@bp.route("/")
def index():
    return render_template("index.html")


@bp.route("/dashboard")
@login_required
def dashboard():
    finalize_due_votes()
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
    target_handle = request.args.get("target", "").strip()
    incoming_requests = (
        MoneyRequest.query.filter_by(target_id=current_user.id, status="pending")
        .order_by(MoneyRequest.created_at.desc())
        .all()
    )
    outgoing_requests = (
        MoneyRequest.query.filter_by(requester_id=current_user.id)
        .order_by(MoneyRequest.created_at.desc())
        .limit(10)
        .all()
    )
    return render_template(
        "dashboard.html",
        balance=current_user.balance,
        transactions=latest_transactions,
        active_match=active_match,
        target_handle=target_handle,
        incoming_requests=incoming_requests,
        outgoing_requests=outgoing_requests,
        qr_data_uri=build_qr_for_user(current_user),
    )


@bp.route("/inbox")
@login_required
def inbox():
    finalize_due_votes()
    incoming_requests = (
        MoneyRequest.query.filter_by(target_id=current_user.id)
        .order_by(MoneyRequest.created_at.desc())
        .all()
    )
    outgoing_requests = (
        MoneyRequest.query.filter_by(requester_id=current_user.id)
        .order_by(MoneyRequest.created_at.desc())
        .all()
    )
    alert_receipts = (
        AlertReceipt.query.filter_by(user_id=current_user.id)
        .join(Alert)
        .order_by(Alert.created_at.desc())
        .all()
    )

    newly_read = []
    now = datetime.utcnow()
    for receipt in alert_receipts:
        if receipt.read_at is None:
            receipt.read_at = now
            newly_read.append(receipt)
    if newly_read:
        db.session.commit()

    return render_template(
        "inbox.html",
        incoming_requests=incoming_requests,
        outgoing_requests=outgoing_requests,
        alert_receipts=alert_receipts,
    )


@bp.route("/transfer", methods=["POST"])
@login_required
def handle_transfer():
    action = request.form.get("action")
    handle = (request.form.get("handle") or "").strip()
    amount = request.form.get("amount", type=float)
    message = (request.form.get("message") or "").strip()
    redirect_target = url_for("main.dashboard", target=handle) if handle else url_for("main.dashboard")

    if not amount or amount <= 0:
        flash("Please enter a positive amount.", "error")
        return redirect(redirect_target)

    try:
        target_user = find_user_by_handle(handle)
    except ValueError:
        flash("Multiple users share that handle. Please use their full email instead.", "error")
        return redirect(redirect_target)

    if not target_user:
        flash("Could not find a user with that handle.", "error")
        return redirect(redirect_target)

    if action == "send":
        if target_user.id == current_user.id:
            flash("You cannot send money to yourself.", "error")
            return redirect(redirect_target)
        if current_user.balance < amount:
            flash("Insufficient balance to send that amount.", "error")
            return redirect(redirect_target)

        note = f" ({message})" if message else ""
        record_transaction(
            current_user,
            -amount,
            f"Transfer to {target_user.name}{note}",
            counterparty=target_user,
            type_="transfer",
            commit=False,
        )
        record_transaction(
            target_user,
            amount,
            f"Transfer from {current_user.name}{note}",
            counterparty=current_user,
            type_="transfer",
            commit=False,
        )
        db.session.commit()
        flash(f"Sent {amount:.2f} credits to {target_user.name}.", "success")
        return redirect(url_for("main.dashboard"))

    if action == "request":
        if target_user.id == current_user.id:
            flash("You cannot request money from yourself.", "error")
            return redirect(redirect_target)
        money_request = MoneyRequest(
            requester=current_user,
            target=target_user,
            amount=amount,
            message=message or None,
        )
        db.session.add(money_request)
        db.session.commit()
        flash(f"Requested {amount:.2f} credits from {target_user.name}.", "success")
        return redirect(url_for("main.dashboard"))

    flash("Unknown action.", "error")
    return redirect(url_for("main.dashboard"))


@bp.route("/requests/<int:request_id>/respond", methods=["POST"])
@login_required
def respond_money_request(request_id):
    money_request = MoneyRequest.query.get_or_404(request_id)
    if money_request.target_id != current_user.id:
        abort(403)
    if money_request.status != "pending":
        flash("This request has already been handled.", "info")
        return redirect(url_for("main.dashboard"))

    action = request.form.get("action")
    if action == "accept":
        if current_user.balance < money_request.amount:
            flash("You do not have enough balance to fulfill this request.", "error")
            return redirect(url_for("main.dashboard"))
        note = f" ({money_request.message})" if money_request.message else ""
        record_transaction(
            current_user,
            -money_request.amount,
            f"Money request from {money_request.requester.name}{note}",
            counterparty=money_request.requester,
            type_="transfer",
            commit=False,
        )
        record_transaction(
            money_request.requester,
            money_request.amount,
            f"Money request fulfilled by {current_user.name}{note}",
            counterparty=current_user,
            type_="transfer",
            commit=False,
        )
        money_request.status = "completed"
        money_request.resolved_at = datetime.utcnow()
        db.session.commit()
        flash(f"Sent {money_request.amount:.2f} credits to {money_request.requester.name}.", "success")
        return redirect(url_for("main.dashboard"))

    if action == "decline":
        money_request.status = "declined"
        money_request.resolved_at = datetime.utcnow()
        db.session.commit()
        flash("Request declined.", "info")
        return redirect(url_for("main.dashboard"))

    flash("Unknown action.", "error")
    return redirect(url_for("main.dashboard"))


@bp.route("/securities")
@login_required
def securities_hub():
    finalize_due_votes()
    simulator = get_simulator()
    securities = Security.query.order_by(Security.symbol.asc()).all()
    security_positions = {
        holding.security_symbol: holding
        for holding in SecurityHolding.query.filter_by(user_id=current_user.id).all()
    }
    for security in securities:
        security.delta_10m = _delta_over_window(security)

    return render_template(
        "securities.html",
        securities=securities,
        security_positions=security_positions,
        update_interval=simulator.interval,
        risk_free_rate=simulator.risk_free_rate,
    )


@bp.route("/api/securities")
@login_required
def securities_snapshot():
    securities = Security.query.order_by(Security.symbol.asc()).all()
    payload = []
    for security in securities:
        change = _delta_over_window(security)
        payload.append(
            {
                "symbol": security.symbol,
                "name": security.name,
                "price": security.last_price,
                "updated_at": security.updated_at.isoformat(),
                "description": security.description,
                "delta_10m": change,
            }
        )
    return jsonify(payload)


@bp.route("/api/securities/<symbol>/details")
@login_required
def security_details(symbol):
    if not symbol:
        abort(404)
    normalized = symbol.strip().upper()
    security = Security.query.get_or_404(normalized)
    simulator = get_simulator()

    position = (
        SecurityHolding.query.filter_by(
            user_id=current_user.id, security_symbol=security.symbol
        )
        .first()
    )

    delta_10m = _delta_over_window(security)

    now = datetime.utcnow()

    option_listings = (
        OptionListing.query.filter_by(security_symbol=security.symbol)
        .filter(OptionListing.expiration > now)
        .order_by(OptionListing.expiration.asc())
        .all()
    )
    option_holdings = {}
    if option_listings:
        listing_ids = [listing.id for listing in option_listings]
        option_holdings = {
            holding.listing_id: holding
            for holding in OptionHolding.query.filter(
                OptionHolding.user_id == current_user.id,
                OptionHolding.listing_id.in_(listing_ids),
            ).all()
        }

    options_payload = []
    for listing in option_listings:
        seconds_left = max(0.0, (listing.expiration - now).total_seconds())
        minutes_left = int((seconds_left + 59) // 60)
        holding = option_holdings.get(listing.id)
        options_payload.append(
            {
                "id": listing.id,
                "contract": f"{listing.security_symbol} {listing.option_type.value.upper()}",
                "option_type": listing.option_type.value,
                "strike": listing.strike,
                "minutes_left": minutes_left,
                "expiration_display": listing.expiration.strftime("%Y-%m-%d %H:%M:%S UTC"),
                "premium": simulator.price_option(listing),
                "holding": (
                    {
                        "quantity": holding.quantity,
                        "average_premium": holding.average_premium,
                    }
                    if holding and holding.quantity
                    else None
                ),
            }
        )

    future_listings = (
        FutureListing.query.filter_by(security_symbol=security.symbol)
        .filter(FutureListing.delivery_date > now)
        .order_by(FutureListing.delivery_date.asc())
        .all()
    )
    future_holdings = {}
    if future_listings:
        listing_ids = [listing.id for listing in future_listings]
        future_holdings = {
            holding.listing_id: holding
            for holding in FutureHolding.query.filter(
                FutureHolding.user_id == current_user.id,
                FutureHolding.listing_id.in_(listing_ids),
            ).all()
        }

    future_payload = []
    for listing in future_listings:
        seconds_left = max(0.0, (listing.delivery_date - now).total_seconds())
        minutes_left = int((seconds_left + 59) // 60)
        holding = future_holdings.get(listing.id)
        future_payload.append(
            {
                "id": listing.id,
                "contract": f"{listing.security_symbol} FUT",
                "minutes_left": minutes_left,
                "delivery_display": listing.delivery_date.strftime("%Y-%m-%d %H:%M:%S UTC"),
                "forward": simulator.price_future(listing),
                "holding": (
                    {
                        "quantity": holding.quantity,
                        "entry_price": holding.entry_price,
                    }
                    if holding and holding.quantity
                    else None
                ),
            }
        )

    candles = _build_candles(security.symbol)

    payload = {
        "symbol": security.symbol,
        "name": security.name,
        "last_price": security.last_price,
        "delta_10m": delta_10m,
        "description": security.description,
        "position": (
            {
                "quantity": position.quantity,
                "average_price": position.average_price,
            }
            if position and position.quantity
            else None
        ),
        "candles": candles,
        "options": options_payload,
        "futures": future_payload,
    }

    return jsonify(payload)


@bp.route("/securities/trade", methods=["POST"])
@login_required
def trade_security():
    finalize_due_votes()
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
        ensure_vote_alerts_for_user(current_user, symbol)
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


@bp.route("/games")
@login_required
def games_lobby():
    manager = get_games_manager()
    games = manager.list_games()
    return render_template("games/index.html", games=games)


@bp.route("/games/<game_key>", methods=["GET", "POST"])
@login_required
def play_game(game_key: str):
    manager = get_games_manager()
    game = manager.get_game(game_key)
    if game is None:
        abort(404)

    handlers = {
        "timed_math": _handle_timed_math_game,
        "reaction": _handle_reaction_game,
        "trivia": _handle_trivia_game,
        "newcomb": _handle_newcomb_game,
        "among_us": _handle_among_us_game,
        "telestrations": _handle_telestrations_game,
    }
    handler = handlers.get(game.type)
    if handler is None:
        abort(404)
    return handler(game, manager)


@bp.route("/games/<game_key>/submit", methods=["POST"])
@login_required
def submit_game_result(game_key: str):
    manager = get_games_manager()
    game = manager.get_game(game_key)
    if game is None:
        return jsonify({"error": "Unknown game."}), 404

    handlers = {
        "reaction": _submit_reaction_game,
        "among_us": _submit_among_us_task,
    }
    handler = handlers.get(game.type)
    if handler is None:
        return jsonify({"error": "Game does not accept direct submissions."}), 400
    return handler(game, manager)


def _handle_timed_math_game(game, manager):
    multiplier = _activate_game_context(game.key)
    result = None
    if request.method == "POST":
        token = request.form.get("token", "")
        answer_raw = request.form.get("answer")
        try:
            payload = manager.load_token(token)
        except BadSignature:
            result = {
                "category": "error",
                "title": "Invalid round",
                "message": "We couldn't verify that round. Try another problem.",
            }
        else:
            if payload.get("game") != game.key:
                result = {
                    "category": "error",
                    "title": "Mismatched round",
                    "message": "That attempt doesn't match the current puzzle.",
                }
            else:
                try:
                    answer = int(answer_raw) if answer_raw is not None else None
                except (TypeError, ValueError):
                    answer = None
                expected = int(payload.get("a", 0)) * int(payload.get("b", 0))
                start_ts = float(payload.get("start", payload.get("_ts", time.time())))
                elapsed = max(0.0, min(300.0, time.time() - start_ts))
                max_time = float(game.params.get("max_time", 10.0) or 10.0)
                base_reward = float(game.params.get("base_reward", 10.0) or 10.0)
                min_reward = float(game.params.get("min_reward", 0.0) or 0.0)
                min_factor = float(game.params.get("min_factor", 0.0) or 0.0)
                if answer is None:
                    result = {
                        "category": "error",
                        "title": "Missing answer",
                        "message": "Please enter a numeric answer before submitting.",
                    }
                elif answer != expected:
                    result = {
                        "category": "error",
                        "title": "Not quite",
                        "message": f"The correct product was {expected}.",
                    }
                else:
                    if max_time <= 0:
                        speed_factor = 1.0
                    else:
                        speed_factor = max(min_factor, max(0.0, (max_time - elapsed) / max_time))
                    raw_reward = max(base_reward * speed_factor, min_reward)
                    payout = round(max(0.0, raw_reward * multiplier), 2)
                    if payout > 0:
                        record_transaction(current_user, payout, f"{game.name} win")
                    result = {
                        "category": "success" if payout > 0 else "info",
                        "title": "Correct!",
                        "message": f"You solved it in {elapsed:.2f}s and earned {payout:.2f} credits.",
                    }

    a = random.randint(10, 99)
    b = random.randint(10, 99)
    start_ts = time.time()
    token = manager.create_token({"game": game.key, "a": a, "b": b, "start": start_ts})
    started_at = datetime.utcfromtimestamp(start_ts).strftime("%H:%M:%S UTC")
    return render_template(
        "games/timed_math.html",
        game=game,
        a=a,
        b=b,
        token=token,
        started_at=started_at,
        result=result,
    )


def _handle_reaction_game(game, manager):
    _activate_game_context(game.key)
    token = manager.create_token({"game": game.key, "mode": "reaction"})
    max_time = float(game.params.get("max_time", 1.0) or 1.0)
    submit_url = url_for("main.submit_game_result", game_key=game.key)
    return render_template(
        "games/reaction.html",
        game=game,
        token=token,
        max_time=f"{max_time:.2f}",
        submit_url=submit_url,
    )


def _handle_trivia_game(game, manager):
    multiplier = _activate_game_context(game.key)
    set_key = str(game.params.get("question_set", "")).strip()
    trivia_set = manager.get_trivia_set(set_key)
    if trivia_set is None:
        abort(404)

    email = (getattr(current_user, "email", "") or "").strip().lower()
    if not email:
        email = f"user-{current_user.id}"
    user_hash = int(hashlib.sha256(email.encode("utf-8")).hexdigest(), 16)

    progress_key = f"game:{game.key}:progress:{current_user.id}"
    AppSetting.delete(progress_key)

    seen_key = f"game:{game.key}:seen:{current_user.id}"
    seen_raw = AppSetting.get(seen_key, "") or ""
    seen_values: Set[int] = set()
    if seen_raw:
        parsed_seen: List[int]
        try:
            data = json.loads(seen_raw)
            if isinstance(data, list):
                parsed_seen = data
            else:
                parsed_seen = []
        except Exception:
            parsed_seen = [value for value in seen_raw.split(",") if value.strip()]
        for value in parsed_seen:
            try:
                seen_values.add(int(value))
            except (TypeError, ValueError):
                continue

    ordered_pairs = trivia_set.ordered_pairs_for_user(user_hash)

    active_key = f"game:{game.key}:active:{current_user.id}"
    active_raw = AppSetting.get(active_key, "") or ""
    active_order: Optional[int] = None
    active_question_id: Optional[str] = None
    if active_raw:
        try:
            active_data = json.loads(active_raw)
        except Exception:
            active_data = None
        if isinstance(active_data, dict):
            try:
                active_order = int(active_data.get("order"))
            except (TypeError, ValueError):
                active_order = None
            question_id = active_data.get("question")
            if isinstance(question_id, str):
                active_question_id = question_id

    selected_question: Optional[TriviaQuestion] = None
    selected_order: Optional[int] = None

    if active_question_id is not None and active_order is not None:
        for order_value, question in ordered_pairs:
            if question.id == active_question_id and order_value == active_order:
                selected_question = question
                selected_order = order_value
                break
        else:
            AppSetting.delete(active_key)

    if selected_question is None:
        for order_value, question in ordered_pairs:
            if order_value in seen_values:
                continue
            selected_question = question
            selected_order = order_value
            AppSetting.set(active_key, json.dumps({"order": order_value, "question": question.id}))
            break

    result = None
    if request.method == "POST" and selected_question is not None:
        token = request.form.get("token", "")
        submitted_answer = request.form.get("answer")
        try:
            payload = manager.load_token(token)
        except BadSignature:
            result = {
                "category": "error",
                "title": "Invalid submission",
                "message": "We couldn't verify that question. Try again.",
            }
        else:
            if payload.get("game") != game.key:
                result = {
                    "category": "error",
                    "title": "Mismatched question",
                    "message": "That answer doesn't match the current prompt.",
                }
            else:
                question_id = payload.get("question")
                try:
                    token_order = int(payload.get("order"))
                except (TypeError, ValueError):
                    token_order = None
                expected_order = int(selected_question.hash_value, 16) ^ user_hash
                if question_id != selected_question.id or token_order != expected_order:
                    result = {
                        "category": "error",
                        "title": "Out of order",
                        "message": "Looks like you've already moved on."
                    }
                elif token_order in seen_values:
                    result = {
                        "category": "error",
                        "title": "Already answered",
                        "message": "You've already completed that question.",
                    }
                else:
                    try:
                        answer_index = int(submitted_answer) if submitted_answer is not None else -1
                    except (TypeError, ValueError):
                        answer_index = -1
                    rate_payload = {
                        "game": game.key,
                        "question": selected_question.id,
                        "hash": selected_question.hash_value,
                        "set": trivia_set.key,
                        "order": expected_order,
                    }
                    rate_token = manager.create_token(rate_payload)
                    if answer_index == selected_question.answer:
                        payout = round(trivia_set.reward * multiplier, 2)
                        if payout > 0:
                            record_transaction(current_user, payout, f"{game.name} correct answer")
                        result = {
                            "category": "success" if payout > 0 else "info",
                            "title": "Correct!",
                            "message": f"You earned {payout:.2f} credits.",
                            "rate": {
                                "token": rate_token,
                                "submitted_by": selected_question.submitted_by,
                            },
                        }
                    else:
                        correct_choice = selected_question.choices[selected_question.answer]
                        result = {
                            "category": "error",
                            "title": "Incorrect",
                            "message": f"The correct answer was '{correct_choice}'.",
                            "rate": {
                                "token": rate_token,
                                "submitted_by": selected_question.submitted_by,
                            },
                        }
                    seen_values.add(expected_order)
                    AppSetting.set(seen_key, json.dumps(sorted(seen_values)))
                    AppSetting.delete(active_key)
                    selected_question = None
                    selected_order = None

    if selected_question is None:
        for order_value, question in ordered_pairs:
            if order_value in seen_values:
                continue
            selected_question = question
            selected_order = order_value
            AppSetting.set(active_key, json.dumps({"order": order_value, "question": question.id}))
            break

    if selected_question is None:
        return render_template("games/trivia_complete.html", game=game, result=result)

    if selected_order is None:
        selected_order = int(selected_question.hash_value, 16) ^ user_hash

    token_payload = {
        "game": game.key,
        "question": selected_question.id,
        "order": selected_order,
    }
    token = manager.create_token(token_payload)
    return render_template(
        "games/trivia.html",
        game=game,
        question=selected_question,
        token=token,
        set_description=trivia_set.description,
        result=result,
        submit_question_url=url_for(
            "main.submit_trivia_question", game_key=game.key, set_key=trivia_set.key
        ),
    )


@bp.route("/games/<game_key>/submit-question", methods=["GET", "POST"])
@login_required
def submit_trivia_question(game_key: str):
    manager = get_games_manager()
    game = manager.get_game(game_key)
    if game is None or game.type != "trivia":
        abort(404)

    set_key = str(request.args.get("set_key") or request.args.get("set") or "").strip()
    if request.method == "POST":
        set_key = str(request.form.get("set_key", set_key)).strip() or set_key
    if not set_key:
        set_key = str(game.params.get("question_set", "")).strip()
    trivia_set = manager.get_trivia_set(set_key)
    if trivia_set is None:
        abort(404)

    errors: List[str] = []
    previous_values = {
        "prompt": "",
        "explanation": "",
        "image": "",
        "choices": ["", "", "", ""],
        "correct_choice": 0,
    }
    if request.method == "POST":
        prompt = (request.form.get("prompt", "") or "").strip()
        explanation = (request.form.get("explanation", "") or "").strip()
        image = (request.form.get("image", "") or "").strip()
        raw_choices = [choice.strip() for choice in request.form.getlist("choices")]
        choices = [choice for choice in raw_choices if choice]
        if not prompt:
            errors.append("Please enter a question prompt.")
        if len(choices) < 2:
            errors.append("Add at least two answer options.")
        try:
            answer_index = int(request.form.get("correct_choice", "0"))
        except (TypeError, ValueError):
            answer_index = 0
        answer_index = max(0, min(answer_index, len(choices) - 1)) if choices else 0

        previous_values.update(
            {
                "prompt": prompt,
                "explanation": explanation,
                "image": image,
                "choices": raw_choices or ["", "", "", ""],
                "correct_choice": answer_index,
            }
        )
        if len(previous_values["choices"]) < 4:
            previous_values["choices"].extend([""] * (4 - len(previous_values["choices"])) )

        if not errors:
            submitted_by = (getattr(current_user, "email", "") or "").strip()
            if not submitted_by:
                submitted_by = f"user-{current_user.id}"
            try:
                manager.append_submitted_question(
                    set_key,
                    {
                        "prompt": prompt,
                        "choices": choices,
                        "answer": answer_index,
                        "explanation": explanation or None,
                        "image": image or None,
                        "submitted_by": submitted_by,
                    },
                )
            except ValueError as error:
                errors.append(str(error))
            else:
                flash("Thanks! Your question was submitted for review.", "success")
                return redirect(url_for("main.play_game", game_key=game.key))

    return render_template(
        "games/trivia_submit.html",
        game=game,
        trivia_set=trivia_set,
        errors=errors,
        set_key=set_key,
        previous=previous_values,
    )


@bp.route("/games/<game_key>/rate", methods=["POST"])
@login_required
def rate_trivia_question(game_key: str):
    manager = get_games_manager()
    game = manager.get_game(game_key)
    if game is None or game.type != "trivia":
        abort(404)

    token_value = request.form.get("token", "")
    action = (request.form.get("action", "") or "").strip().lower()
    try:
        payload = manager.load_token(token_value)
    except BadSignature:
        flash("We couldn't verify that rating request.", "error")
        return redirect(url_for("main.play_game", game_key=game.key))

    if payload.get("game") != game.key:
        flash("That rating doesn't match this game.", "error")
        return redirect(url_for("main.play_game", game_key=game.key))

    if action not in {"good", "bad", "report"}:
        flash("Select a valid rating option.", "error")
        return redirect(url_for("main.play_game", game_key=game.key))

    question_hash = payload.get("hash")
    question_id = payload.get("question")
    set_key = payload.get("set")
    try:
        expected_order = int(payload.get("order"))
    except (TypeError, ValueError):
        expected_order = None

    trivia_set = manager.get_trivia_set(set_key) if isinstance(set_key, str) else None
    question: Optional[TriviaQuestion] = None
    if trivia_set is not None:
        for candidate in trivia_set.questions:
            if candidate.id == question_id and candidate.hash_value == question_hash:
                question = candidate
                break
    if question is None:
        flash("We couldn't find that question anymore.", "error")
        return redirect(url_for("main.play_game", game_key=game.key))

    # Ensure the player has already answered this question
    if expected_order is not None:
        seen_key = f"game:{game.key}:seen:{current_user.id}"
        seen_raw = AppSetting.get(seen_key, "") or ""
        seen_values: Set[int] = set()
        if seen_raw:
            try:
                data = json.loads(seen_raw)
                if isinstance(data, list):
                    for value in data:
                        try:
                            seen_values.add(int(value))
                        except (TypeError, ValueError):
                            continue
            except Exception:
                for value in seen_raw.split(","):
                    try:
                        seen_values.add(int(value))
                    except (TypeError, ValueError):
                        continue
        if expected_order not in seen_values:
            flash("Answer the question before rating it.", "error")
            return redirect(url_for("main.play_game", game_key=game.key))

    rating_key = f"game:{game.key}:rating:{current_user.id}:{question_hash}"
    if AppSetting.get(rating_key):
        flash("You've already rated that question.", "info")
        return redirect(url_for("main.play_game", game_key=game.key))

    counts_key = f"game:{game.key}:rating-counts:{question_hash}"
    counts_raw = AppSetting.get(counts_key, "{}") or "{}"
    try:
        counts = json.loads(counts_raw)
    except Exception:
        counts = {}
    if not isinstance(counts, dict):
        counts = {}
    counts[action] = int(counts.get(action, 0)) + 1
    AppSetting.set(counts_key, json.dumps(counts))
    AppSetting.set(rating_key, action)

    if action == "good" and question.submitted_by:
        reward_amount = float(game.params.get("rating_reward", 0.0) or 0.0)
        if reward_amount > 0:
            submitter = User.query.filter_by(email=question.submitted_by).first()
            if submitter is not None:
                description = f"{game.name} question upvote"
                record_transaction(
                    submitter,
                    reward_amount,
                    description,
                    counterparty=current_user.email if getattr(current_user, "email", None) else None,
                )

    flash("Thanks for the feedback!", "success")
    return redirect(url_for("main.play_game", game_key=game.key))


def _handle_newcomb_game(game, manager):
    multiplier = _activate_game_context(game.key)
    result = None
    selection_raw = request.form.get("selection", "") if request.method == "POST" else ""
    selection = [part for part in selection_raw.split(",") if part]
    token = manager.create_token({"game": game.key, "mode": "newcomb"})
    base_small = float(game.params.get("payout_small", 25.0) or 0.0)
    base_large = float(game.params.get("payout_large", 150.0) or 0.0)
    base_both = float(game.params.get("payout_both", 0.0) or 0.0)
    if request.method == "POST":
        token_value = request.form.get("token", "")
        try:
            payload = manager.load_token(token_value)
        except BadSignature:
            result = {
                "category": "error",
                "title": "Invalid selection",
                "message": "We couldn't verify your choice. Try again.",
            }
        else:
            if payload.get("game") != game.key:
                result = {
                    "category": "error",
                    "title": "Mismatched selection",
                    "message": "Those boxes don't match the current round.",
                }
            else:
                chosen = set(selection)
                if not chosen:
                    result = {
                        "category": "error",
                        "title": "No boxes selected",
                        "message": "Choose at least one box before submitting.",
                    }
                elif chosen == {"transparent", "opaque"}:
                    payout = round(max(0.0, base_both * multiplier), 2)
                    result = {
                        "category": "error" if payout <= 0 else "info",
                        "title": "Two-box trap",
                        "message": f"Omega saw it coming. You earned {payout:.2f} credits.",
                    }
                elif chosen == {"transparent"}:
                    payout = round(max(0.0, base_small * multiplier), 2)
                    if payout > 0:
                        record_transaction(current_user, payout, f"{game.name} small box")
                    result = {
                        "category": "success" if payout > 0 else "info",
                        "title": "Safe pick",
                        "message": f"The transparent box paid out {payout:.2f} credits.",
                    }
                elif chosen == {"opaque"}:
                    payout = round(max(0.0, base_large * multiplier), 2)
                    if payout > 0:
                        record_transaction(current_user, payout, f"{game.name} opaque box")
                    result = {
                        "category": "success" if payout > 0 else "info",
                        "title": "Bold move",
                        "message": f"Omega left you {payout:.2f} credits.",
                    }
                else:
                    payout = 0.0
                    result = {
                        "category": "error",
                        "title": "Confused choice",
                        "message": "We only recognize the transparent and opaque boxes.",
                    }
    small_display = round(max(0.0, base_small * multiplier), 2)
    large_display = round(max(0.0, base_large * multiplier), 2)
    return render_template(
        "games/newcomb.html",
        game=game,
        token=token,
        selection=selection,
        small_payout=f"{small_display:.2f}",
        large_payout=f"{large_display:.2f}",
        result=result,
    )


def _handle_among_us_game(game, manager):
    _activate_game_context(game.key)
    task_type = str(game.params.get("task", "")).strip()
    if task_type not in {"swipe_card", "prime_shields", "align_engine"}:
        abort(404)

    state: dict = {}
    payload = {"game": game.key, "task": task_type}
    if task_type == "align_engine":
        target = random.randint(10, 90)
        precision = float(game.params.get("precision", 3.0) or 3.0)
        state = {"target": target, "precision": precision}
        payload.update(state)
    token = manager.create_token(payload)
    submit_url = url_for("main.submit_game_result", game_key=game.key)
    return render_template(
        "games/among_us.html",
        game=game,
        task_type=task_type,
        submit_url=submit_url,
        token=token,
        task_state=state or None,
    )


def _handle_telestrations_game(game, manager):
    configured_turns = _get_telestrations_max_turns(game.params)
    _ensure_seeded_telestrations_games(game.params)
    active_game = _find_active_telestration_game(current_user.id)

    if request.method == "POST":
        action = (request.form.get("action") or "").strip().lower()
        if action == "start":
            prompt = (request.form.get("prompt") or "").strip()
            if active_game is not None:
                flash("You already have a telestrations game in progress. Finish it before starting another.", "warning")
            elif not prompt:
                flash("Provide a word or short phrase to start the game.", "warning")
            elif len(prompt) > 120:
                flash("Keep the starting prompt under 120 characters.", "warning")
            else:
                new_game = TelestrationGame(
                    creator=current_user,
                    prompt=prompt,
                    max_turns=configured_turns,
                    turns_taken=1,
                )
                first_entry = TelestrationEntry(
                    game=new_game,
                    contributor=current_user,
                    turn_index=1,
                    entry_type="description",
                    text_content=prompt,
                )
                db.session.add(new_game)
                db.session.add(first_entry)
                db.session.commit()
                flash("Game started! The drawing relay is waiting for the next player.", "success")
                return redirect(url_for("main.telestrations_play", game_id=new_game.id))
        elif action == "join":
            selection = _pick_telestration_game_for_user(current_user)
            if selection is None:
                flash("No active telestrations games need your help right now. Check back soon!", "info")
            else:
                # Double-check that the selected game is still active and user hasn't played
                if (selection.is_active() and 
                    not any(entry.contributor_id == current_user.id for entry in selection.entries)):
                    return redirect(url_for("main.telestrations_play", game_id=selection.id))
                else:
                    # Game state changed, try again
                    flash("Game state changed while selecting. Please try joining again.", "info")
        else:
            flash("We couldn't determine that action.", "warning")

    active_count = _telestrations_active_query().count()
    active_game = _find_active_telestration_game(current_user.id)
    return render_template(
        "games/telestrations.html",
        game=game,
        active_count=active_count,
        max_turns=configured_turns,
        active_game=active_game,
        upvote_reward=_get_telestrations_upvote_reward(game.params),
    )


def _telestrations_active_query():
    return TelestrationGame.query.filter(
        TelestrationGame.completed_at.is_(None),
        TelestrationGame.turns_taken < TelestrationGame.max_turns,
    )


def _find_active_telestration_game(user_id: int):
    if not user_id:
        return None
    return (
        _telestrations_active_query()
        .filter(TelestrationGame.creator_id == user_id)
        .order_by(TelestrationGame.created_at.desc())
        .first()
    )


def _pick_telestration_game_for_user(user):
    query = _telestrations_active_query()
    if not getattr(user, "id", None):
        return query.order_by(func.random()).first()
    subquery = (
        db.session.query(TelestrationEntry.game_id)
        .filter(TelestrationEntry.contributor_id == user.id)
        .subquery()
    )
    query = query.filter(~TelestrationGame.id.in_(subquery))
    query = query.filter(TelestrationGame.creator_id != user.id)
    return query.order_by(func.random()).first()


def _get_telestrations_max_turns(params: Optional[dict] = None) -> int:
    default_value = 8
    if isinstance(params, dict):
        try:
            candidate = int(params.get("max_turns", default_value) or default_value)
        except (TypeError, ValueError):
            candidate = default_value
        default_value = candidate
    raw_setting = AppSetting.get("telestrations_max_turns", None)
    if raw_setting:
        try:
            value = int(raw_setting)
        except (TypeError, ValueError):
            value = default_value
    else:
        value = default_value
    return max(2, value)


def _get_telestrations_upvote_reward(params: Optional[dict] = None) -> float:
    default_value = 1.0
    if isinstance(params, dict):
        try:
            candidate = float(params.get("upvote_reward", default_value) or default_value)
        except (TypeError, ValueError):
            candidate = default_value
        default_value = candidate
    raw_setting = AppSetting.get("telestrations_upvote_reward", None)
    if raw_setting:
        try:
            value = float(raw_setting)
        except (TypeError, ValueError):
            value = default_value
    else:
        value = default_value
    return max(0.0, value)


def _get_telestrations_seed_user() -> Tuple[Optional[User], bool]:
    identifier = "telestrations-seed"
    user = User.query.filter_by(google_id=identifier).first()
    if user is not None:
        return user, False
    email = current_app.config.get("TELESTRATIONS_SEED_EMAIL", "telestrations-seed@system.local")
    existing = User.query.filter_by(email=email).first()
    if existing is not None:
        if existing.google_id != identifier:
            existing.google_id = identifier
            db.session.add(existing)
            db.session.flush()
            return existing, True
        return existing, False
    name = current_app.config.get("TELESTRATIONS_SEED_NAME", "Arcade Muse")
    user = User(
        google_id=identifier,
        email=email,
        name=name,
        role=Role.PLAYER,
    )
    db.session.add(user)
    db.session.flush()
    return user, True


def _ensure_seeded_telestrations_games(params: Optional[dict] = None) -> None:
    prompts = extract_seed_prompts(params)
    if not prompts:
        return
    seed_user, user_created = _get_telestrations_seed_user()
    if seed_user is None:
        return
    configured_turns = _get_telestrations_max_turns(params)
    changed = user_created
    for prompt in prompts:
        normalized = prompt.casefold()
        existing = (
            _telestrations_active_query()
            .filter(func.lower(TelestrationGame.prompt) == normalized)
            .first()
        )
        if existing is not None:
            continue
        new_game = TelestrationGame(
            creator=seed_user,
            prompt=prompt,
            max_turns=configured_turns,
            turns_taken=1,
        )
        first_entry = TelestrationEntry(
            game=new_game,
            contributor=seed_user,
            turn_index=1,
            entry_type="description",
            text_content=prompt,
        )
        db.session.add(new_game)
        db.session.add(first_entry)
        changed = True
    if changed:
        db.session.commit()


def _telestrations_storage_dir() -> Path:
    configured = current_app.config.get("TELESTRATIONS_STORAGE_PATH")
    base = Path(configured) if configured else Path(current_app.instance_path) / "telestrations"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return base


def _compress_image_to_jpeg(data: bytes, target_size_kb: int = 500) -> Tuple[bytes, str]:
    """Convert any image format to compressed JPEG under target size.
    
    Args:
        data: Raw image data bytes
        target_size_kb: Target maximum file size in KB (default: 500KB)
    
    Returns:
        Tuple of (compressed_jpeg_data, jpeg_mime_type)
    """
    try:
        # Open the image from bytes
        image = Image.open(io.BytesIO(data))
        
        # Convert to RGB if necessary (for transparency, CMYK, etc.)
        if image.mode not in ('RGB', 'L'):
            image = image.convert('RGB')
        
        # Start with high quality and reduce if needed
        quality = 95
        target_size_bytes = target_size_kb * 1024
        
        while quality > 20:  # Don't go below 20% quality
            output_buffer = io.BytesIO()
            image.save(output_buffer, format='JPEG', quality=quality, optimize=True)
            
            if output_buffer.tell() <= target_size_bytes:
                output_buffer.seek(0)
                return output_buffer.getvalue(), "image/jpeg"
            
            # Reduce quality and try again
            quality -= 10
        
        # If still too large at minimum quality, resize the image
        if output_buffer.tell() > target_size_bytes:
            width, height = image.size
            scale_factor = 0.8
            
            while output_buffer.tell() > target_size_bytes and scale_factor > 0.3:
                new_width = int(width * scale_factor)
                new_height = int(height * scale_factor)
                resized_image = image.resize((new_width, new_height), Image.Resampling.LANCZOS)
                
                output_buffer = io.BytesIO()
                resized_image.save(output_buffer, format='JPEG', quality=75, optimize=True)
                
                if output_buffer.tell() <= target_size_bytes:
                    break
                    
                scale_factor -= 0.1
        
        output_buffer.seek(0)
        return output_buffer.getvalue(), "image/jpeg"
        
    except Exception as e:
        # If conversion fails, raise an exception to be handled by the caller
        raise ValueError(f"Failed to compress image to JPEG: {str(e)}")


def _convert_heic_to_jpeg(data: bytes) -> Tuple[bytes, str]:
    """Convert HEIC image data to JPEG format.
    
    Returns:
        Tuple of (converted_jpeg_data, jpeg_mime_type)
    """
    # Use the general compression function for HEIC files
    return _compress_image_to_jpeg(data)


def _store_telestration_image(data: bytes, mime_type: str, game_id: int, turn_index: int) -> str:
    # Compress all images to JPEG format under 500KB
    try:
        data, mime_type = _compress_image_to_jpeg(data)
    except ValueError as e:
        raise ValueError(f"Could not process image: {str(e)}")
    
    # All images are now JPEG after compression
    extension = ".jpg"
    token = secrets.token_hex(8)
    filename = f"{game_id:04d}-{turn_index:02d}-{token}{extension}"
    storage_path = _telestrations_storage_dir() / filename
    with storage_path.open("wb") as handle:
        handle.write(data)
    return filename


def _notify_telestration_completed(game: TelestrationGame) -> None:
    participants: List[User] = []
    seen_ids: Set[int] = set()
    for entry in game.entries:
        if entry.contributor_id in seen_ids or entry.contributor is None:
            continue
        participants.append(entry.contributor)
        seen_ids.add(entry.contributor_id)
    link = url_for("main.telestrations_hall_of_fame")
    _create_alert(
        game.creator,
        participants,
        "One of your telestrations games wrapped up! See how the chain ended.",
        title="Telestrations complete",
        category="telestrations",
        payload={"game_id": game.id, "url": link},
    )


def _submit_reaction_game(game, manager):
    multiplier = _activate_game_context(game.key)
    if not request.is_json:
        return jsonify({"error": "Expected JSON payload."}), 400
    data = request.get_json(silent=True) or {}
    token = data.get("token")
    duration = data.get("duration")
    try:
        duration_value = float(duration)
    except (TypeError, ValueError):
        duration_value = -1.0
    try:
        payload = manager.load_token(token or "")
    except BadSignature:
        return jsonify({"error": "Invalid attempt."}), 400
    if payload.get("game") != game.key:
        return jsonify({"error": "Mismatched attempt."}), 400
    elapsed = max(0.0, duration_value)
    max_time = float(game.params.get("max_time", 1.0) or 1.0)
    base_reward = float(game.params.get("base_reward", 5.0) or 5.0)
    if elapsed <= 0.0:
        return jsonify({"error": "Too early."}), 400
    if max_time <= 0:
        factor = 1.0
    else:
        factor = max(0.0, min(1.0, (max_time - elapsed) / max_time))
    payout = round(max(0.0, base_reward * factor * multiplier), 2)
    if payout > 0:
        record_transaction(current_user, payout, f"{game.name} reaction win")
    message = f"Reaction time: {elapsed:.3f}s. Payout: {payout:.2f} credits."
    return jsonify({"category": "success" if payout > 0 else "info", "message": message, "payout": payout})


def _submit_among_us_task(game, manager):
    multiplier = _activate_game_context(game.key)
    if not request.is_json:
        return jsonify({"error": "Expected JSON payload."}), 400
    data = request.get_json(silent=True) or {}
    token = data.get("token")
    try:
        payload = manager.load_token(token or "")
    except BadSignature:
        return jsonify({"error": "Invalid attempt."}), 400
    if payload.get("game") != game.key:
        return jsonify({"error": "Mismatched attempt."}), 400

    task_type = payload.get("task")
    duration = data.get("duration")
    try:
        duration_value = float(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration_value = None

    base_reward = float(game.params.get("base_reward", 5.0) or 5.0)
    if task_type == "swipe_card":
        ideal = float(game.params.get("ideal_time", 1.2) or 1.2)
        tolerance = max(0.1, float(game.params.get("tolerance", 0.6) or 0.6))
        if duration_value is None or duration_value <= 0:
            return jsonify({"error": "Swipe not detected."}), 400
        deviation = abs(duration_value - ideal)
        if deviation > tolerance:
            return jsonify({"error": "Card rejected. Too fast or too slow."}), 400
        factor = max(0.2, 1.0 - deviation / tolerance)
        payout = round(max(0.0, base_reward * factor * multiplier), 2)
        if payout > 0:
            record_transaction(current_user, payout, f"{game.name} success")
        message = f"Swipe speed {duration_value:.2f}s. Payout: {payout:.2f} credits."
        return jsonify({"category": "success" if payout > 0 else "info", "message": message, "payout": payout})

    if task_type == "prime_shields":
        if duration_value is None or duration_value <= 0:
            return jsonify({"error": "Toggle the shields first."}), 400
        efficiency = max(0.3, min(1.0, 3.5 / max(duration_value, 0.3)))
        payout = round(max(0.0, base_reward * efficiency * multiplier), 2)
        if payout > 0:
            record_transaction(current_user, payout, f"{game.name} shields")
        message = f"Shields primed in {duration_value:.2f}s. Payout: {payout:.2f} credits."
        return jsonify({"category": "success" if payout > 0 else "info", "message": message, "payout": payout})

    if task_type == "align_engine":
        try:
            value = float(data.get("value"))
        except (TypeError, ValueError):
            return jsonify({"error": "Missing alignment value."}), 400
        target = float(payload.get("target", 50.0))
        precision = float(payload.get("precision", game.params.get("precision", 3.0) or 3.0))
        delta = abs(value - target)
        if delta > precision:
            return jsonify({"error": "Alignment off target."}), 400
        if duration_value is None or duration_value <= 0:
            duration_value = 1.0
        efficiency = max(0.4, min(1.0, 2.5 / max(duration_value, 0.4)))
        payout = round(max(0.0, base_reward * efficiency * multiplier), 2)
        if payout > 0:
            record_transaction(current_user, payout, f"{game.name} engines")
        message = (
            f"Thrusters aligned within {delta:.2f} units in {duration_value:.2f}s. "
            f"Payout: {payout:.2f} credits."
        )
        return jsonify({"category": "success" if payout > 0 else "info", "message": message, "payout": payout})

    return jsonify({"error": "Unknown task."}), 400


@bp.route("/games/telestrations/status")
@login_required
def telestrations_status():
    manager = get_games_manager()
    game = manager.get_game("telestrations") if manager else None
    params = game.params if game else None
    _ensure_seeded_telestrations_games(params)
    count = _telestrations_active_query().count()
    return jsonify({"active_games": count})


@bp.route("/games/telestrations/session/<int:game_id>", methods=["GET", "POST"])
@login_required
def telestrations_play(game_id: int):
    game = TelestrationGame.query.get_or_404(game_id)
    if not game.is_active() and request.method == "GET":
        flash("This telestrations game has wrapped up. Visit the hall of fame to see the results.", "info")
        return redirect(url_for("main.telestrations_hall_of_fame"))

    last_entry = game.entries[-1] if game.entries else None
    expected_type = "image" if game.turns_taken % 2 == 1 else "description"
    user_has_played = any(entry.contributor_id == current_user.id for entry in game.entries)
    can_contribute = game.is_active() and not user_has_played

    if request.method == "POST":
        if not can_contribute:
            flash("This round can't accept another contribution from you.", "warning")
            return redirect(url_for("main.play_game", game_key="telestrations"))

        # Use SELECT FOR UPDATE to lock the game row and prevent race conditions
        try:
            locked_game = TelestrationGame.query.with_for_update().get(game_id)
            if locked_game is None:
                flash("Game not found.", "error")
                return redirect(url_for("main.play_game", game_key="telestrations"))
            
            # Re-check if game is still active after locking
            if not locked_game.is_active():
                flash("This game has already been completed.", "info")
                return redirect(url_for("main.telestrations_hall_of_fame"))
            
            # Re-check if user has already played after locking
            user_has_played_locked = any(entry.contributor_id == current_user.id for entry in locked_game.entries)
            if user_has_played_locked:
                flash("You have already contributed to this game.", "warning")
                return redirect(url_for("main.play_game", game_key="telestrations"))

            next_turn = locked_game.turns_taken + 1
            new_entry: Optional[TelestrationEntry] = None
            
            # Recalculate expected type based on locked game state
            expected_type = "image" if locked_game.turns_taken % 2 == 1 else "description"
            
            if expected_type == "description":
                description = (request.form.get("description") or "").strip()
                if not description:
                    flash("Add a brief description before submitting.", "warning")
                    return redirect(url_for("main.telestrations_play", game_id=game.id))
                new_entry = TelestrationEntry(
                    game=locked_game,
                    contributor=current_user,
                    turn_index=next_turn,
                    entry_type="description",
                    text_content=description,
                )
            else:
                image_file = request.files.get("image")
                if image_file is None or not image_file.filename:
                    flash("Upload a quick sketch or snapshot for this clue.", "warning")
                    return redirect(url_for("main.telestrations_play", game_id=game.id))
                data = image_file.read()
                if not data:
                    flash("We couldn't read that image. Try again.", "warning")
                    return redirect(url_for("main.telestrations_play", game_id=game.id))
                if len(data) > 5_000_000:
                    flash("Images must be smaller than 5MB.", "warning")
                    return redirect(url_for("main.telestrations_play", game_id=game.id))
                mime_type = image_file.mimetype or "image/png"
                if not mime_type.lower().startswith("image/"):
                    flash("Only image uploads are supported for telestrations rounds.", "warning")
                    return redirect(url_for("main.telestrations_play", game_id=game.id))
                
                try:
                    filename = _store_telestration_image(data, mime_type, locked_game.id, next_turn)
                    # All images are converted to JPEG during processing
                    stored_mime_type = "image/jpeg"
                except ValueError as e:
                    flash(f"Could not process the uploaded image: {str(e)}", "warning")
                    return redirect(url_for("main.telestrations_play", game_id=game.id))
                
                new_entry = TelestrationEntry(
                    game=locked_game,
                    contributor=current_user,
                    turn_index=next_turn,
                    entry_type="image",
                    image_filename=filename,
                    image_mime_type=stored_mime_type,
                )

            if new_entry is None:
                flash("We couldn't record that move.", "error")
                return redirect(url_for("main.telestrations_play", game_id=game.id))

            # Save the entry and update game state atomically
            db.session.add(new_entry)
            locked_game.turns_taken = next_turn
            finished = False
            if locked_game.turns_taken >= locked_game.max_turns:
                finished = True
                locked_game.completed_at = datetime.utcnow()
            
            db.session.commit()
            
            if finished:
                _notify_telestration_completed(locked_game)
                db.session.commit()
                flash("That completed the chain! Everyone has been notified.", "success")
                return redirect(url_for("main.telestrations_hall_of_fame"))
            flash("Thanks for adding to the chain!", "success")
            return redirect(url_for("main.play_game", game_key="telestrations"))
            
        except IntegrityError:
            # Handle the case where another user submitted the same turn
            db.session.rollback()
            flash("Someone else just submitted this turn. Please try another game.", "info")
            return redirect(url_for("main.play_game", game_key="telestrations"))
        except Exception as e:
            # Handle any other database errors
            db.session.rollback()
            flash("An error occurred while saving your submission. Please try again.", "error")
            return redirect(url_for("main.telestrations_play", game_id=game.id))

    turn_index = min(game.turns_taken + 1, game.max_turns)
    return render_template(
        "games/telestrations_play.html",
        game=game,
        last_entry=last_entry,
        expected_type=expected_type,
        can_contribute=can_contribute,
        user_has_played=user_has_played,
        turn_index=turn_index,
    )


@bp.route("/games/telestrations/entries/<int:entry_id>/image")
@login_required
def telestrations_entry_image(entry_id: int):
    entry = TelestrationEntry.query.get_or_404(entry_id)
    if entry.entry_type != "image" or not entry.image_filename:
        abort(404)
    storage_path = _telestrations_storage_dir() / entry.image_filename
    if not storage_path.exists():
        abort(404)
    mimetype = entry.image_mime_type or mimetypes.guess_type(storage_path.name)[0] or "image/octet-stream"
    return send_file(storage_path, mimetype=mimetype, as_attachment=False)


@bp.route("/games/telestrations/hall-of-fame")
@login_required
def telestrations_hall_of_fame():
    games = (
        TelestrationGame.query.filter(TelestrationGame.completed_at.isnot(None))
        .order_by(TelestrationGame.completed_at.desc())
        .all()
    )
    voted_entries: Set[int] = set()
    if getattr(current_user, "id", None):
        voted_entries = {
            vote.entry_id
            for vote in TelestrationUpvote.query.filter_by(voter_id=current_user.id).all()
        }
    return render_template(
        "games/telestrations_hall_of_fame.html",
        games=games,
        voted_entries=voted_entries,
        upvote_reward=_get_telestrations_upvote_reward(),
    )


@bp.route("/games/telestrations/entries/<int:entry_id>/upvote", methods=["POST"])
@login_required
def telestrations_upvote(entry_id: int):
    entry = TelestrationEntry.query.get_or_404(entry_id)
    if entry.contributor_id == current_user.id:
        flash("You can't upvote your own contribution.", "warning")
        return redirect(request.referrer or url_for("main.telestrations_hall_of_fame"))

    existing = TelestrationUpvote.query.filter_by(entry_id=entry.id, voter_id=current_user.id).first()
    if existing is not None:
        flash("You've already upvoted this entry.", "info")
        return redirect(request.referrer or url_for("main.telestrations_hall_of_fame"))

    vote = TelestrationUpvote(entry=entry, voter_id=current_user.id)
    db.session.add(vote)
    reward_base = _get_telestrations_upvote_reward()
    multiplier = _activate_game_context("telestrations")
    reward = round(max(0.0, reward_base * multiplier), 2)
    if reward > 0 and entry.contributor is not None:
        record_transaction(entry.contributor, reward, "Telestrations upvote reward")
    else:
        db.session.commit()
    flash(
        "Thanks for cheering on that clue!" + (f" You tipped the artist {reward:.2f} credits." if reward > 0 else ""),
        "success",
    )
    return redirect(request.referrer or url_for("main.telestrations_hall_of_fame"))


@bp.route("/casino")
@login_required
def casino():
    manager = get_casino_manager()
    AppSetting.set("current_game_context", "casino")
    return render_template(
        "casino.html",
        slots=manager.get_slots(),
        manager=manager,
    )


@bp.route("/casino/slot", methods=["POST"])
@login_required
def play_slot():
    AppSetting.set("current_game_context", "casino")
    wants_json = request.is_json or request.accept_mimetypes.best == "application/json"

    if request.is_json:
        payload = request.get_json(silent=True) or {}
        slot_id = payload.get("slot_id")
        wager = payload.get("wager")
    else:
        slot_id = request.form.get("slot_id")
        wager = request.form.get("wager")

    try:
        wager_value = float(wager) if wager is not None else None
    except (TypeError, ValueError):
        wager_value = None

    if not slot_id or wager_value is None:
        message = "Choose a machine and wager."
        if wants_json:
            return jsonify({"error": message}), 400
        flash(message, "error")
        return redirect(url_for("main.casino"))

    if wager_value <= 0:
        message = "Wager must be positive."
        if wants_json:
            return jsonify({"error": message}), 400
        flash(message, "error")
        return redirect(url_for("main.casino"))

    if current_user.balance < wager_value:
        message = "Insufficient balance for that spin."
        if wants_json:
            return jsonify({"error": message}), 400
        flash(message, "error")
        return redirect(url_for("main.casino"))

    manager = get_casino_manager()
    try:
        result = manager.play_slot(slot_id, wager_value)
        description = f"{result.machine.name} slot spin ({result.outcome})"
        record_transaction(
            current_user,
            result.player_delta,
            description,
            type_="casino",
            commit=False,
        )
        db.session.commit()
        manager.publish_earnings_if_due()

        if wants_json:
            response = {
                "machine": {
                    "key": result.machine.key,
                    "name": result.machine.name,
                    "theme": result.machine.theme,
                },
                "reels": result.reels,
                "outcome": result.outcome,
                "player_delta": result.player_delta,
                "wager": wager_value,
                "balance": round(current_user.balance, 2),
            }
            if result.prize:
                response["prize"] = result.prize.to_dict()
            if result.wins:
                response["wins"] = [win.to_dict() for win in result.wins]
            return jsonify(response)

        row_strings: List[str] = []
        for row in range(3):
            symbols = [result.reels[col][row] for col in range(len(result.reels))]
            row_strings.append(" ".join(symbols))
        reels_display = " / ".join(row_strings)
        if result.wins:
            wins_text = "Wins: " + ", ".join(
                f"{_format_line_label(win)} ({win.prize.label})" for win in result.wins
            )
        else:
            wins_text = ""
        if result.player_delta > 0:
            message = (
                f"{description}: {reels_display} — You won {result.player_delta:.2f} credits! {wins_text}"
            ).strip()
            flash(message, "success")
        elif result.player_delta == 0:
            message = (
                f"{description}: {reels_display} — Broke even. {wins_text}"
            ).strip()
            flash(message, "info")
        else:
            flash(
                f"{description}: {reels_display} — Lost {abs(result.player_delta):.2f} credits.",
                "warning",
            )
    except ValueError as exc:
        db.session.rollback()
        manager.publish_earnings_if_due()
        if wants_json:
            return jsonify({"error": str(exc)}), 400
        flash(str(exc), "error")
    return redirect(url_for("main.casino"))


def _format_line_label(win: "SlotLineWin") -> str:
    labels = {
        "row": ["Top row", "Middle row", "Bottom row"],
        "column": ["Left column", "Center column", "Right column"],
        "diagonal": ["Main diagonal", "Counter diagonal"],
    }
    options = labels.get(win.line_type, [])
    try:
        return options[win.index]
    except IndexError:
        return win.line_type.capitalize()


@bp.route("/casino/blackjack", methods=["POST"])
@login_required
def play_blackjack():
    AppSetting.set("current_game_context", "casino")
    wager = request.form.get("wager", type=float)
    if wager is None:
        flash("Enter a wager for blackjack.", "error")
        return redirect(url_for("main.casino"))
    if wager <= 0:
        flash("Wager must be positive.", "error")
        return redirect(url_for("main.casino"))
    manager = get_casino_manager()
    if current_user.balance < wager:
        flash("Insufficient balance for that hand.", "error")
        return redirect(url_for("main.casino"))
    try:
        result = manager.play_blackjack(wager)
        description = f"Blackjack hand ({result.outcome})"
        record_transaction(
            current_user,
            result.player_delta,
            description,
            type_="casino",
            commit=False,
        )
        db.session.commit()
        player_hand = ", ".join(result.player_cards)
        dealer_hand = ", ".join(result.dealer_cards)
        if result.player_delta > 0:
            flash(
                f"{description}: You {result.outcome}! Player {player_hand} ({result.player_total}) vs Dealer {dealer_hand} ({result.dealer_total}). Won {result.player_delta:.2f} credits.",
                "success",
            )
        elif result.player_delta < 0:
            flash(
                f"{description}: Dealer showed {dealer_hand} ({result.dealer_total}). Lost {abs(result.player_delta):.2f} credits.",
                "warning",
            )
        else:
            flash(
                f"{description}: Push with {player_hand} ({result.player_total}) against {dealer_hand} ({result.dealer_total}).",
                "info",
            )
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
    manager.publish_earnings_if_due()
    return redirect(url_for("main.casino"))


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


@bp.route("/marketplace", methods=["GET", "POST"])
@login_required
def marketplace():
    products = sync_products_from_stock()
    product_map = {product.id: product for product in products}
    visible_products = [product for product in products if product.enabled]

    if request.method == "POST":
        cart_raw = request.form.get("cart", "[]")
        try:
            cart_items = json.loads(cart_raw or "[]")
        except json.JSONDecodeError:
            flash("We couldn't process your cart. Please try again.", "error")
            return redirect(url_for("main.marketplace"))

        selection_counts: dict[int, int] = {}
        for entry in cart_items:
            if not isinstance(entry, dict):
                continue
            try:
                product_id = int(entry.get("id"))
                quantity = int(entry.get("quantity"))
            except (TypeError, ValueError):
                continue
            if quantity <= 0:
                continue
            selection_counts[product_id] = selection_counts.get(product_id, 0) + quantity

        if not selection_counts:
            flash("Add at least one item to your order before checking out.", "warning")
            return redirect(url_for("main.marketplace"))

        selections: list[tuple[Product, int]] = []
        for product_id, quantity in selection_counts.items():
            product = product_map.get(product_id)
            if not product or not product.enabled:
                continue
            if product.stock < quantity:
                flash(f"Not enough stock for {product.name}.", "error")
                return redirect(url_for("main.marketplace"))
            selections.append((product, quantity))

        if not selections:
            flash("Add at least one item to your order before checking out.", "warning")
            return redirect(url_for("main.marketplace"))

        total_cost = 0.0
        pricing_breakdown: dict[int, tuple[list[float], float]] = {}
        for product, quantity in selections:
            inc_pct = get_price_increase_pct(product)
            current_price = product.price
            line_prices: list[float] = []
            for _ in range(quantity):
                line_prices.append(current_price)
                if inc_pct > 0:
                    current_price = current_price * (1 + inc_pct / 100.0)
            subtotal = round(sum(line_prices), 2)
            pricing_breakdown[product.id] = (line_prices, subtotal)
            total_cost += subtotal

        total_cost = round(total_cost, 2)
        if total_cost <= 0:
            flash("This order would not cost anything. Please try again.", "warning")
            return redirect(url_for("main.marketplace"))

        if current_user.balance < total_cost - 1e-6:
            flash("You do not have enough credits for this order.", "error")
            return redirect(url_for("main.marketplace"))

        order = MerchantOrder(user=current_user, total_price=total_cost)
        db.session.add(order)
        db.session.flush()

        for product, quantity in selections:
            product.stock = max(0, (product.stock or 0) - quantity)
            line_prices, subtotal = pricing_breakdown.get(product.id, ([], 0.0))
            avg_price = round(subtotal / quantity, 2) if quantity else 0.0
            item = MerchantOrderItem(
                order=order,
                product=product,
                quantity=quantity,
                unit_price=avg_price,
                subtotal=subtotal,
                pricing_snapshot=[round(price, 4) for price in line_prices],
            )
            db.session.add(item)
            apply_dynamic_price_increase(product, quantity, commit=False)

        charge_txn = record_transaction(
            current_user,
            -total_cost,
            f"Marketplace order #{order.id}",
            type_="purchase",
            commit=False,
        )
        order.charge_transaction = charge_txn

        sender = _merchant_sender()
        message = "Thanks for your purchase!\n\n" + _format_order_lines(order)
        _create_alert(
            sender,
            [current_user],
            message,
            title=f"Marketplace order #{order.id}",
            category="order_receipt",
            payload={"order_id": order.id, "status": "pending", "total": total_cost},
        )

        db.session.commit()
        flash("Order placed! We'll let you know when it's ready.", "success")
        return redirect(url_for("main.marketplace"))

    return render_template("marketplace.html", products=visible_products)


@bp.route("/merchant", methods=["GET", "POST"])
@login_required
def merchant_portal():
    if not current_user.is_merchant:
        abort(403)

    products = sync_products_from_stock()
    pending_orders = (
        MerchantOrder.query.filter_by(status="pending")
        .order_by(MerchantOrder.created_at.asc())
        .all()
    )

    if request.method == "POST":
        action = request.form.get("action")
        if action in {"complete_order", "cancel_order"}:
            try:
                order_id = int(request.form.get("order_id", ""))
            except (TypeError, ValueError):
                flash("Invalid order selection.", "error")
                return redirect(url_for("main.merchant_portal"))
            order = MerchantOrder.query.get(order_id)
            if not order:
                flash("Order not found.", "error")
                return redirect(url_for("main.merchant_portal"))
            if order.status != "pending":
                flash("This order has already been processed.", "info")
                return redirect(url_for("main.merchant_portal"))

            if action == "complete_order":
                order.status = "completed"
                order.completed_at = datetime.utcnow()
                payout = record_transaction(
                    current_user,
                    order.total_price,
                    f"Fulfilled order #{order.id}",
                    counterparty=order.user,
                    type_="sale",
                    commit=False,
                )
                order.payout_transaction = payout
                _create_alert(
                    current_user,
                    [order.user],
                    "Your order is ready!\n\n" + _format_order_lines(order),
                    title=f"Order #{order.id} completed",
                    category="order_update",
                    payload={"order_id": order.id, "status": "completed"},
                )
                db.session.commit()
                flash(f"Order #{order.id} marked completed.", "success")
            else:
                order.status = "cancelled"
                order.cancelled_at = datetime.utcnow()
                for item in order.items:
                    item.product.stock = (item.product.stock or 0) + item.quantity
                    item.product.updated_at = datetime.utcnow()
                refund = record_transaction(
                    order.user,
                    order.total_price,
                    f"Refund for order #{order.id}",
                    counterparty=current_user,
                    type_="refund",
                    commit=False,
                )
                order.refund_transaction = refund
                _create_alert(
                    current_user,
                    [order.user],
                    (
                        f"Your order #{order.id} was cancelled. "
                        f"We've refunded {order.total_price:.2f} credits."
                    ),
                    title=f"Order #{order.id} cancelled",
                    category="order_update",
                    payload={"order_id": order.id, "status": "cancelled"},
                )
                db.session.commit()
                flash(f"Order #{order.id} cancelled and refunded.", "warning")
            return redirect(url_for("main.merchant_portal"))

        product_id = request.form.get("product_id")
        if action in {"update_price", "update_stock", "update_product_increase_pct"} and not product_id:
            flash("Select a product first.", "error")
            return redirect(url_for("main.merchant_portal"))

        if action == "update_price" and product_id:
            product = Product.query.get(int(product_id))
            if not product:
                flash("Product not found.", "error")
            else:
                try:
                    new_price = float(request.form.get("price", product.price))
                except (TypeError, ValueError):
                    flash("Provide a valid price.", "error")
                    return redirect(url_for("main.merchant_portal"))
                update_price(product, new_price)
            return redirect(url_for("main.merchant_portal"))

        if action == "update_stock" and product_id:
            product = Product.query.get(int(product_id))
            if not product:
                flash("Product not found.", "error")
            else:
                try:
                    new_stock = int(request.form.get("stock", product.stock))
                except (TypeError, ValueError):
                    flash("Provide a valid stock quantity.", "error")
                    return redirect(url_for("main.merchant_portal"))
                product.stock = max(0, new_stock)
                product.updated_at = datetime.utcnow()
                db.session.commit()
                flash("Stock updated.", "success")
            return redirect(url_for("main.merchant_portal"))

        if action == "update_product_increase_pct" and product_id:
            try:
                pct = float(request.form.get("increase_pct", ""))
                if pct < 0:
                    raise ValueError("Percentage must be non-negative")
                AppSetting.set(f"product:{int(product_id)}:increase_pct", str(pct))
                flash("Per-product sensitivity saved.", "success")
            except Exception as exc:
                flash(f"Failed to save per-product sensitivity: {exc}", "error")
            return redirect(url_for("main.merchant_portal"))

        return redirect(url_for("main.merchant_portal"))

    return render_template(
        "merchant.html",
        products=products,
        pending_orders=pending_orders,
    )


def update_price(product, new_price, *, commit: bool = True, announce: bool = True):
    product.price = max(0, new_price)
    product.updated_at = datetime.utcnow()
    db.session.add(PriceHistory(product=product, price=product.price))
    if commit:
        db.session.commit()
    if announce:
        flash("Price updated.", "success")


def get_price_increase_pct(product: Product) -> float:
    try:
        return float(
            AppSetting.get(
                f"product:{product.id}:increase_pct",
                AppSetting.get("price_increase_pct", "5.0") or "5.0",
            )
        )
    except Exception:
        return 0.0


def apply_dynamic_price_increase(product: Product, quantity: int, *, commit: bool = True):
    if quantity <= 0:
        if commit:
            db.session.commit()
        return
    inc_pct = get_price_increase_pct(product)
    for _ in range(quantity):
        if inc_pct <= 0:
            break
        new_price = product.price * (1 + inc_pct / 100.0)
        update_price(product, new_price, commit=False, announce=False)
    if commit:
        db.session.commit()


def build_qr_for_user(user):
    handle = user.email.split("@")[0]
    target_url = url_for("main.dashboard", target=handle, _external=True)
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(target_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    data = base64.b64encode(buf.getvalue()).decode("utf-8")
    return f"data:image/png;base64,{data}"


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
        try:
            apply_dynamic_price_increase(product, 1, commit=False)
            db.session.commit()
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
    finalize_due_votes()
    products = Product.query.order_by(Product.name).all()
    securities = Security.query.order_by(Security.symbol.asc()).all()
    users = User.query.order_by(User.name).all()
    price_history = PriceHistory.query.order_by(PriceHistory.timestamp.desc()).limit(50).all()
    recent_transactions = Transaction.query.order_by(Transaction.created_at.desc()).limit(20).all()
    recent_alerts = Alert.query.order_by(Alert.created_at.desc()).limit(10).all()
    open_votes = (
        ShareholderVote.query.filter(ShareholderVote.finalized_at.is_(None))
        .order_by(ShareholderVote.deadline.asc())
        .all()
    )
    recent_votes = (
        ShareholderVote.query.order_by(ShareholderVote.created_at.desc()).limit(5).all()
    )
    stats = build_price_stats(products)
    default_increase = AppSetting.get("price_increase_pct", "5.0")
    # Defaults for per-game settings
    sp_dec = AppSetting.get("game:single_player:decrease_pct", AppSetting.get("game_reward_decrease_pct", "5.0"))
    sp_mult = AppSetting.get("game:single_player:multiplier", "1.0")
    pd_dec = AppSetting.get("game:prisoners:decrease_pct", AppSetting.get("game_reward_decrease_pct", "5.0"))
    pd_mult = AppSetting.get("game:prisoners:multiplier", "1.0")
    casino_manager = get_casino_manager()
    casino_status = casino_manager.get_status()

    # Prepare defaults/preserved values for the shareholder vote form
    now_str = datetime.utcnow().strftime("%Y-%m-%dT%H:%M")
    preserved_form = session.pop("vote_form", None) or {}
    vote_defaults = {
        "security_symbol": preserved_form.get("security_symbol", ""),
        "vote_title": preserved_form.get("vote_title", ""),
        "vote_message": preserved_form.get("vote_message", ""),
        "vote_options": preserved_form.get("vote_options", ""),
        "vote_deadline": preserved_form.get("vote_deadline", now_str),
    }

    return render_template(
        "admin.html",
        products=products,
        price_history=price_history,
        transactions=recent_transactions,
        stats=stats,
        users=users,
        securities=securities,
        price_increase_pct=default_increase,
        sp_dec=sp_dec,
        sp_mult=sp_mult,
        pd_dec=pd_dec,
        pd_mult=pd_mult,
        casino_status=casino_status,
        alerts=recent_alerts,
        open_votes=open_votes,
        recent_votes=recent_votes,
        vote_defaults=vote_defaults,
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


@bp.route("/admin/casino/publish", methods=["POST"])
@login_required
def admin_casino_publish():
    if not current_user.is_admin:
        abort(403)
    manager = get_casino_manager()
    try:
        summary = manager.publish_earnings_if_due(force=True)
        flash(f"Casino earnings publication triggered: {summary}", "success")
    except Exception as exc:
        db.session.rollback()
        flash(f"Casino earnings publication failed: {exc}", "error")
    return redirect(url_for("main.admin_dashboard"))


@bp.route("/admin/alerts", methods=["POST"])
@login_required
def create_alert():
    if not current_user.is_admin:
        abort(403)

    message = (request.form.get("message") or "").strip()
    audience = request.form.get("audience", "all")
    target_handle = (request.form.get("target_handle") or "").strip()

    if not message:
        flash("Alert message cannot be empty.", "error")
        return redirect(url_for("main.admin_dashboard"))

    recipients: List[User] = []
    if audience == "handle":
        try:
            target_user = find_user_by_handle(target_handle)
        except ValueError:
            flash("Multiple users share that handle. Please use their full email instead.", "error")
            return redirect(url_for("main.admin_dashboard"))
        if not target_user:
            flash("Could not find a user with that handle.", "error")
            return redirect(url_for("main.admin_dashboard"))
        recipients = [target_user]
    else:
        recipients = User.query.filter(User.role == Role.PLAYER).all()

    if not recipients:
        flash("No recipients found for that alert.", "error")
        return redirect(url_for("main.admin_dashboard"))

    alert = Alert(creator=current_user, message=message)
    db.session.add(alert)
    db.session.flush()
    for user in recipients:
        receipt = AlertReceipt(alert=alert, user=user)
        db.session.add(receipt)
    db.session.commit()
    flash(
        f"Alert sent to {len(recipients)} player{'s' if len(recipients) != 1 else ''}.",
        "success",
    )
    return redirect(url_for("main.admin_dashboard"))


@bp.route("/admin/votes", methods=["POST"])
@login_required
def create_shareholder_vote():
    if not current_user.is_admin:
        abort(403)
    finalize_due_votes()
    symbol = (request.form.get("security_symbol") or "").strip().upper()
    title = (request.form.get("vote_title") or "").strip()
    message = (request.form.get("vote_message") or "").strip()
    options_raw = request.form.get("vote_options") or ""
    deadline_raw = request.form.get("vote_deadline") or ""

    form_snapshot = {
        "security_symbol": symbol,
        "vote_title": title,
        "vote_message": message,
        "vote_options": options_raw,
        "vote_deadline": deadline_raw,
    }

    if not symbol or not title or not message:
        flash("Security, title, and message are required to start a vote.", "error")
        session["vote_form"] = form_snapshot
        return redirect(url_for("main.admin_dashboard"))

    security = Security.query.get(symbol)
    if not security:
        flash("Could not find that security.", "error")
        session["vote_form"] = form_snapshot
        return redirect(url_for("main.admin_dashboard"))

    try:
        deadline = datetime.strptime(deadline_raw, "%Y-%m-%dT%H:%M")
    except ValueError:
        flash("Please provide a valid deadline.", "error")
        session["vote_form"] = form_snapshot
        return redirect(url_for("main.admin_dashboard"))

    if deadline <= datetime.utcnow():
        flash("Deadline must be in the future.", "error")
        session["vote_form"] = form_snapshot
        return redirect(url_for("main.admin_dashboard"))

    options = [line.strip() for line in options_raw.replace("\r", "").split("\n") if line.strip()]
    if len(options) < 2:
        flash("Provide at least two voting options.", "error")
        session["vote_form"] = form_snapshot
        return redirect(url_for("main.admin_dashboard"))

    vote = ShareholderVote(
        creator=current_user,
        security_symbol=security.symbol,
        title=title,
        message=message,
        deadline=deadline,
    )
    db.session.add(vote)
    db.session.flush()
    for idx, label in enumerate(options):
        option = ShareholderVoteOption(vote=vote, label=label, position=idx)
        db.session.add(option)

    holdings = (
        SecurityHolding.query.filter(
            SecurityHolding.security_symbol == security.symbol,
            SecurityHolding.quantity > 0,
        )
        .all()
    )
    recipients: List[User] = []
    sent_user_ids: Set[int] = set()
    for holding in holdings:
        if holding.user is None or holding.user_id in sent_user_ids:
            continue
        participant = ShareholderVoteParticipant(vote=vote, user=holding.user)
        db.session.add(participant)
        sent_user_ids.add(holding.user_id)
        recipients.append(holding.user)

    payload = {
        "vote_id": vote.id,
        "deadline": deadline.isoformat(),
        "security_symbol": security.symbol,
    }
    alert = _create_alert(
        current_user,
        recipients,
        message,
        title=f"Shareholder vote: {title}",
        category="vote_invite",
        payload=payload,
        vote=vote,
    )
    if alert:
        sent_at = datetime.utcnow()
        for participant in vote.participants:
            if participant.user_id in sent_user_ids:
                participant.alerted_at = sent_at
        db.session.commit()
        flash(
            f"Vote sent to {len(recipients)} shareholder{'s' if len(recipients) != 1 else ''}.",
            "success",
        )
    else:
        db.session.commit()
        flash(
            "Vote created, but no current shareholders were notified.",
            "info",
        )

    # Clear any preserved form data on success
    session.pop("vote_form", None)
    return redirect(url_for("main.admin_dashboard"))


@bp.route("/votes/<int:vote_id>")
@login_required
def view_shareholder_vote(vote_id: int):
    finalize_due_votes()
    vote = ShareholderVote.query.get_or_404(vote_id)
    ballot = ShareholderVoteBallot.query.filter_by(
        vote_id=vote.id, user_id=current_user.id
    ).first()
    holding = SecurityHolding.query.filter_by(
        user_id=current_user.id, security_symbol=vote.security_symbol
    ).first()
    current_shares = float(holding.quantity or 0.0) if holding and holding.quantity else 0.0
    now = datetime.utcnow()
    voting_open = now < vote.deadline and not vote.finalized_at
    if vote.finalized_at and vote.final_results:
        chart_data = vote.final_results
    else:
        chart_data = _compute_vote_snapshot(vote)
    total_shares = chart_data.get("total_shares", 0.0) or 0.0
    return render_template(
        "shareholder_vote.html",
        vote=vote,
        ballot=ballot,
        voting_open=voting_open,
        chart_data=chart_data,
        current_shares=current_shares,
        total_shares=total_shares,
    )


@bp.route("/votes/<int:vote_id>/cast", methods=["POST"])
@login_required
def cast_shareholder_vote(vote_id: int):
    finalize_due_votes()
    vote = ShareholderVote.query.get_or_404(vote_id)
    if vote.finalized_at or datetime.utcnow() >= vote.deadline:
        flash("Voting has closed for this motion.", "error")
        return redirect(url_for("main.view_shareholder_vote", vote_id=vote.id))

    option_id = request.form.get("option_id", type=int)
    option = ShareholderVoteOption.query.filter_by(id=option_id, vote_id=vote.id).first()
    if not option:
        flash("Select a valid voting option.", "error")
        return redirect(url_for("main.view_shareholder_vote", vote_id=vote.id))

    ensure_vote_alerts_for_user(current_user, vote.security_symbol)
    holding = SecurityHolding.query.filter_by(
        user_id=current_user.id, security_symbol=vote.security_symbol
    ).first()
    if not holding or not holding.quantity or holding.quantity <= 0:
        flash("You must hold shares in this security to vote.", "error")
        return redirect(url_for("main.view_shareholder_vote", vote_id=vote.id))

    ballot = ShareholderVoteBallot.query.filter_by(
        vote_id=vote.id, user_id=current_user.id
    ).first()
    if ballot:
        ballot.option = option
        ballot.submitted_at = datetime.utcnow()
    else:
        ballot = ShareholderVoteBallot(vote=vote, user=current_user, option=option)
        db.session.add(ballot)

    participant = ShareholderVoteParticipant.query.filter_by(
        vote_id=vote.id, user_id=current_user.id
    ).first()
    if not participant:
        participant = ShareholderVoteParticipant(vote=vote, user=current_user)
        db.session.add(participant)
    if participant.alerted_at is None:
        participant.alerted_at = datetime.utcnow()

    db.session.commit()
    flash("Vote submitted.", "success")
    return redirect(url_for("main.view_shareholder_vote", vote_id=vote.id))


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
