from __future__ import annotations

import copy
import math
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Dict, Iterable, Optional

import tomllib

from . import db


_CURRENT_GAME_CONTEXT: ContextVar[Optional[str]] = ContextVar(
    "economy_game_context", default=None
)


def _default_config() -> dict:
    return {
        "pricing": {
            "purchase_impact": 0.0125,
            "cross_cooling": 0.0015,
            "default_liquidity": 100.0,
            "min_price": 0.1,
            "max_price": 10_000.0,
            "liquidity_overrides": {},
        },
        "payouts": {
            "payout_impact": 0.008,
            "cross_recovery": 0.001,
            "default_liquidity": 8.0,
            "min_multiplier": 0.05,
            "max_multiplier": 5.0,
            "baseline_multiplier": 1.0,
            "liquidity_overrides": {},
            "tracked_games": ["single_player", "prisoners"],
        },
    }


def _ensure_directory(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _render_config(config: dict) -> str:
    pricing = config.get("pricing", {})
    payouts = config.get("payouts", {})
    lines: list[str] = []
    lines.append("[pricing]")
    lines.append(f"purchase_impact = {pricing.get('purchase_impact', 0.0)}")
    lines.append(f"cross_cooling = {pricing.get('cross_cooling', 0.0)}")
    lines.append(f"default_liquidity = {pricing.get('default_liquidity', 0.0)}")
    lines.append(f"min_price = {pricing.get('min_price', 0.0)}")
    lines.append(f"max_price = {pricing.get('max_price', 0.0)}")
    liquidity_overrides = pricing.get("liquidity_overrides", {}) or {}
    if liquidity_overrides:
        lines.append("")
        lines.append("[pricing.liquidity_overrides]")
        for key, value in liquidity_overrides.items():
            lines.append(f"{key} = {float(value)}")

    lines.append("")
    lines.append("[payouts]")
    lines.append(f"payout_impact = {payouts.get('payout_impact', 0.0)}")
    lines.append(f"cross_recovery = {payouts.get('cross_recovery', 0.0)}")
    lines.append(f"default_liquidity = {payouts.get('default_liquidity', 0.0)}")
    lines.append(f"min_multiplier = {payouts.get('min_multiplier', 0.0)}")
    lines.append(f"max_multiplier = {payouts.get('max_multiplier', 0.0)}")
    lines.append(f"baseline_multiplier = {payouts.get('baseline_multiplier', 1.0)}")
    tracked = payouts.get("tracked_games", []) or []
    tracked_repr = ", ".join(f'"{game}"' for game in tracked)
    lines.append(f"tracked_games = [{tracked_repr}]")
    payout_overrides = payouts.get("liquidity_overrides", {}) or {}
    if payout_overrides:
        lines.append("")
        lines.append("[payouts.liquidity_overrides]")
        for key, value in payout_overrides.items():
            lines.append(f"{key} = {float(value)}")
    lines.append("")
    return "\n".join(lines).strip() + "\n"


def _inverse_ratio(value: float, liquidity: float) -> float:
    if liquidity <= 0:
        return 0.0
    denominator = max(abs(value), 1e-6)
    return liquidity / denominator


def _clamp(value: float, minimum: float, maximum: float) -> float:
    if value < minimum:
        return minimum
    if value > maximum:
        return maximum
    return value


@dataclass(frozen=True)
class PurchaseQuoteContext:
    first_price: float
    step_factor: float
    min_price: float
    max_price: float


@dataclass
class EconomyManager:
    config_path: Path
    _config: dict = field(default_factory=_default_config)
    _lock: Lock = field(default_factory=Lock)
    _config_mtime: Optional[float] = None

    def _load_config_locked(self) -> None:
        try:
            stat = self.config_path.stat()
        except FileNotFoundError:
            _ensure_directory(self.config_path)
            self._write_config_locked()
            return

        if self._config_mtime is not None and stat.st_mtime <= self._config_mtime:
            return

        try:
            with self.config_path.open("rb") as fh:
                data = tomllib.load(fh)
        except tomllib.TOMLDecodeError:
            # Keep existing config if new version fails to parse
            return

        config = _default_config()
        pricing = config["pricing"]
        payouts = config["payouts"]

        raw_pricing = data.get("pricing", {}) or {}
        raw_payouts = data.get("payouts", {}) or {}

        pricing.update(
            {
                "purchase_impact": float(raw_pricing.get("purchase_impact", pricing["purchase_impact"])),
                "cross_cooling": float(raw_pricing.get("cross_cooling", pricing["cross_cooling"])),
                "default_liquidity": float(
                    raw_pricing.get("default_liquidity", pricing["default_liquidity"])
                ),
                "min_price": float(raw_pricing.get("min_price", pricing["min_price"])),
                "max_price": float(raw_pricing.get("max_price", pricing["max_price"])),
            }
        )
        liquidity_overrides = raw_pricing.get("liquidity_overrides", {}) or {}
        pricing["liquidity_overrides"] = {
            str(key): float(value)
            for key, value in liquidity_overrides.items()
            if _is_number(value)
        }

        payouts.update(
            {
                "payout_impact": float(raw_payouts.get("payout_impact", payouts["payout_impact"])),
                "cross_recovery": float(
                    raw_payouts.get("cross_recovery", payouts["cross_recovery"])
                ),
                "default_liquidity": float(
                    raw_payouts.get("default_liquidity", payouts["default_liquidity"])
                ),
                "min_multiplier": float(
                    raw_payouts.get("min_multiplier", payouts["min_multiplier"])
                ),
                "max_multiplier": float(
                    raw_payouts.get("max_multiplier", payouts["max_multiplier"])
                ),
                "baseline_multiplier": float(
                    raw_payouts.get("baseline_multiplier", payouts["baseline_multiplier"])
                ),
            }
        )
        payout_overrides = raw_payouts.get("liquidity_overrides", {}) or {}
        payouts["liquidity_overrides"] = {
            str(key): float(value)
            for key, value in payout_overrides.items()
            if _is_number(value)
        }
        tracked_games = raw_payouts.get("tracked_games", payouts.get("tracked_games", [])) or []
        payouts["tracked_games"] = [str(entry) for entry in tracked_games]

        self._config = config
        self._config_mtime = stat.st_mtime

    def _write_config_locked(self) -> None:
        _ensure_directory(self.config_path)
        content = _render_config(self._config)
        self.config_path.write_text(content, encoding="utf-8")
        try:
            self._config_mtime = self.config_path.stat().st_mtime
        except FileNotFoundError:
            self._config_mtime = None

    def get_config(self) -> dict:
        with self._lock:
            self._load_config_locked()
            return copy.deepcopy(self._config)

    def update_config(self, *, pricing: dict | None = None, payouts: dict | None = None) -> None:
        with self._lock:
            self._load_config_locked()
            if pricing:
                self._config["pricing"].update(pricing)
            if payouts:
                self._config["payouts"].update(payouts)
            self._write_config_locked()

    # Pricing adjustments -------------------------------------------------
    def apply_purchase(self, product, quantity: int) -> list[dict[str, float]]:
        if quantity <= 0:
            return []
        from .models import Product

        with self._lock:
            self._load_config_locked()
            pricing = self._config["pricing"]

        liquidity = self._product_liquidity(product, pricing)
        base_price = max(product.price or 0.0, 0.0)
        ratio_inverse = _inverse_ratio(base_price or 1.0, liquidity)
        increase = pricing["purchase_impact"] * ratio_inverse
        increase = min(increase, 5.0)
        factor = (1.0 + increase) ** quantity
        new_price = _clamp(base_price * factor, pricing["min_price"], pricing["max_price"])
        adjustments: dict[int, dict[str, float]] = {}
        if self._update_product_price(product, new_price):
            product_id = int(getattr(product, "id", 0) or 0)
            adjustments[product_id] = {
                "product_id": product_id,
                "before": float(base_price),
                "after": float(new_price),
            }

        cross = pricing.get("cross_cooling", 0.0)
        if cross <= 0:
            return list(adjustments.values())

        others: Iterable[Product] = (
            Product.query.filter(Product.id != product.id, Product.enabled.is_(True)).all()
        )
        for other in others:
            other_liq = self._product_liquidity(other, pricing)
            other_ratio_inverse = _inverse_ratio(other.price or 1.0, other_liq)
            decrease = min(cross * quantity * other_ratio_inverse, 0.95)
            factor = max(0.0, 1.0 - decrease)
            new_value = _clamp(
                (other.price or 0.0) * factor,
                pricing["min_price"],
                pricing["max_price"],
            )
            before_value = float(other.price or 0.0)
            if self._update_product_price(other, new_value):
                key = int(getattr(other, "id", 0) or 0)
                entry = adjustments.get(key)
                if entry:
                    entry["after"] = float(new_value)
                else:
                    adjustments[key] = {
                        "product_id": key,
                        "before": before_value,
                        "after": float(new_value),
                    }

        return list(adjustments.values())

    def get_purchase_quote_context(self, product) -> PurchaseQuoteContext:
        first_price, step_factor, min_price, max_price = self._purchase_quote_parameters(product)
        return PurchaseQuoteContext(first_price=first_price, step_factor=step_factor, min_price=min_price, max_price=max_price)

    def quote_purchase_prices(self, product, quantity: int) -> list[float]:
        if quantity <= 0:
            return []
        first_price, step_factor, min_price, max_price = self._purchase_quote_parameters(product)
        current = first_price
        prices: list[float] = []
        for _ in range(quantity):
            prices.append(round(current, 4))
            next_price = _clamp(current * step_factor, min_price, max_price)
            if not math.isfinite(next_price):
                next_price = current
            current = next_price
        return prices

    def _purchase_quote_parameters(self, product) -> tuple[float, float, float, float]:
        with self._lock:
            self._load_config_locked()
            pricing = self._config["pricing"]
        liquidity = self._product_liquidity(product, pricing)
        base_price = max(product.price or 0.0, 0.0)
        ratio_inverse = _inverse_ratio(base_price or 1.0, liquidity)
        increase = min(pricing["purchase_impact"] * ratio_inverse, 5.0)
        step_factor = max(0.0, 1.0 + increase)
        min_price = pricing["min_price"]
        max_price = pricing["max_price"]
        first_price = round(_clamp(base_price, min_price, max_price), 4)
        return first_price, step_factor, min_price, max_price

    def _product_liquidity(self, product, pricing: dict) -> float:
        overrides = pricing.get("liquidity_overrides", {}) or {}
        key_id = str(getattr(product, "id", ""))
        key_name = getattr(product, "name", "")
        if key_id in overrides:
            return max(float(overrides[key_id]), 1e-6)
        lowered = key_name.lower() if isinstance(key_name, str) else ""
        if lowered and lowered in overrides:
            return max(float(overrides[lowered]), 1e-6)
        base_stock = getattr(product, "base_stock", None)
        if base_stock is not None and base_stock > 0:
            return float(base_stock)
        return float(pricing.get("default_liquidity", 1.0))

    def _update_product_price(self, product, new_price: float) -> bool:
        if not math.isfinite(new_price):
            return False
        current = product.price or 0.0
        if abs(current - new_price) < 1e-4:
            return False
        from .models import PriceHistory

        product.price = new_price
        product.updated_at = datetime.utcnow()
        history = PriceHistory(product=product, price=new_price)
        db.session.add(history)
        return True

    def reverse_adjustments(self, adjustments: Iterable[dict[str, float]]) -> None:
        entries: dict[int, tuple[float, float]] = {}
        for entry in adjustments or []:
            try:
                product_id = int(entry.get("product_id"))
            except (TypeError, ValueError):
                continue
            if product_id <= 0:
                continue
            try:
                before = float(entry.get("before"))
                after = float(entry.get("after"))
            except (TypeError, ValueError):
                continue
            if not math.isfinite(before) or not math.isfinite(after):
                continue
            entries[product_id] = (before, after)

        if not entries:
            return

        from .models import Product

        products = Product.query.filter(Product.id.in_(entries.keys())).all()
        product_map = {product.id: product for product in products}

        with self._lock:
            self._load_config_locked()
            pricing = self._config["pricing"]
            for product_id, (before, after) in entries.items():
                product = product_map.get(product_id)
                if not product:
                    continue
                current = product.price or 0.0
                if abs(after) < 1e-6:
                    continue
                factor = before / after
                new_price = _clamp(current * factor, pricing["min_price"], pricing["max_price"])
                self._update_product_price(product, new_price)

    # Game payout adjustments ---------------------------------------------
    def activate_game_context(self, key: str) -> float:
        key = str(key)
        _CURRENT_GAME_CONTEXT.set(key)
        with self._lock:
            self._load_config_locked()
            self._ensure_tracked_game_locked(key)
            payouts = self._config["payouts"]
        return self.get_game_multiplier(key, payouts)

    def current_game_context(self) -> Optional[str]:
        return _CURRENT_GAME_CONTEXT.get()

    def get_game_multiplier(self, key: str, payouts_cfg: Optional[dict] = None) -> float:
        from .models import AppSetting

        if payouts_cfg is None:
            with self._lock:
                self._load_config_locked()
                payouts_cfg = self._config["payouts"]
        stored = AppSetting.get(f"economy:game:{key}:multiplier", None)
        if stored is None:
            return float(payouts_cfg.get("baseline_multiplier", 1.0))
        try:
            value = float(stored)
        except (TypeError, ValueError):
            return float(payouts_cfg.get("baseline_multiplier", 1.0))
        return _clamp(
            value,
            float(payouts_cfg.get("min_multiplier", 0.0)),
            float(payouts_cfg.get("max_multiplier", 10.0)),
        )

    def _set_game_multiplier(self, key: str, value: float, payouts_cfg: dict) -> None:
        from .models import AppSetting

        value = _clamp(
            float(value),
            float(payouts_cfg.get("min_multiplier", 0.0)),
            float(payouts_cfg.get("max_multiplier", 10.0)),
        )
        current = self.get_game_multiplier(key, payouts_cfg)
        if abs(current - value) < 1e-4:
            return
        AppSetting.set(f"economy:game:{key}:multiplier", f"{value:.6f}")

    def record_game_payout(self, amount: float, game_key: Optional[str] = None) -> None:
        if amount <= 0:
            return
        with self._lock:
            self._load_config_locked()
            payouts = self._config["payouts"]
        key = str(game_key or self.current_game_context() or "")
        if not key:
            return
        self._ensure_tracked_game(key, payouts)
        liquidity = self._game_liquidity(key, payouts)
        inverse = _inverse_ratio(amount, liquidity)
        primary_decrease = min(payouts["payout_impact"] * inverse, 0.95)
        current = self.get_game_multiplier(key, payouts)
        primary_multiplier = current * max(0.0, 1.0 - primary_decrease)
        self._set_game_multiplier(key, primary_multiplier, payouts)

        cross = payouts.get("cross_recovery", 0.0)
        if cross <= 0:
            return

        for other_key in self._tracked_games(payouts):
            if other_key == key:
                continue
            other_multiplier = self.get_game_multiplier(other_key, payouts)
            other_liq = self._game_liquidity(other_key, payouts)
            other_inverse = _inverse_ratio(other_multiplier or 1.0, other_liq)
            increase = min(cross * other_inverse, 0.5)
            adjusted = other_multiplier * (1.0 + increase)
            self._set_game_multiplier(other_key, adjusted, payouts)

    def get_game_multipliers(self) -> Dict[str, float]:
        with self._lock:
            self._load_config_locked()
            payouts = self._config["payouts"]
            keys = list(self._tracked_games(payouts))
        return {key: self.get_game_multiplier(key, payouts) for key in keys}

    def _ensure_tracked_game_locked(self, key: str) -> None:
        payouts = self._config["payouts"]
        if key not in payouts.get("tracked_games", []):
            payouts.setdefault("tracked_games", []).append(key)
            self._write_config_locked()

    def _ensure_tracked_game(self, key: str, payouts: dict) -> None:
        with self._lock:
            self._load_config_locked()
            if key not in self._config["payouts"].get("tracked_games", []):
                self._config["payouts"].setdefault("tracked_games", []).append(key)
                self._write_config_locked()

    def _tracked_games(self, payouts: dict) -> Iterable[str]:
        return payouts.get("tracked_games", []) or []

    def _game_liquidity(self, key: str, payouts: dict) -> float:
        overrides = payouts.get("liquidity_overrides", {}) or {}
        if key in overrides:
            return max(float(overrides[key]), 1e-6)
        return float(payouts.get("default_liquidity", 1.0))


def _is_number(value) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


_economy_manager: Optional[EconomyManager] = None


def init_economy(app) -> None:
    global _economy_manager
    if _economy_manager is not None:
        return
    path_value = app.config.get(
        "ECONOMY_CONFIG_PATH",
        Path(app.root_path) / "config" / "economy.toml",
    )
    config_path = Path(path_value)
    _economy_manager = EconomyManager(config_path=config_path)
    # Ensure initial config exists
    _economy_manager.get_config()


def get_economy_manager() -> Optional[EconomyManager]:
    return _economy_manager

