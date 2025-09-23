from datetime import datetime
from enum import Enum
from typing import Union

from flask_login import UserMixin
from sqlalchemy import CheckConstraint, Enum as SqlEnum

from . import db, login_manager


class Role(str, Enum):
    PLAYER = "player"
    MERCHANT = "merchant"
    ADMIN = "admin"

    _LEVELS = {
        "player": 0,
        "merchant": 1,
        "admin": 2,
    }

    def _rank(self) -> int:
        if self is Role.PLAYER:
            return 0
        if self is Role.MERCHANT:
            return 1
        if self is Role.ADMIN:
            return 2
        return -1

    def at_least(self, other: Union["Role", str]) -> bool:
        if not isinstance(other, Role):
            other = Role(other)
        return self._rank() >= other._rank()


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    google_id = db.Column(db.String(255), unique=True, nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False)
    name = db.Column(db.String(255), nullable=False)
    balance = db.Column(db.Float, default=0.0, nullable=False)
    role = db.Column(SqlEnum(Role), default=Role.PLAYER, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    transactions = db.relationship(
        "Transaction",
        foreign_keys="Transaction.user_id",
        primaryjoin="User.id == Transaction.user_id",
        backref=db.backref("user", lazy=True),
        lazy=True,
    )
    security_positions = db.relationship(
        "SecurityHolding",
        backref=db.backref("user", lazy=True),
        lazy=True,
    )
    option_positions = db.relationship(
        "OptionHolding",
        backref=db.backref("user", lazy=True),
        lazy=True,
    )
    future_positions = db.relationship(
        "FutureHolding",
        backref=db.backref("user", lazy=True),
        lazy=True,
    )
    alert_receipts = db.relationship(
        "AlertReceipt",
        backref=db.backref("user", lazy=True),
        lazy=True,
    )

    def get_id(self):
        return str(self.id)

    def has_privilege(self, role: Union[Role, str]) -> bool:
        current_role = self.role if isinstance(self.role, Role) else Role(self.role)
        return current_role.at_least(role)

    @property
    def is_admin(self):
        return self.has_privilege(Role.ADMIN)

    @property
    def is_merchant(self):
        return self.has_privilege(Role.MERCHANT)


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    description = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    counterparty_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    type = db.Column(db.String(50), nullable=False, default="game")

    counterparty = db.relationship("User", foreign_keys=[counterparty_id], lazy=True)


class MoneyRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    requester_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    target_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    message = db.Column(db.String(255), nullable=True)
    status = db.Column(db.String(20), nullable=False, default="pending")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    resolved_at = db.Column(db.DateTime, nullable=True)

    requester = db.relationship("User", foreign_keys=[requester_id], lazy=True)
    target = db.relationship("User", foreign_keys=[target_id], lazy=True)


class ShareholderVote(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    security_symbol = db.Column(db.String(8), db.ForeignKey("security.symbol"), nullable=False)
    creator_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    title = db.Column(db.String(255), nullable=False)
    message = db.Column(db.Text, nullable=False)
    deadline = db.Column(db.DateTime, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    finalized_at = db.Column(db.DateTime, nullable=True)
    final_results = db.Column(db.JSON, nullable=True)

    creator = db.relationship("User", foreign_keys=[creator_id], lazy=True)
    security = db.relationship("Security", lazy=True)
    options = db.relationship(
        "ShareholderVoteOption",
        backref=db.backref("vote", lazy=True),
        cascade="all, delete-orphan",
        order_by="ShareholderVoteOption.position",
        lazy=True,
    )
    ballots = db.relationship(
        "ShareholderVoteBallot",
        backref=db.backref("vote", lazy=True),
        cascade="all, delete-orphan",
        lazy=True,
    )
    participants = db.relationship(
        "ShareholderVoteParticipant",
        backref=db.backref("vote", lazy=True),
        cascade="all, delete-orphan",
        lazy=True,
    )


class ShareholderVoteOption(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    vote_id = db.Column(db.Integer, db.ForeignKey("shareholder_vote.id"), nullable=False)
    label = db.Column(db.String(255), nullable=False)
    position = db.Column(db.Integer, nullable=False, default=0)


class ShareholderVoteBallot(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    vote_id = db.Column(db.Integer, db.ForeignKey("shareholder_vote.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    option_id = db.Column(db.Integer, db.ForeignKey("shareholder_vote_option.id"), nullable=False)
    submitted_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    option = db.relationship("ShareholderVoteOption", lazy=True)
    user = db.relationship("User", lazy=True)

    __table_args__ = (
        db.UniqueConstraint("vote_id", "user_id", name="uq_vote_ballot"),
    )


class ShareholderVoteParticipant(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    vote_id = db.Column(db.Integer, db.ForeignKey("shareholder_vote.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    alerted_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    user = db.relationship("User", lazy=True)

    __table_args__ = (
        db.UniqueConstraint("vote_id", "user_id", name="uq_vote_participant"),
    )


class Alert(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    creator_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    title = db.Column(db.String(255), nullable=True)
    message = db.Column(db.Text, nullable=False)
    category = db.Column(db.String(50), nullable=False, default="message")
    payload = db.Column(db.JSON, nullable=True)
    vote_id = db.Column(db.Integer, db.ForeignKey("shareholder_vote.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    creator = db.relationship("User", foreign_keys=[creator_id], lazy=True)
    recipients = db.relationship(
        "AlertReceipt",
        backref=db.backref("alert", lazy=True),
        cascade="all, delete-orphan",
        lazy=True,
    )


class AlertReceipt(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    alert_id = db.Column(db.Integer, db.ForeignKey("alert.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    read_at = db.Column(db.DateTime, nullable=True)

    __table_args__ = (
        db.UniqueConstraint("alert_id", "user_id", name="uq_alert_receipt"),
    )


class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.String(255), nullable=True)
    price = db.Column(db.Float, nullable=False)
    stock = db.Column(db.Integer, nullable=False, default=0)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    price_history = db.relationship("PriceHistory", backref="product", lazy=True)

    __table_args__ = (CheckConstraint("price >= 0"), CheckConstraint("stock >= 0"),)


class PriceHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False)
    price = db.Column(db.Float, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class Security(db.Model):
    symbol = db.Column(db.String(8), primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text, nullable=False)
    last_price = db.Column(db.Float, nullable=False)
    drift = db.Column(db.Float, nullable=False)
    volatility = db.Column(db.Float, nullable=False)
    mean_reversion = db.Column(db.Float, nullable=False)
    fundamental_value = db.Column(db.Float, nullable=False)
    liquidity = db.Column(db.Float, nullable=False)
    impact = db.Column(db.Float, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    price_history = db.relationship("SecurityPriceHistory", backref="security", lazy=True)

    __table_args__ = (
        CheckConstraint("last_price > 0"),
        CheckConstraint("volatility >= 0"),
        CheckConstraint("liquidity > 0"),
    )


class SecurityPriceHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    security_symbol = db.Column(db.String(8), db.ForeignKey("security.symbol"), nullable=False)
    price = db.Column(db.Float, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class SecurityHolding(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    security_symbol = db.Column(db.String(8), db.ForeignKey("security.symbol"), nullable=False)
    quantity = db.Column(db.Float, nullable=False, default=0.0)
    average_price = db.Column(db.Float, nullable=False, default=0.0)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    security = db.relationship("Security", lazy=True)

    __table_args__ = (
        db.UniqueConstraint("user_id", "security_symbol", name="uq_security_holding"),
    )


class OptionType(str, Enum):
    CALL = "call"
    PUT = "put"


class OptionListing(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    security_symbol = db.Column(db.String(8), db.ForeignKey("security.symbol"), nullable=False)
    option_type = db.Column(SqlEnum(OptionType), nullable=False)
    strike = db.Column(db.Float, nullable=False)
    expiration = db.Column(db.DateTime, nullable=False)
    style = db.Column(db.String(32), nullable=False, default="european")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    security = db.relationship("Security", lazy=True)

    __table_args__ = (CheckConstraint("strike > 0"),)


class OptionHolding(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    listing_id = db.Column(db.Integer, db.ForeignKey("option_listing.id"), nullable=False)
    quantity = db.Column(db.Integer, nullable=False, default=0)
    average_premium = db.Column(db.Float, nullable=False, default=0.0)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    listing = db.relationship("OptionListing", lazy=True)

    __table_args__ = (
        db.UniqueConstraint("user_id", "listing_id", name="uq_option_holding"),
    )


class FutureListing(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    security_symbol = db.Column(db.String(8), db.ForeignKey("security.symbol"), nullable=False)
    delivery_date = db.Column(db.DateTime, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    security = db.relationship("Security", lazy=True)

    __table_args__ = (
        CheckConstraint("delivery_date > created_at"),
    )


class FutureHolding(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    listing_id = db.Column(db.Integer, db.ForeignKey("future_listing.id"), nullable=False)
    quantity = db.Column(db.Integer, nullable=False, default=0)
    entry_price = db.Column(db.Float, nullable=False, default=0.0)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    listing = db.relationship("FutureListing", lazy=True)

    __table_args__ = (
        db.UniqueConstraint("user_id", "listing_id", name="uq_future_holding"),
    )


class QueueEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    user = db.relationship("User", lazy=True)

    __table_args__ = (db.UniqueConstraint("user_id", name="uq_queue_user"),)


class PrisonersMatch(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    player1_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    player2_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    player1_choice = db.Column(db.String(20), nullable=True)
    player2_choice = db.Column(db.String(20), nullable=True)
    status = db.Column(db.String(20), default="waiting", nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    resolved_at = db.Column(db.DateTime, nullable=True)

    player1 = db.relationship("User", foreign_keys=[player1_id])
    player2 = db.relationship("User", foreign_keys=[player2_id])

    def is_participant(self, user):
        return user.id in {self.player1_id, self.player2_id}

    def record_choice(self, user, choice):
        if user.id == self.player1_id:
            self.player1_choice = choice
        elif user.id == self.player2_id:
            self.player2_choice = choice

    def both_choices_made(self):
        return self.player1_choice is not None and self.player2_choice is not None


class AppSetting(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(120), unique=True, nullable=False)
    value = db.Column(db.String(255), nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    @staticmethod
    def get(key: str, default: str | None = None) -> str | None:
        try:
            setting = AppSetting.query.filter_by(key=key).first()
            return setting.value if setting else default
        except Exception:
            # Table may not exist yet; return default
            return default

    @staticmethod
    def set(key: str, value: str) -> None:
        try:
            setting = AppSetting.query.filter_by(key=key).first()
        except Exception:
            # Attempt to create missing tables and retry once
            db.create_all()
            setting = AppSetting.query.filter_by(key=key).first()
        if setting is None:
            setting = AppSetting(key=key, value=value, updated_at=datetime.utcnow())
            db.session.add(setting)
        else:
            setting.value = value
            setting.updated_at = datetime.utcnow()
        db.session.commit()

    @staticmethod
    def delete(key: str) -> None:
        try:
            setting = AppSetting.query.filter_by(key=key).first()
            if setting is not None:
                db.session.delete(setting)
                db.session.commit()
        except Exception:
            # Table may not exist yet; ignore
            return


class TelestrationGame(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    creator_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    prompt = db.Column(db.String(255), nullable=False)
    max_turns = db.Column(db.Integer, nullable=False, default=8)
    turns_taken = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    completed_at = db.Column(db.DateTime, nullable=True)

    creator = db.relationship("User", foreign_keys=[creator_id], lazy=True)
    entries = db.relationship(
        "TelestrationEntry",
        backref=db.backref("game", lazy=True),
        order_by="TelestrationEntry.turn_index",
        cascade="all, delete-orphan",
        lazy=True,
    )

    def is_active(self) -> bool:
        return self.completed_at is None and self.turns_taken < self.max_turns


class TelestrationEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey("telestration_game.id"), nullable=False)
    contributor_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    turn_index = db.Column(db.Integer, nullable=False)
    entry_type = db.Column(db.String(20), nullable=False)
    text_content = db.Column(db.Text, nullable=True)
    image_filename = db.Column(db.String(512), nullable=True)
    image_mime_type = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    contributor = db.relationship("User", foreign_keys=[contributor_id], lazy=True)
    upvotes = db.relationship(
        "TelestrationUpvote",
        backref=db.backref("entry", lazy=True),
        cascade="all, delete-orphan",
        lazy=True,
    )

    __table_args__ = (
        db.UniqueConstraint("game_id", "turn_index", name="uq_telestration_turn"),
    )

    def contributor_prefix(self) -> str:
        if not self.contributor or not self.contributor.email:
            return "unknown"
        return self.contributor.email.split("@", 1)[0]

    def image_available(self) -> bool:
        return self.entry_type == "image" and bool(self.image_filename)

    def upvote_count(self) -> int:
        return len(self.upvotes or [])


class TelestrationUpvote(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    entry_id = db.Column(db.Integer, db.ForeignKey("telestration_entry.id"), nullable=False)
    voter_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    voter = db.relationship("User", foreign_keys=[voter_id], lazy=True)

    __table_args__ = (
        db.UniqueConstraint("entry_id", "voter_id", name="uq_telestration_upvote"),
    )
