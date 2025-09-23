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
except ModuleNotFoundError:  # pragma: no cover - defensive fallback
    import tomli as tomllib  # type: ignore

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
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TriviaQuestion:
    id: str
    prompt: str
    choices: List[str]
    answer: int
    image: Optional[str] = None
    explanation: Optional[str] = None


@dataclass
class TriviaSet:
    key: str
    title: str
    description: Optional[str]
    reward: float
    questions: List[TriviaQuestion] = field(default_factory=list)

    def ordered_for_user(self, user_identifier: str) -> List[TriviaQuestion]:
        """Return questions in a deterministic order based on the user id."""

        def _ordering(question: TriviaQuestion) -> str:
            seed = f"{self.key}:{user_identifier}:{question.id}".encode("utf-8")
            return hashlib.sha256(seed).hexdigest()

        return sorted(self.questions, key=_ordering)


class GamesManager:
    """Loads and exposes lightweight games defined by configuration files."""

    def __init__(self, app, games_path: Path, trivia_path: Path):
        self.app = app
        self.games_path = games_path
        self.trivia_path = trivia_path
        self._games: Dict[str, GameDefinition] = {}
        self._trivia_sets: Dict[str, TriviaSet] = {}
        self._serializer = URLSafeSerializer(app.config.get("SECRET_KEY", "dev"), salt="games")
        self.reload()

    # ------------------------------------------------------------------
    # Configuration loading
    def reload(self) -> None:
        games_data = _load_toml(self.games_path)
        trivia_data = _load_toml(self.trivia_path)

        games: Dict[str, GameDefinition] = {}
        for entry in games_data.get("games", []):
            if not isinstance(entry, dict):
                continue
            key = str(entry.get("key", "")).strip()
            name = str(entry.get("name", key or "Game")).strip() or "Game"
            type_ = str(entry.get("type", "")).strip()
            description = str(entry.get("description", "")).strip()
            params = {
                k: v for k, v in entry.items() if k not in {"key", "name", "type", "description"}
            }
            if key and type_:
                games[key] = GameDefinition(key=key, name=name, type=type_, description=description, params=params)
        self._games = games

        sets: Dict[str, TriviaSet] = {}
        for entry in trivia_data.get("sets", []):
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
                    questions.append(
                        TriviaQuestion(
                            id=qid,
                            prompt=prompt,
                            choices=clean_choices,
                            answer=max(0, min(answer, len(clean_choices) - 1)),
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
        self._trivia_sets = sets

    # ------------------------------------------------------------------
    # Trivia helpers
    def get_trivia_set(self, key: str) -> Optional[TriviaSet]:
        return self._trivia_sets.get(key)

    # ------------------------------------------------------------------
    # Game helpers
    def list_games(self) -> List[GameDefinition]:
        return sorted(self._games.values(), key=lambda game: game.name.lower())

    def get_game(self, key: str) -> Optional[GameDefinition]:
        return self._games.get(key)

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

