"""Evidence-based motivation/competition-pressure signals."""
from __future__ import annotations

import re

from engine.sources.team_match import match_team


def build_motivation_signal(
    analysis: dict | None,
    match_context: dict | None,
    home_team: str,
    away_team: str,
) -> dict:
    """Return bounded urgency scores only when table or match stakes are explicit."""
    analysis = analysis or {}
    context = match_context or {}
    rules = context.get("format_rules") or {}
    result = {
        "available": False,
        "home_urgency": 0.0,
        "away_urgency": 0.0,
        "home_reasons": [],
        "away_reasons": [],
        "source": [],
    }

    standings = analysis.get("full_standings") or []
    if standings and rules:
        table = analysis.get("league_table") or {}
        table_names = {
            "home": ((table.get("team_A") or {}).get("total") or {}).get("name") or home_team,
            "away": ((table.get("team_B") or {}).get("total") or {}).get("name") or away_team,
        }
        progress = min(1.0, max(0.0, max((r.get("played", 0) for r in standings), default=0)
                                / max(1.0, float(rules.get("games_per_team") or 1))))
        phase_factor = 0.45 + 0.55 * progress
        for side, name in (("home", home_team), ("away", away_team)):
            team_name = table_names[side]
            row = next((r for r in standings if match_team(name, r.get("team", ""))
                        or match_team(team_name, r.get("team", ""))), None)
            if not row:
                continue
            urgency, reason = _league_urgency(row, standings, rules, phase_factor)
            if urgency > 0:
                result[f"{side}_urgency"] = max(result[f"{side}_urgency"], urgency)
                result[f"{side}_reasons"].append(reason)
                if not any("积分榜" in source for source in result["source"]):
                    result["source"].append("懂球帝完整积分榜")

    round_name = str(context.get("round_name") or "")
    match_title = str(context.get("match_title") or "")
    stage_text = f"{round_name} {match_title}"
    if re.search(r"淘汰|决赛|半决赛|四分之一|1/8|1/4|附加赛|季后赛|Round One|Conference", stage_text, re.I):
        agg = context.get("aggregate_scores") or {}
        home_agg, away_agg = agg.get("home"), agg.get("away")
        aggregate_allowed = rules.get("aggregate_allowed", True)
        if "次回合" in stage_text and aggregate_allowed and home_agg is not None and away_agg is not None:
            diff = int(home_agg) - int(away_agg)
            if diff < 0:
                result["home_urgency"] = max(result["home_urgency"], 0.95)
                result["away_urgency"] = max(result["away_urgency"], 0.55)
                result["home_reasons"].append(f"次回合总比分 {home_agg}-{away_agg} 落后")
                result["away_reasons"].append(f"次回合总比分 {home_agg}-{away_agg} 领先")
            elif diff > 0:
                result["home_urgency"] = max(result["home_urgency"], 0.55)
                result["away_urgency"] = max(result["away_urgency"], 0.95)
                result["home_reasons"].append(f"次回合总比分 {home_agg}-{away_agg} 领先")
                result["away_reasons"].append(f"次回合总比分 {home_agg}-{away_agg} 落后")
            else:
                result["home_urgency"] = max(result["home_urgency"], 0.8)
                result["away_urgency"] = max(result["away_urgency"], 0.8)
                result["home_reasons"].append("次回合总比分持平")
                result["away_reasons"].append("次回合总比分持平")
            result["source"].append("懂球帝次回合总比分")
        else:
            result["home_urgency"] = max(result["home_urgency"], 0.65)
            result["away_urgency"] = max(result["away_urgency"], 0.65)
            label = round_name or match_title or "淘汰赛"
            result["home_reasons"].append(f"赛事阶段：{label}")
            result["away_reasons"].append(f"赛事阶段：{label}")
            result["source"].append("懂球帝赛事阶段")

    result["home_urgency"] = round(min(1.0, result["home_urgency"]), 3)
    result["away_urgency"] = round(min(1.0, result["away_urgency"]), 3)
    result["available"] = bool(result["source"])
    return result


def _league_urgency(row: dict, standings: list[dict], rules: dict, phase_factor: float) -> tuple[float, str]:
    rank = int(row.get("rank") or 0)
    points = int(row.get("points") or 0)
    size = len(standings)
    if not rank or size < 6:
        return 0.0, ""
    pressure = 0.0
    reason = ""
    for zone in rules.get("race_zones") or []:
        start, end = int(zone.get("start", 1)), int(zone.get("end", 1))
        if start <= rank <= end:
            rival = next((r for r in standings if int(r.get("rank") or 0) == end + 1), None)
            gap = max(0, points - int((rival or {}).get("points") or points))
        elif rank > end:
            target = next((r for r in standings if int(r.get("rank") or 0) == end), None)
            gap = max(0, int((target or {}).get("points") or points) - points)
        else:
            continue
        closeness = max(0.0, 1.0 - gap / 9.0)
        score = closeness * float(zone.get("weight", 0.7)) * phase_factor
        if score > pressure:
            pressure = score
            reason = f"{zone.get('label', '目标区')}边界相差 {gap} 分"

    danger_from_bottom = int(rules.get("danger_start_from_bottom") or 0)
    if danger_from_bottom:
        danger_start = max(1, size - danger_from_bottom + 1)
        if rank >= danger_start:
            safe_rank = danger_start - 1
            safe = next((r for r in standings if int(r.get("rank") or 0) == safe_rank), None)
            gap = max(0, int((safe or {}).get("points") or points) - points)
            playoff_from_bottom = int(rules.get("relegation_playoff_rank_from_bottom") or 0)
            playoff_rank = size - playoff_from_bottom + 1 if playoff_from_bottom else 0
            if playoff_rank and rank == playoff_rank:
                label = "保级附加赛区"
            elif playoff_rank and rank > playoff_rank:
                label = "直接降级区"
            else:
                label = "降级区"
        else:
            danger = next((r for r in standings if int(r.get("rank") or 0) == danger_start), None)
            gap = max(0, points - int((danger or {}).get("points") or points))
            label = "降级线"
        score = max(0.0, 1.0 - gap / 9.0) * 0.9 * phase_factor
        if score > pressure:
            pressure = score
            reason = f"{label}边界相差 {gap} 分"
    return round(min(1.0, pressure), 3), reason
