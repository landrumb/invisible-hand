"""Casino games, configuration, and earnings publication utilities."""
from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:  # Python 3.11+
    import tomllib  # type: ignore[attr-defined]
except ModuleNotFoundError:  # pragma: no cover - defensive fallback
    import tomli as tomllib  # type: ignore

from flask import current_app, has_app_context
from sqlalchemy.orm import joinedload

from . import db, get_nyc_now
from .models import AppSetting, Security, SecurityHolding, Transaction, User


PENDING_KEY = "casino:pending_profit"
LAST_PUBLISH_KEY = "casino:last_publish_at"
SUMMARY_KEY = "casino:last_publish_summary"
CASINO_SYMBOL = "CT"


@dataclass
class SlotPrize:
    symbol: str
    label: str
    multiplier: float
    image: Optional[str] = None

    def to_dict(self) -> Dict[str, object]:
        return {
            "symbol": self.symbol,
            "label": self.label,
            "multiplier": self.multiplier,
            "image": self.image,
        }


@dataclass
class SlotMachine:
    key: str
    name: str
    theme: str
    prizes: List[SlotPrize]
    payout_rate: float = 0.95

    @property
    def symbols(self) -> List[str]:
        return [prize.symbol for prize in self.prizes]

    def serialize_prizes(self) -> List[Dict[str, object]]:
        return [prize.to_dict() for prize in self.prizes]


@dataclass
class SlotLineWin:
    line_type: str
    index: int
    coordinates: List[Tuple[int, int]]
    prize: SlotPrize
    payout: float

    def to_dict(self) -> Dict[str, object]:
        return {
            "line_type": self.line_type,
            "index": self.index,
            "coordinates": [[col, row] for col, row in self.coordinates],
            "multiplier": self.prize.multiplier,
            "payout": self.payout,
            "prize": self.prize.to_dict(),
        }


@dataclass
class SlotSpinResult:
    machine: SlotMachine
    reels: List[List[str]]
    outcome: str
    player_delta: float
    wager: float
    total_winnings: float
    prize: Optional[SlotPrize] = None
    wins: Optional[List[SlotLineWin]] = None


@dataclass
class BlackjackResult:
    player_cards: List[str]
    dealer_cards: List[str]
    player_total: int
    dealer_total: int
    outcome: str
    player_delta: float


class CasinoManager:
    """Orchestrates casino games and dividend logic for Casino Technologies."""

    publish_interval = timedelta(minutes=20)

    def __init__(self, app, config_path: Path):
        self.app = app
        self.config_path = config_path
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.slots: Dict[str, SlotMachine] = {}
        self.blackjack_min_bet: float = 5.0
        self.blackjack_max_bet: float = 250.0
        self.blackjack_payout: float = 1.5
        self._pending_profit: float = 0.0
        self._last_publish: Optional[datetime] = None
        self.reload_config()
        self._load_state()

    # ------------------------------------------------------------------
    # Configuration
    def reload_config(self) -> None:
        defaults = self._default_slots()
        data = {}
        try:
            with self.config_path.open("rb") as handle:
                data = tomllib.load(handle)
        except FileNotFoundError:
            data = {}

        payouts_cfg = data.get("payouts", {})
        default_slot_rate = float(payouts_cfg.get("default_slot", 0.95))
        slot_overrides = data.get("slots", {})

        slots: Dict[str, SlotMachine] = {}
        for key, slot in defaults.items():
            override = slot_overrides.get(key, {}) if isinstance(slot_overrides, dict) else {}
            payout = float(override.get("payout", default_slot_rate))
            name = str(override.get("name", slot.name))
            theme = str(override.get("theme", slot.theme))
            base_prizes = [
                SlotPrize(
                    symbol=prize.symbol,
                    label=prize.label,
                    multiplier=prize.multiplier,
                    image=prize.image,
                )
                for prize in slot.prizes
            ]
            override_symbols = override.get("symbols")
            if isinstance(override_symbols, list):
                updated = []
                for index, prize in enumerate(base_prizes):
                    try:
                        symbol_override = override_symbols[index]
                    except IndexError:
                        symbol_override = prize.symbol
                    if isinstance(symbol_override, str) and symbol_override.strip():
                        updated.append(
                            SlotPrize(
                                symbol=symbol_override.strip(),
                                label=prize.label,
                                multiplier=prize.multiplier,
                                image=prize.image,
                            )
                        )
                    else:
                        updated.append(prize)
                base_prizes = updated

            override_prizes = override.get("prizes")
            prizes = base_prizes
            if isinstance(override_prizes, list):
                custom: List[SlotPrize] = []
                for entry in override_prizes:
                    if not isinstance(entry, dict):
                        continue
                    symbol = str(entry.get("symbol", "")).strip()
                    if not symbol:
                        continue
                    label = str(entry.get("label", symbol)).strip() or symbol
                    try:
                        multiplier = float(entry.get("multiplier", 1.0))
                    except (TypeError, ValueError):
                        multiplier = 1.0
                    image = entry.get("image")
                    if isinstance(image, str) and image.strip():
                        image_value = image.strip()
                    else:
                        image_value = None
                    custom.append(
                        SlotPrize(
                            symbol=symbol,
                            label=label,
                            multiplier=max(0.0, multiplier),
                            image=image_value,
                        )
                    )
                if custom:
                    prizes = custom
            slots[key] = SlotMachine(
                key=key,
                name=name,
                theme=theme,
                prizes=prizes,
                payout_rate=max(0.0, min(0.999, payout)),
            )
        self.slots = slots

        blackjack_cfg = data.get("blackjack", {}) if isinstance(data.get("blackjack"), dict) else {}
        try:
            self.blackjack_min_bet = max(0.01, float(blackjack_cfg.get("min_bet", 5.0)))
        except (TypeError, ValueError):
            self.blackjack_min_bet = 5.0
        try:
            self.blackjack_max_bet = max(
                self.blackjack_min_bet,
                float(blackjack_cfg.get("max_bet", 250.0)),
            )
        except (TypeError, ValueError):
            self.blackjack_max_bet = max(self.blackjack_min_bet, 250.0)
        try:
            self.blackjack_payout = max(1.0, float(blackjack_cfg.get("blackjack_payout", 1.5)))
        except (TypeError, ValueError):
            self.blackjack_payout = 1.5

    def _default_slots(self) -> Dict[str, SlotMachine]:
        return {
            "nova": SlotMachine(
                key="nova",
                name="Nebula Nights",
                theme="Cosmic auroras and shimmering stardust",
                prizes=[
                    SlotPrize("🌠", "Shooting Stars", 1.6),
                    SlotPrize("🪐", "Orbiting Planets", 1.4),
                    SlotPrize("✨", "Stellar Glints", 1.2),
                    SlotPrize("☄️", "Comet Flash", 1.0),
                    SlotPrize("💫", "Gravity Loop", 0.8),
                    SlotPrize("🌌", "Galactic Glow", 0.6),
                ],
            ),
            "neon": SlotMachine(
                key="neon",
                name="Neon Mirage",
                theme="Cyberpunk skylines flickering in synthwave hues",
                prizes=[
                    SlotPrize("🔮", "Crystal Visions", 1.7),
                    SlotPrize("💎", "Diamond Pulse", 1.5),
                    SlotPrize("🎰", "Jackpot Echo", 1.3),
                    SlotPrize("🛸", "Hover Cab", 1.1),
                    SlotPrize("💡", "Neon Spark", 0.9),
                    SlotPrize("🪙", "Token Toss", 0.7),
                ],
            ),
            "abyss": SlotMachine(
                key="abyss",
                name="Abyssal Fortune",
                theme="Deep-sea treasures guarded by luminous creatures",
                prizes=[
                    SlotPrize("🐚", "Pearl Cache", 1.8),
                    SlotPrize("🪸", "Coral Bloom", 1.5),
                    SlotPrize("🐙", "Octo Whirl", 1.2),
                    SlotPrize("🦑", "Ink Trail", 1.0),
                    SlotPrize("🔱", "Tidal Crest", 0.85),
                    SlotPrize("🐠", "School Swirl", 0.65),
                ],
            ),
        }

    # ------------------------------------------------------------------
    # State helpers
    def _load_state(self) -> None:
        with self.app.app_context():
            self._pending_profit = self._get_setting_float(PENDING_KEY, 0.0)
            self._last_publish = self._get_setting_datetime(LAST_PUBLISH_KEY)

    def _get_setting(self, key: str) -> Optional[AppSetting]:
        try:
            return AppSetting.query.filter_by(key=key).first()
        except Exception:
            db.create_all()
            return AppSetting.query.filter_by(key=key).first()

    def _set_setting(self, key: str, value: str, commit: bool = False) -> None:
        setting = self._get_setting(key)
        if setting is None:
            setting = AppSetting(key=key, value=value, updated_at=datetime.utcnow())
            db.session.add(setting)
        else:
            setting.value = value
            setting.updated_at = datetime.utcnow()
        if commit:
            db.session.commit()

    def _get_setting_float(self, key: str, default: float = 0.0) -> float:
        setting = self._get_setting(key)
        if not setting:
            return default
        try:
            return float(setting.value)
        except (TypeError, ValueError):
            return default

    def _get_setting_datetime(self, key: str) -> Optional[datetime]:
        setting = self._get_setting(key)
        if not setting or not setting.value:
            return None
        try:
            return datetime.fromisoformat(setting.value)
        except ValueError:
            return None

    def _set_pending_profit(self, value: float, commit: bool = False) -> None:
        self._pending_profit = value
        self._set_setting(PENDING_KEY, f"{value:.6f}", commit=commit)

    def _set_last_publish(self, when: datetime, summary: str, commit: bool = False) -> None:
        self._last_publish = when
        self._set_setting(LAST_PUBLISH_KEY, when.isoformat(), commit=False)
        self._set_setting(SUMMARY_KEY, summary, commit=commit)

    # ------------------------------------------------------------------
    # Accessors
    def get_slots(self) -> List[SlotMachine]:
        return list(self.slots.values())

    def get_slot(self, key: str) -> SlotMachine:
        slot = self.slots.get(key)
        if not slot:
            raise ValueError("Slot machine not found.")
        return slot

    def get_status(self) -> Dict[str, Optional[object]]:
        summary_setting = self._get_setting(SUMMARY_KEY)
        next_publish = None
        if self._last_publish:
            next_publish = self._last_publish + self.publish_interval
        return {
            "pending_profit": self._pending_profit,
            "last_publish_at": self._last_publish,
            "next_publish_at": next_publish,
            "last_summary": summary_setting.value if summary_setting else None,
        }

    # ------------------------------------------------------------------
    # Games
    def play_slot(self, key: str, wager: float) -> SlotSpinResult:
        if wager <= 0:
            raise ValueError("Wager must be positive.")

        slot = self.get_slot(key)
        symbol_choices = slot.symbols or ["❓"]
        prize_lookup: Dict[str, SlotPrize] = {prize.symbol: prize for prize in slot.prizes}

        reels: List[List[str]] = []
        for _ in range(3):
            column = [random.choice(symbol_choices) for _ in range(3)]
            reels.append(column)

        win_probability = max(0.0, min(1.0, slot.payout_rate))
        wins = self._evaluate_slot_grid(reels, prize_lookup, wager)
        if not wins and random.random() < win_probability and slot.prizes:
            # Force a winning line to better align with the configured payout rate
            target_prize = random.choice(slot.prizes)
            line_definitions = self._slot_line_definitions()
            line_type, index, coordinates = random.choice(line_definitions)
            for col, row in coordinates:
                reels[col][row] = target_prize.symbol
            wins = self._evaluate_slot_grid(reels, prize_lookup, wager)

        total_payout = sum(win.payout for win in wins)
        total_winnings = round(total_payout, 2)
        player_delta = round(total_payout - wager, 2)

        if player_delta > 0:
            outcome = "win"
        elif player_delta == 0:
            outcome = "push"
        else:
            outcome = "lose"

        prize = wins[0].prize if wins else None
        self._set_pending_profit(self._pending_profit - player_delta, commit=False)
        return SlotSpinResult(
            machine=slot,
            reels=reels,
            outcome=outcome,
            player_delta=player_delta,
            wager=wager,
            total_winnings=total_winnings,
            prize=prize,
            wins=wins,
        )

    def _evaluate_slot_grid(
        self,
        reels: List[List[str]],
        prize_lookup: Dict[str, SlotPrize],
        wager: float,
    ) -> List[SlotLineWin]:
        wins: List[SlotLineWin] = []
        line_definitions = self._slot_line_definitions()
        for line_type, index, coordinates in line_definitions:
            symbols = [reels[col][row] for col, row in coordinates]
            if len(set(symbols)) != 1:
                continue
            symbol = symbols[0]
            prize = prize_lookup.get(symbol)
            if not prize:
                continue
            payout = round(wager * prize.multiplier, 2)
            wins.append(
                SlotLineWin(
                    line_type=line_type,
                    index=index,
                    coordinates=coordinates,
                    prize=prize,
                    payout=payout,
                )
            )
        return wins

    @staticmethod
    def _slot_line_definitions() -> List[Tuple[str, int, List[Tuple[int, int]]]]:
        lines: List[Tuple[str, int, List[Tuple[int, int]]]] = []
        for row in range(3):
            coords = [(col, row) for col in range(3)]
            lines.append(("row", row, coords))
        for col in range(3):
            coords = [(col, row) for row in range(3)]
            lines.append(("column", col, coords))
        lines.append(("diagonal", 0, [(0, 0), (1, 1), (2, 2)]))
        lines.append(("diagonal", 1, [(2, 0), (1, 1), (0, 2)]))
        return lines

    def play_blackjack(self, wager: float) -> BlackjackResult:
        if wager <= 0:
            raise ValueError("Wager must be positive.")
        if wager < self.blackjack_min_bet or wager > self.blackjack_max_bet:
            raise ValueError(
                f"Blackjack wager must be between {self.blackjack_min_bet:.2f} and {self.blackjack_max_bet:.2f}."
            )

        deck = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]

        def draw_card() -> str:
            return random.choice(deck)

        def hand_value(cards: List[str]) -> int:
            total = 0
            aces = 0
            for card in cards:
                if card == "A":
                    aces += 1
                    total += 11
                elif card in {"J", "Q", "K"}:
                    total += 10
                else:
                    total += int(card)
            while total > 21 and aces:
                total -= 10
                aces -= 1
            return total

        player_cards = [draw_card(), draw_card()]
        dealer_cards = [draw_card(), draw_card()]
        player_total = hand_value(player_cards)
        dealer_total = hand_value(dealer_cards)
        natural_player = player_total == 21
        natural_dealer = dealer_total == 21

        if not natural_player:
            while player_total < 17:
                player_cards.append(draw_card())
                player_total = hand_value(player_cards)
                if player_total > 21:
                    break

        if player_total <= 21:
            while dealer_total < 17:
                dealer_cards.append(draw_card())
                dealer_total = hand_value(dealer_cards)

        if player_total > 21:
            outcome = "bust"
            player_delta = round(-wager, 2)
        elif natural_player and not natural_dealer:
            payout = wager * self.blackjack_payout
            player_delta = round(payout - wager, 2)
            outcome = "blackjack"
        elif dealer_total > 21 or player_total > dealer_total:
            player_delta = round(wager, 2)
            outcome = "win"
        elif player_total == dealer_total:
            player_delta = 0.0
            outcome = "push"
        else:
            player_delta = round(-wager, 2)
            outcome = "lose"

        self._set_pending_profit(self._pending_profit - player_delta, commit=False)
        return BlackjackResult(
            player_cards=player_cards,
            dealer_cards=dealer_cards,
            player_total=player_total,
            dealer_total=dealer_total,
            outcome=outcome,
            player_delta=player_delta,
        )

    # ------------------------------------------------------------------
    # Earnings publication
    def publish_earnings_if_due(self, *, force: bool = False) -> Optional[str]:
        now = datetime.utcnow()
        with self._lock:
            if force or self._should_publish(now):
                return self._publish_earnings(now)
        return None

    def _should_publish(self, now: datetime) -> bool:
        if self._last_publish is None:
            return True
        return now - self._last_publish >= self.publish_interval

    def _publish_earnings(self, now: datetime) -> str:
        profit = self._pending_profit
        summary = "No casino activity this period."
        try:
            if abs(profit) < 1e-6:
                summary = "Casino held steady; no dividends or buybacks were required."
            elif profit > 0:
                summary = self._distribute_dividends(profit)
            else:
                summary = self._cover_losses(-profit)
            self._set_pending_profit(0.0, commit=False)
            self._set_last_publish(now, summary, commit=False)
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        finally:
            db.session.remove()
        return summary

    def _distribute_dividends(self, profit: float) -> str:
        dividend_pool = profit * 0.5
        holdings = (
            SecurityHolding.query.filter(
                SecurityHolding.security_symbol == CASINO_SYMBOL,
                SecurityHolding.quantity > 0,
            )
            .options(joinedload(SecurityHolding.user))
            .all()
        )
        total_shares = sum(float(h.quantity or 0.0) for h in holdings)
        if total_shares <= 0:
            return "Casino earned profit but no outstanding shares existed; retained earnings."
        per_share = dividend_pool / total_shares
        for holding in holdings:
            qty = float(holding.quantity or 0.0)
            if qty <= 0:
                continue
            amount = qty * per_share
            if abs(amount) < 1e-6:
                continue
            user = holding.user or User.query.get(holding.user_id)
            if not user:
                continue
            user.balance += amount
            txn = Transaction(
                user_id=user.id,
                amount=amount,
                description="Casino Technologies dividend",
                type="dividend",
            )
            db.session.add(txn)
        db.session.flush()
        return (
            f"Casino distributed {dividend_pool:.2f} credits in dividends at {per_share:.4f} per share."
        )

    def _cover_losses(self, loss: float) -> str:
        security = Security.query.get(CASINO_SYMBOL)
        if not security:
            return "Casino recorded a loss but CT security is unavailable."
        price = max(0.01, security.last_price)
        quantity = loss / price
        simulator = getattr(current_app, "market_simulator", None)
        if simulator is not None:
            simulator.apply_order_impact(CASINO_SYMBOL, -quantity)
        db.session.flush()
        return (
            f"Casino covered losses of {loss:.2f} credits by issuing approximately {quantity:.2f} CT shares."
        )

    # ------------------------------------------------------------------
    # Lifecycle
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="casino-manager", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1)

    def _run(self) -> None:  # pragma: no cover - background thread
        with self.app.app_context():
            while not self._stop.is_set():
                start = time.monotonic()
                try:
                    self.publish_earnings_if_due()
                except Exception:
                    db.session.rollback()
                elapsed = time.monotonic() - start
                delay = max(30.0, 60.0 - elapsed)
                time.sleep(delay)


# ----------------------------------------------------------------------
# Application helpers

def init_casino(app) -> CasinoManager:
    config_path = Path(app.root_path) / "config" / "casino.toml"
    manager = CasinoManager(app, config_path)

    @app.before_request
    def _ensure_casino_running() -> None:  # pragma: no cover - background thread
        if not getattr(app, "_casino_thread_started", False):
            manager.start()
            setattr(app, "_casino_thread_started", True)

    import atexit

    atexit.register(manager.stop)
    app.casino_manager = manager
    return manager


def get_casino_manager() -> CasinoManager:
    if has_app_context():
        return current_app.casino_manager
    raise RuntimeError("No active Flask application context.")

