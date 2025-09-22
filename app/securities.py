"""Market simulation, pricing, and trading utilities for the arcade securities desk."""
from __future__ import annotations

import math
import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from flask import current_app, has_app_context

from . import db
from .models import (
    FutureHolding,
    FutureListing,
    OptionHolding,
    OptionListing,
    OptionType,
    Security,
    SecurityHolding,
    SecurityPriceHistory,
)

SECONDS_IN_YEAR = 365 * 24 * 60 * 60
MIN_PRICE = 0.01


@dataclass
class SecurityConfig:
    symbol: str
    name: str
    description: str
    initial_price: float
    drift: float
    volatility: float
    mean_reversion: float
    fundamental_value: float
    liquidity: float
    impact: float
    options_tenors: List[int]
    options_strike_multipliers: List[float]
    futures_tenors: List[int]


@dataclass
class TradeResult:
    symbol: str
    quantity: float
    price: float
    notional: float
    action: str
    description: str
    cash_delta: float


class MarketSimulator:
    """Controls stochastic price evolution and derivative listings."""

    def __init__(self, app, config_path: Path):
        self.app = app
        self.config_path = config_path
        self.interval = 5.0
        self.risk_free_rate = 0.01
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.configs: Dict[str, SecurityConfig] = {}
        self.reload_config()

    # ------------------------------------------------------------------
    # Configuration loading
    def reload_config(self) -> None:
        raw = _load_toml(self.config_path)
        market_cfg = raw.get("market", {})
        self.interval = float(market_cfg.get("update_interval_seconds", 5))
        risk_cfg = raw.get("risk", {})
        self.risk_free_rate = float(risk_cfg.get("risk_free_rate", 0.01))

        securities_cfg = raw.get("securities", {})
        configs: Dict[str, SecurityConfig] = {}
        for symbol, payload in securities_cfg.items():
            configs[symbol] = SecurityConfig(
                symbol=symbol,
                name=str(payload.get("name", symbol)),
                description=str(payload.get("description", "")),
                initial_price=float(payload.get("initial_price", 100.0)),
                drift=float(payload.get("drift", 0.0)),
                volatility=max(0.0, float(payload.get("volatility", 0.2))),
                mean_reversion=float(payload.get("mean_reversion", 0.0)),
                fundamental_value=float(payload.get("fundamental_value", 100.0)),
                liquidity=max(1e-6, float(payload.get("liquidity", 1.0))),
                impact=max(0.0, float(payload.get("impact", 0.01))),
                options_tenors=[int(x) for x in payload.get("options_tenors", [7, 30])],
                options_strike_multipliers=[
                    float(x) for x in payload.get("options_strike_multipliers", [0.9, 1.0, 1.1])
                ],
                futures_tenors=[int(x) for x in payload.get("futures_tenors", [30])],
            )
        self.configs = configs

    # ------------------------------------------------------------------
    # Lifecycle hooks
    def ensure_initialized(self) -> None:
        with self.app.app_context():
            db.create_all()
            now = datetime.utcnow()
            for symbol, config in self.configs.items():
                security = Security.query.get(symbol)
                if not security:
                    security = Security(
                        symbol=symbol,
                        name=config.name,
                        description=config.description,
                        last_price=config.initial_price,
                        drift=config.drift,
                        volatility=config.volatility,
                        mean_reversion=config.mean_reversion,
                        fundamental_value=config.fundamental_value,
                        liquidity=config.liquidity,
                        impact=config.impact,
                        updated_at=now,
                    )
                    db.session.add(security)
                    db.session.add(
                        SecurityPriceHistory(
                            security_symbol=symbol, price=config.initial_price, timestamp=now
                        )
                    )
                else:
                    security.name = config.name
                    security.description = config.description
                    security.drift = config.drift
                    security.volatility = config.volatility
                    security.mean_reversion = config.mean_reversion
                    security.fundamental_value = config.fundamental_value
                    security.liquidity = config.liquidity
                    security.impact = config.impact
                    if security.last_price <= 0:
                        security.last_price = config.initial_price
                        db.session.add(
                            SecurityPriceHistory(
                                security_symbol=symbol,
                                price=config.initial_price,
                                timestamp=now,
                            )
                        )
                self._ensure_option_listings(security, config, now)
                self._ensure_future_listings(security, config, now)
            db.session.commit()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="market-simulator", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1)

    # ------------------------------------------------------------------
    def _run(self) -> None:
        with self.app.app_context():
            while not self._stop.is_set():
                start = time.monotonic()
                self.step()
                elapsed = time.monotonic() - start
                delay = max(0.0, self.interval - elapsed)
                time.sleep(delay)

    def step(self) -> None:
        with self._lock:
            now = datetime.utcnow()
            for symbol, config in self.configs.items():
                security = Security.query.get(symbol)
                if not security:
                    continue
                price = max(MIN_PRICE, security.last_price)
                dt = max(self.interval, 1.0) / SECONDS_IN_YEAR
                mean_reversion_term = config.mean_reversion * (config.fundamental_value - price) / max(price, 1e-6)
                drift = config.drift + mean_reversion_term
                sigma = max(0.0, config.volatility)
                shock = random.gauss(0.0, 1.0)
                exponent = (drift - 0.5 * sigma * sigma) * dt + sigma * math.sqrt(dt) * shock
                new_price = max(MIN_PRICE, price * math.exp(exponent))
                security.last_price = new_price
                security.updated_at = now
                db.session.add(
                    SecurityPriceHistory(security_symbol=symbol, price=new_price, timestamp=now)
                )
            db.session.commit()

    # ------------------------------------------------------------------
    def price_option(self, listing: OptionListing) -> float:
        security = listing.security
        if not security:
            return 0.0
        spot = max(MIN_PRICE, security.last_price)
        strike = listing.strike
        time_to_expiry = max(0.0, (listing.expiration - datetime.utcnow()).total_seconds()) / SECONDS_IN_YEAR
        if time_to_expiry <= 0:
            if listing.option_type is OptionType.CALL:
                return max(0.0, spot - strike)
            return max(0.0, strike - spot)
        sigma = max(MIN_PRICE, security.volatility)
        rate = self.risk_free_rate
        return _black_scholes(spot, strike, time_to_expiry, rate, sigma, listing.option_type)

    def price_future(self, listing: FutureListing) -> float:
        security = listing.security
        if not security:
            return 0.0
        spot = max(MIN_PRICE, security.last_price)
        time_to_delivery = max(
            0.0, (listing.delivery_date - datetime.utcnow()).total_seconds() / SECONDS_IN_YEAR
        )
        rate = self.risk_free_rate
        return spot * math.exp(rate * time_to_delivery)

    def apply_order_impact(self, symbol: str, signed_quantity: float) -> None:
        if signed_quantity == 0:
            return
        config = self.configs.get(symbol)
        if not config:
            return
        ctx = None
        if not has_app_context():
            ctx = self.app.app_context()
            ctx.push()
        try:
            security = Security.query.get(symbol)
            if not security:
                return
            adjustment = 1.0 + config.impact * signed_quantity / max(config.liquidity, 1e-6)
            adjustment = max(0.5, min(1.5, adjustment))
            new_price = max(MIN_PRICE, security.last_price * adjustment)
            now = datetime.utcnow()
            security.last_price = new_price
            security.updated_at = now
            db.session.add(
                SecurityPriceHistory(security_symbol=symbol, price=new_price, timestamp=now)
            )
            db.session.flush()
        finally:
            if ctx is not None:
                ctx.pop()

    # ------------------------------------------------------------------
    def _ensure_option_listings(self, security: Security, config: SecurityConfig, now: datetime) -> None:
        base_price = max(MIN_PRICE, security.last_price or config.initial_price)
        for tenor in config.options_tenors:
            expiration = (now + timedelta(days=tenor)).replace(second=0, microsecond=0)
            for multiplier in config.options_strike_multipliers:
                strike = round(base_price * multiplier, 2)
                for option_type in (OptionType.CALL, OptionType.PUT):
                    existing = OptionListing.query.filter_by(
                        security_symbol=security.symbol,
                        option_type=option_type,
                        strike=strike,
                        expiration=expiration,
                    ).first()
                    if not existing:
                        db.session.add(
                            OptionListing(
                                security_symbol=security.symbol,
                                option_type=option_type,
                                strike=strike,
                                expiration=expiration,
                            )
                        )

    def _ensure_future_listings(self, security: Security, config: SecurityConfig, now: datetime) -> None:
        for tenor in config.futures_tenors:
            delivery_date = (now + timedelta(days=tenor)).replace(second=0, microsecond=0)
            existing = FutureListing.query.filter_by(
                security_symbol=security.symbol, delivery_date=delivery_date
            ).first()
            if not existing:
                db.session.add(
                    FutureListing(security_symbol=security.symbol, delivery_date=delivery_date)
                )


# ----------------------------------------------------------------------
# Trading helpers

def execute_equity_trade(user, symbol: str, quantity: float) -> TradeResult:
    if quantity == 0:
        raise ValueError("Quantity must be non-zero.")
    security = Security.query.get(symbol)
    if not security:
        raise ValueError("Security not found.")
    quantity = float(quantity)
    price = max(MIN_PRICE, security.last_price)
    notional = price * abs(quantity)
    holding = SecurityHolding.query.filter_by(user_id=user.id, security_symbol=symbol).first()
    if quantity > 0:
        if user.balance < notional:
            raise ValueError("Insufficient balance to buy.")
        if not holding:
            holding = SecurityHolding(user_id=user.id, security_symbol=symbol)
        new_qty = holding.quantity + quantity
        total_cost = holding.quantity * holding.average_price + notional
        holding.quantity = new_qty
        holding.average_price = total_cost / new_qty if new_qty else 0.0
    else:
        if not holding or holding.quantity < abs(quantity):
            raise ValueError("Not enough shares to sell.")
        new_qty = holding.quantity + quantity
        holding.quantity = new_qty
        if new_qty <= 0:
            holding.quantity = 0.0
            holding.average_price = 0.0
    holding.updated_at = datetime.utcnow()
    db.session.add(holding)
    liquidity_scale = max(security.liquidity, 1.0)
    current_app.market_simulator.apply_order_impact(symbol, quantity / liquidity_scale)
    return TradeResult(
        symbol=symbol,
        quantity=quantity,
        price=price,
        notional=notional,
        action="buy" if quantity > 0 else "sell",
        description=("Bought" if quantity > 0 else "Sold") + f" {abs(quantity):.2f} {symbol}",
        cash_delta=-notional if quantity > 0 else notional,
    )


def execute_option_trade(user, listing_id: int, quantity: int) -> TradeResult:
    if quantity == 0:
        raise ValueError("Quantity must be non-zero.")
    listing = OptionListing.query.get(listing_id)
    if not listing:
        raise ValueError("Option listing not found.")
    simulator: MarketSimulator = current_app.market_simulator
    premium = simulator.price_option(listing)
    notional = premium * abs(quantity)
    holding = OptionHolding.query.filter_by(user_id=user.id, listing_id=listing_id).first()
    if quantity > 0:
        if user.balance < notional:
            raise ValueError("Insufficient balance to buy option.")
        if not holding:
            holding = OptionHolding(user_id=user.id, listing_id=listing_id)
        new_qty = holding.quantity + quantity
        total_premium = holding.quantity * holding.average_premium + notional
        holding.quantity = new_qty
        holding.average_premium = total_premium / new_qty if new_qty else 0.0
    else:
        if not holding or holding.quantity < abs(quantity):
            raise ValueError("Not enough contracts to sell.")
        holding.quantity += quantity
        if holding.quantity <= 0:
            holding.quantity = 0
            holding.average_premium = 0.0
    holding.updated_at = datetime.utcnow()
    db.session.add(holding)
    option_delta_sign = 1 if listing.option_type is OptionType.CALL else -1
    signed_pressure = option_delta_sign * quantity
    current_app.market_simulator.apply_order_impact(
        listing.security_symbol, signed_pressure * 0.05
    )
    action = "buy" if quantity > 0 else "sell"
    kind = "call" if listing.option_type is OptionType.CALL else "put"
    description = f"{action.title()} {abs(quantity)} {kind.upper()} {listing.security_symbol} @{listing.strike:.2f}"
    return TradeResult(
        symbol=listing.security_symbol,
        quantity=float(quantity),
        price=premium,
        notional=notional,
        action=action,
        description=description,
        cash_delta=-notional if quantity > 0 else notional,
    )


def execute_future_trade(user, listing_id: int, quantity: int) -> TradeResult:
    if quantity == 0:
        raise ValueError("Quantity must be non-zero.")
    listing = FutureListing.query.get(listing_id)
    if not listing:
        raise ValueError("Future listing not found.")
    simulator: MarketSimulator = current_app.market_simulator
    forward_price = simulator.price_future(listing)
    holding = FutureHolding.query.filter_by(user_id=user.id, listing_id=listing_id).first()
    previous_qty = holding.quantity if holding else 0
    new_qty = previous_qty + quantity
    prev_margin = forward_price * abs(previous_qty) * 0.1
    new_margin = forward_price * abs(new_qty) * 0.1
    margin_delta = new_margin - prev_margin
    if margin_delta > 0 and user.balance < margin_delta:
        raise ValueError("Insufficient balance for margin.")
    if not holding:
        holding = FutureHolding(user_id=user.id, listing_id=listing_id)
    holding.quantity = new_qty
    holding.entry_price = forward_price if new_qty else 0.0
    holding.updated_at = datetime.utcnow()
    db.session.add(holding)
    current_app.market_simulator.apply_order_impact(listing.security_symbol, quantity * 0.1)
    action = "long" if new_qty > 0 else "short" if new_qty < 0 else "flat"
    description = (
        f"Adjusted future position on {listing.security_symbol} by {int(quantity):+d} contract(s)"
    )
    return TradeResult(
        symbol=listing.security_symbol,
        quantity=float(quantity),
        price=forward_price,
        notional=abs(margin_delta),
        action=action,
        description=description,
        cash_delta=-margin_delta,
    )


# ----------------------------------------------------------------------
# Pricing utilities

def _black_scholes(spot: float, strike: float, time_to_expiry: float, rate: float, sigma: float, option_type: OptionType) -> float:
    if spot <= 0 or strike <= 0 or time_to_expiry <= 0 or sigma <= 0:
        if option_type is OptionType.CALL:
            return max(0.0, spot - strike)
        return max(0.0, strike - spot)
    sqrt_t = math.sqrt(time_to_expiry)
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * time_to_expiry) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    if option_type is OptionType.CALL:
        return spot * _norm_cdf(d1) - strike * math.exp(-rate * time_to_expiry) * _norm_cdf(d2)
    else:
        return strike * math.exp(-rate * time_to_expiry) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# ----------------------------------------------------------------------
# Minimal TOML loader (sufficient for the project configuration)

def _load_toml(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Missing securities configuration at {path}")
    data: dict = {}
    current_stack: List[str] = []
    current_section = data
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section_path = line[1:-1].strip()
            if not section_path:
                raise ValueError("Empty section in TOML file.")
            current_stack = section_path.split(".")
            current_section = data
            for part in current_stack:
                current_section = current_section.setdefault(part, {})
            continue
        if "=" not in line:
            raise ValueError(f"Invalid line in TOML: {raw_line}")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        current_section[key] = _parse_toml_value(value)
    return data


def _parse_toml_value(value: str):
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        parts = [part.strip() for part in inner.split(",")]
        return [_parse_toml_value(part) for part in parts]
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    if "." in value or "e" in value.lower():
        try:
            return float(value)
        except ValueError as exc:  # pragma: no cover - defensive
            raise ValueError(f"Unable to parse float from '{value}'") from exc
    try:
        return int(value)
    except ValueError:
        return value


# ----------------------------------------------------------------------
# Application helper

def init_market(app) -> MarketSimulator:
    config_path = Path(app.root_path) / "config" / "securities.toml"
    simulator = MarketSimulator(app, config_path)
    simulator.ensure_initialized()

    @app.before_first_request
    def _start_market_thread() -> None:  # pragma: no cover - background thread
        simulator.start()

    app.market_simulator = simulator
    return simulator


def get_simulator() -> MarketSimulator:
    return current_app.market_simulator
