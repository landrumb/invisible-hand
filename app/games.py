"""Game configuration management and helpers for lightweight experiences."""
from __future__ import annotations

import hashlib
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from flask import current_app, has_app_context

try:  # Python 3.11+
    import tomllib  # type: ignore[attr-defined]
    TOMLDecodeError = tomllib.TOMLDecodeError  # type: ignore[attr-defined]
except ModuleNotFoundError:  # pragma: no cover - defensive fallback
    import tomli as tomllib  # type: ignore
    TOMLDecodeError = tomllib.TOMLDecodeError  # type: ignore[attr-defined]

from itsdangerous import BadSignature, URLSafeSerializer


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _load_toml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("rb") as handle:
        return tomllib.load(handle)


@dataclass
class GameDefinition:
    key: str
    name: str
    type: str
    description: str
    enabled: bool = True
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TriviaQuestion:
    id: str
    prompt: str
    choices: List[str]
    answer: int
    hash_value: str
    image: Optional[str] = None
    explanation: Optional[str] = None


@dataclass
class TriviaSet:
    key: str
    title: str
    description: Optional[str]
    reward: float
    questions: List[TriviaQuestion] = field(default_factory=list)

    def ordered_pairs_for_user(self, user_hash: int) -> List[tuple[int, TriviaQuestion]]:
        """Return sorted (order value, question) pairs for the given user hash."""

        pairs: List[tuple[int, TriviaQuestion]] = []
        for question in self.questions:
            question_hash = int(question.hash_value, 16)
            order_value = question_hash ^ user_hash
            pairs.append((order_value, question))
        return sorted(pairs, key=lambda item: item[0])


class GamesManager:
    """Loads and exposes lightweight games defined by configuration files."""

    def __init__(self, app, games_path: Path, trivia_path: Path):
        self.app = app
        self.games_path = games_path
        self.trivia_path = trivia_path
        self._games: Dict[str, GameDefinition] = {}
        self._trivia_sets: Dict[str, TriviaSet] = {}
        self._serializer = URLSafeSerializer(app.config.get("SECRET_KEY", "dev"), salt="games")
        self._games_mtime: Optional[float] = None
        self._trivia_mtime: Optional[float] = None
        self._failed_games_mtime: Optional[float] = None
        self._failed_trivia_mtime: Optional[float] = None
        self.reload(force=True)

    # ------------------------------------------------------------------
    # Configuration loading
    def reload(self, *, force: bool = False) -> None:
        if force:
            self._games_mtime = None
            self._trivia_mtime = None
            self._failed_games_mtime = None
            self._failed_trivia_mtime = None
        self._ensure_current(force=force)

    def _ensure_current(self, *, force: bool = False) -> None:
        self._maybe_reload_games(force=force)
        self._maybe_reload_trivia(force=force)

    def _maybe_reload_games(self, *, force: bool = False) -> None:
        current_mtime = self._get_mtime(self.games_path)
        if not force:
            if current_mtime == self._games_mtime:
                return
            if current_mtime is not None and current_mtime == self._failed_games_mtime:
                return
        try:
            data = _load_toml(self.games_path)
        except TOMLDecodeError as error:  # pragma: no cover - depends on toml parser
            self._failed_games_mtime = current_mtime
            self._log_warning("Failed to parse games configuration; keeping previous games.", error)
            return
        except Exception as error:  # pragma: no cover - defensive
            self._failed_games_mtime = current_mtime
            self._log_warning("Error loading games configuration; keeping previous games.", error)
            return

        self._games = self._parse_games(data)
        self._games_mtime = current_mtime
        self._failed_games_mtime = None

    def _maybe_reload_trivia(self, *, force: bool = False) -> None:
        current_mtime = self._get_mtime(self.trivia_path)
        if not force:
            if current_mtime == self._trivia_mtime:
                return
            if current_mtime is not None and current_mtime == self._failed_trivia_mtime:
                return
        try:
            data = _load_toml(self.trivia_path)
        except TOMLDecodeError as error:  # pragma: no cover - depends on toml parser
            self._failed_trivia_mtime = current_mtime
            self._log_warning("Failed to parse trivia configuration; keeping previous sets.", error)
            return
        except Exception as error:  # pragma: no cover - defensive
            self._failed_trivia_mtime = current_mtime
            self._log_warning("Error loading trivia configuration; keeping previous sets.", error)
            return

        self._trivia_sets = self._parse_trivia_sets(data)
        self._trivia_mtime = current_mtime
        self._failed_trivia_mtime = None

    def _parse_games(self, data: Dict[str, Any]) -> Dict[str, GameDefinition]:
        if not isinstance(data, dict):
            return {}
        games: Dict[str, GameDefinition] = {}
        for entry in data.get("games", []):
            if not isinstance(entry, dict):
                continue
            key = str(entry.get("key", "")).strip()
            name = str(entry.get("name", key or "Game")).strip() or "Game"
            type_ = str(entry.get("type", "")).strip()
            description = str(entry.get("description", "")).strip()
            enabled = self._coerce_enabled(entry.get("enabled", True))
            params = {
                k: v
                for k, v in entry.items()
                if k
                not in {
                    "key",
                    "name",
                    "type",
                    "description",
                    "enabled",
                }
            }
            if key and type_:
                games[key] = GameDefinition(
                    key=key,
                    name=name,
                    type=type_,
                    description=description,
                    enabled=enabled,
                    params=params,
                )
        return games

    def _parse_trivia_sets(self, data: Dict[str, Any]) -> Dict[str, TriviaSet]:
        if not isinstance(data, dict):
            return {}
        sets: Dict[str, TriviaSet] = {}
        for entry in data.get("sets", []):
            if not isinstance(entry, dict):
                continue
            key = str(entry.get("key", "")).strip()
            if not key:
                continue
            title = str(entry.get("title", key)).strip() or key
            description = entry.get("description")
            if isinstance(description, str):
                description_value: Optional[str] = description.strip()
            else:
                description_value = None
            reward = _as_float(entry.get("reward", 5.0), 5.0)
            questions: List[TriviaQuestion] = []
            for question_data in entry.get("questions", []):
                if not isinstance(question_data, dict):
                    continue
                qid = str(question_data.get("id", "")).strip() or f"{key}-{len(questions)}"
                prompt = str(question_data.get("prompt", "")).strip()
                choices = question_data.get("choices") or []
                if not isinstance(choices, list):
                    choices = []
                clean_choices = [str(choice) for choice in choices if isinstance(choice, (str, int, float))]
                answer = int(question_data.get("answer", 0))
                image = question_data.get("image")
                image_value = str(image).strip() if isinstance(image, str) and image.strip() else None
                explanation = question_data.get("explanation")
                explanation_value = (
                    str(explanation).strip() if isinstance(explanation, str) and explanation.strip() else None
                )
                if prompt and clean_choices:
                    hash_seed_parts = [
                        key,
                        qid,
                        prompt,
                        "|".join(clean_choices),
                        str(answer),
                        image_value or "",
                        explanation_value or "",
                    ]
                    hash_seed = "::".join(hash_seed_parts).encode("utf-8")
                    question_hash = hashlib.sha256(hash_seed).hexdigest()
                    questions.append(
                        TriviaQuestion(
                            id=qid,
                            prompt=prompt,
                            choices=clean_choices,
                            answer=max(0, min(answer, len(clean_choices) - 1)),
                            hash_value=question_hash,
                            image=image_value,
                            explanation=explanation_value,
                        )
                    )
            if questions:
                sets[key] = TriviaSet(
                    key=key,
                    title=title,
                    description=description_value,
                    reward=max(0.0, reward),
                    questions=questions,
                )
        return sets

    @staticmethod
    def _coerce_enabled(value: Any) -> bool:
        if isinstance(value, str):
            return value.strip().lower() not in {"", "0", "false", "no", "off"}
        if value is None:
            return False
        return bool(value)

    @staticmethod
    def _get_mtime(path: Path) -> Optional[float]:
        try:
            return path.stat().st_mtime
        except FileNotFoundError:
            return None

    def _log_warning(self, message: str, error: Exception) -> None:
        logger = getattr(self.app, "logger", None)
        if hasattr(logger, "warning"):
            logger.warning(message, exc_info=error)

    # ------------------------------------------------------------------
    # Trivia helpers
    def get_trivia_set(self, key: str) -> Optional[TriviaSet]:
        self._ensure_current()
        return self._trivia_sets.get(key)

    # ------------------------------------------------------------------
    # Game helpers
    def list_games(self) -> List[GameDefinition]:
        self._ensure_current()
        return sorted(
            (game for game in self._games.values() if game.enabled),
            key=lambda game: game.name.lower(),
        )

    def get_game(self, key: str) -> Optional[GameDefinition]:
        self._ensure_current()
        game = self._games.get(key)
        if game is None or not game.enabled:
            return None
        return game

    # ------------------------------------------------------------------
    # Token helpers
    def create_token(self, payload: Dict[str, Any]) -> str:
        payload = dict(payload)
        payload.setdefault("_ts", time.time())
        return self._serializer.dumps(payload)

    def load_token(self, token: str) -> Dict[str, Any]:
        data = self._serializer.loads(token)
        if not isinstance(data, dict):
            raise BadSignature("Invalid token payload")
        return data


def get_games_manager() -> GamesManager:
    if has_app_context():
        app = current_app
    else:  # pragma: no cover - fallback for CLI usage
        raise RuntimeError("Games manager requires an app context")

    manager = app.extensions.get("games_manager")
    if isinstance(manager, GamesManager):
        return manager

    config_dir = Path(app.root_path) / "config"
    manager = GamesManager(
        app,
        games_path=config_dir / "games.toml",
        trivia_path=config_dir / "trivia.toml",
    )
    app.extensions["games_manager"] = manager
    return manager


def init_games(app) -> None:
    """Initialize the games manager during application startup."""

    config_dir = Path(app.root_path) / "config"
    manager = GamesManager(
        app,
        games_path=config_dir / "games.toml",
        trivia_path=config_dir / "trivia.toml",
    )
    app.extensions["games_manager"] = manager

