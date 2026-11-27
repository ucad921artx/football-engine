"""User-configured factor-weighted Dixon-Coles score distribution.

Each category contributes a standalone score matrix. Categories without usable
match data are omitted and the remaining configured weights are renormalized.
"""
from __future__ import annotations

from dataclasses import replace

import numpy as np

from .base import MatchPrediction, TeamRating
from .dixon_coles import DixonColesConfig, DixonColesModel

DEFAULT_FACTOR_WEIGHTS = {
    "strength": 0.55,
    "venue_environment": 0.12,
    "personnel": 0.10,
    "recent_form": 0.08,
    "motivation": 0.11,
    "schedule_fitness": 0.04,
}


def predict_weighted_factors(
    home: TeamRating,
    away: TeamRating,
    config: DixonColesConfig,
    *,
    factor_weights: dict[str, float] | None = None,
    injuries: dict | None = None,
    lineups: dict | None = None,
    matchup_history: dict | None = None,
    motivation: dict | None = None,
    djyy_probs: dict | None = None,
    schedule_available: bool = False,
    motivation_available: bool = False,
    matchup_available: bool = False,
) -> tuple[MatchPrediction, dict[str, float], tuple[str, ...]]:
    """Build and mix factor score matrices using the configured factor shares."""
    weights = factor_weights or DEFAULT_FACTOR_WEIGHTS
    model = DixonColesModel(config)
    matrices: dict[str, np.ndarray] = {}

    # Rating/attack/defence/Elo are the strength block. Remove distinct factors
    # from its copies to avoid counting form, venue, injuries and rest twice.
    h_strength = replace(home, form=0.0, injury=0.0, rest_days=3)
    a_strength = replace(away, form=0.0, injury=0.0, rest_days=3)
    matrices["strength"] = _matrix(model, h_strength, a_strength, neutral=True)

    baseline_home = TeamRating(name=home.name)
    baseline_away = TeamRating(name=away.name)
    matrices["venue_environment"] = _matrix(model, baseline_home, baseline_away, neutral=False)

    if home.form != 0 or away.form != 0:
        h_form = replace(baseline_home, form=home.form)
        a_form = replace(baseline_away, form=away.form)
        matrices["recent_form"] = _matrix(model, h_form, a_form, neutral=True)

    inj = injuries or {}
    h_missing = max(0.0, float(inj.get("home_attackers", 0) or 0))
    a_missing = max(0.0, float(inj.get("away_attackers", 0) or 0))
    # DJYY sometimes provides injured player names/reasons but no positions.
    # In that case use a smaller, capped availability penalty instead of
    # pretending every sidelined player is an attacker.
    h_unknown = max(0.0, float(inj.get("home_count", 0) or 0)) if not h_missing else 0.0
    a_unknown = max(0.0, float(inj.get("away_count", 0) or 0)) if not a_missing else 0.0
    lineup_data = lineups or {}
    h_attackers = lineup_data.get("home_starting_attackers")
    a_attackers = lineup_data.get("away_starting_attackers")
    if h_missing or a_missing or h_unknown or a_unknown or h_attackers is not None or a_attackers is not None:
        h_delta = (max(-0.06, min(0.06, (float(h_attackers) - 3.0) * 0.015)) if h_attackers is not None else 0.0)
        a_delta = (max(-0.06, min(0.06, (float(a_attackers) - 3.0) * 0.015)) if a_attackers is not None else 0.0)
        h_penalty = min(0.04, 0.005 * h_unknown) if h_unknown else min(0.12, 0.04 * h_missing)
        a_penalty = min(0.04, 0.005 * a_unknown) if a_unknown else min(0.12, 0.04 * a_missing)
        h_personnel = replace(baseline_home, attack=max(0.88, min(1.06, 1.0 + h_delta - h_penalty)))
        a_personnel = replace(baseline_away, attack=max(0.88, min(1.06, 1.0 + a_delta - a_penalty)))
        matrices["personnel"] = _matrix(model, h_personnel, a_personnel, neutral=True)

    if schedule_available:
        h_rest = replace(baseline_home, rest_days=max(0, home.rest_days))
        a_rest = replace(baseline_away, rest_days=max(0, away.rest_days))
        matrices["schedule_fitness"] = _matrix(model, h_rest, a_rest, neutral=True)

    # E12: objective standings/competition pressure creates a small independent
    # attacking-intent matrix. Never alias the strength matrix; that would count
    # team quality twice. The multiplier is deliberately bounded at +2.5% xG.
    mot = motivation or {}
    if motivation_available and mot.get("available"):
        h_urgency = max(0.0, min(1.0, float(mot.get("home_urgency", 0) or 0)))
        a_urgency = max(0.0, min(1.0, float(mot.get("away_urgency", 0) or 0)))
        h_motivation = replace(baseline_home, attack=1.0 + 0.025 * h_urgency)
        a_motivation = replace(baseline_away, attack=1.0 + 0.025 * a_urgency)
        matrices["motivation"] = _matrix(model, h_motivation, a_motivation, neutral=True)

    total = sum(max(0.0, float(weights.get(key, 0.0))) for key in matrices)
    if total <= 0:
        matrices = {"strength": matrices["strength"]}
        total = max(0.0, float(weights.get("strength", 1.0))) or 1.0
    used = {key: max(0.0, float(weights.get(key, 0.0))) / total for key in matrices}
    matrix = sum(used[key] * matrices[key] for key in used)
    matrix = np.maximum(matrix, 0.0)
    matrix /= matrix.sum() or 1.0

    home_win = float(np.tril(matrix, -1).sum())
    draw = float(np.trace(matrix))
    away_win = float(np.triu(matrix, 1).sum())
    home_xg = float(sum(i * matrix[i, :].sum() for i in range(matrix.shape[0])))
    away_xg = float(sum(j * matrix[:, j].sum() for j in range(matrix.shape[1])))
    top = sorted(
        ((i, j, float(matrix[i, j])) for i in range(matrix.shape[0]) for j in range(matrix.shape[1])),
        key=lambda row: row[2], reverse=True,
    )[:8]
    prediction = MatchPrediction(
        match_id="",
        home_team=home.name,
        away_team=away.name,
        competition="",
        home_win_prob=round(home_win, 4),
        draw_prob=round(draw, 4),
        away_win_prob=round(away_win, 4),
        home_xg=round(home_xg, 3),
        away_xg=round(away_xg, 3),
        top_scores=[(i, j, round(p, 4)) for i, j, p in top],
        model_name="weighted_factor_dixon_coles",
        confidence=round(max(home_win, draw, away_win), 4),
    )
    # DJYY's pre-match 1X2 estimate is a model input, not a separate fusion
    # channel. Combine it inside the model distribution, leaving the later
    # model/market blend at the configured 80/20.
    if djyy_probs:
        try:
            djyy = np.asarray([
                float(djyy_probs[k]) for k in ("home", "draw", "away")
            ], dtype=float)
            if np.all(np.isfinite(djyy)) and np.all(djyy >= 0) and djyy.sum() > 0:
                djyy /= djyy.sum()
                local = np.asarray([home_win, draw, away_win], dtype=float)
                blended = 0.5 * local + 0.5 * djyy
                prediction.home_win_prob = round(float(blended[0]), 4)
                prediction.draw_prob = round(float(blended[1]), 4)
                prediction.away_win_prob = round(float(blended[2]), 4)
                prediction.confidence = round(float(blended.max()), 4)
        except (TypeError, ValueError):
            pass
    missing = tuple(key for key in DEFAULT_FACTOR_WEIGHTS if key not in matrices)
    return prediction, used, missing


def _matrix(model: DixonColesModel, home: TeamRating, away: TeamRating, *, neutral: bool) -> np.ndarray:
    home_xg, away_xg = model._expected_goals(home, away, is_neutral=neutral)
    return model._score_distribution(home_xg, away_xg)
