"""懂球帝公开赛事数据接口。"""
from __future__ import annotations

import json
import re
import time
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import requests

from .team_match import match_pair


class _StandingsParser(HTMLParser):
    """Extract the visible standings table from Dongqiudi's PC data page."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_table = False
        self.in_row = False
        self.in_cell = False
        self.current: list[str] = []
        self.rows: list[list[str]] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set((attrs.get("class") or "").split())
        if tag == "table" and "dp-table" in classes and not self.in_table:
            self.in_table = True
        elif self.in_table and tag == "tr" and "dp-row" in classes:
            self.in_row = True
            self.current = []
        elif self.in_row and tag == "td":
            self.in_cell = True
            self.current.append("")

    def handle_endtag(self, tag):
        if tag == "td" and self.in_cell:
            self.in_cell = False
        elif tag == "tr" and self.in_row:
            self.in_row = False
            if len(self.current) >= 9:
                self.rows.append(self.current)
        elif tag == "table" and self.in_table:
            self.in_table = False

    def handle_data(self, data):
        if self.in_cell:
            text = " ".join(data.split())
            if text:
                self.current[-1] += text



class DongqiudiSource:
    """Fetch match analysis and line-ups from api.dongqiudi.com."""

    BASE = "https://api.dongqiudi.com"
    HEADERS = {
        "User-Agent": "okhttp/3.12.1",
        "Referer": "https://m.dongqiudi.com/",
        "Accept": "application/json",
    }

    def __init__(self):
        self._session = requests.Session()
        self._daily_cache: dict[str, list[dict]] = {}
        self._match_cache: dict[int, dict] = {}
        self._overview_cache: dict[int, dict] = {}
        self._standings_cache: dict[str, list[dict]] = {}
        rules_path = Path(__file__).resolve().parents[2] / "config" / "motivation_formats.json"
        try:
            self._motivation_formats = json.loads(rules_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._motivation_formats = {}

    def _get(self, path: str, params: dict | None = None) -> dict | list | None:
        for attempt in range(2):
            try:
                response = self._session.get(
                    f"{self.BASE}{path}", params=params, headers=self.HEADERS, timeout=(3.05, 12)
                )
                if response.status_code == 200:
                    data = response.json()
                    if isinstance(data, (dict, list)):
                        return data
            except (requests.RequestException, ValueError):
                pass
            if attempt == 0:
                time.sleep(0.25)
        return None

    def _get_pc(self, path: str, params: dict | None = None) -> dict | list | None:
        for attempt in range(2):
            try:
                response = self._session.get(
                    f"https://pc.dongqiudi.com{path}", params=params,
                    headers={**self.HEADERS, "User-Agent": "Mozilla/5.0"}, timeout=(3.05, 15)
                )
                if response.status_code == 200:
                    data = response.json()
                    if isinstance(data, (dict, list)):
                        return data
            except (requests.RequestException, ValueError):
                pass
            if attempt == 0:
                time.sleep(0.25)
        return None

    @staticmethod
    def _pc_id(match_id: int | str) -> str:
        value = str(match_id)
        return value[1:] if value.startswith("5") else value

    def _overview(self, match_id: int | str) -> dict:
        key = int(match_id)
        if key not in self._overview_cache:
            self._overview_cache[key] = self._get_pc(f"/api/data/overview/match/{key}") or {}
        return self._overview_cache[key]

    def _full_standings(self, competition_id: str) -> list[dict]:
        """Load full standings only for competitions with explicit format rules."""
        competition_id = str(competition_id or "")
        if not competition_id or competition_id not in self._motivation_formats:
            return []
        if competition_id not in self._standings_cache:
            rows: list[dict] = []
            for attempt in range(2):
                try:
                    response = self._session.get(
                        "https://pc.dongqiudi.com/data",
                        params={"cid": competition_id, "expanded": 1,
                                "sport": "soccer", "tab": "standings"},
                        headers={**self.HEADERS, "User-Agent": "Mozilla/5.0"},
                        timeout=(3.05, 15),
                    )
                    if response.status_code == 200:
                        parser = _StandingsParser()
                        parser.feed(response.text)
                        for item in parser.rows:
                            try:
                                goals = item[6].split("/")
                                rows.append({
                                    "rank": int(item[0]), "team": item[1],
                                    "played": int(item[2]), "points": int(item[8]),
                                    "goals_for": int(goals[0]),
                                    "goals_against": int(goals[1]),
                                })
                            except (IndexError, TypeError, ValueError):
                                continue
                    if rows:
                        break
                except requests.RequestException:
                    pass
                if attempt == 0:
                    time.sleep(0.5)
            self._standings_cache[competition_id] = rows
        return self._standings_cache[competition_id]

    def fixtures(self, target_date: date) -> list[dict]:
        key = target_date.isoformat()
        if key in self._daily_cache:
            return self._daily_cache[key]
        params = {"start": f"{key}00:00:00", "version": 551, "init": 1}
        data = self._get("/data/tab/important", params=params)
        rows = data.get("list", []) if isinstance(data, dict) else []
        lottery = self._get("/data/tab/lottery", params=params)
        lottery_rows = lottery.get("list", []) if isinstance(lottery, dict) else []
        # The important-games list omits some lower-profile fixtures. Merge the
        # lottery schedule as a second index while preserving important-list order.
        merged = rows + lottery_rows
        deduped: list[dict] = []
        seen: set[str] = set()
        for row in merged:
            if row.get("relate_type") != "match":
                continue
            match_key = str(row.get("match_id") or row.get("relate_id") or "")
            if not match_key or match_key in seen:
                continue
            seen.add(match_key)
            deduped.append(row)
        rows = deduped
        self._daily_cache[key] = rows
        return rows

    def find_match(self, home_team: str, away_team: str, target_date: date) -> dict | None:
        for row in self.fixtures(target_date):
            if match_pair(home_team, away_team,
                          row.get("team_A_name", ""), row.get("team_B_name", "")):
                return row
        return None

    def match_data(self, match_id: int, target_date: date) -> dict | None:
        if match_id in self._match_cache:
            return self._match_cache[match_id]
        mobile_analysis = self._get(f"/mobile/match/analysis/{match_id}")
        pc_analysis = self._get_pc(
            f"/api/data/match/pre_analysis_v1/{match_id}",
            {"platform": "iphone", "version": 718},
        )
        lineup = self._get_pc(
            f"/sport-data/soccer/biz/dqd/v1/match/lineup/{self._pc_id(match_id)}",
            {"app": "dqd", "lang": "zh-cn"},
        )
        if not isinstance(pc_analysis, dict) and not isinstance(mobile_analysis, dict):
            return None
        analysis = mobile_analysis if isinstance(mobile_analysis, dict) else {}
        info = pc_analysis if isinstance(pc_analysis, dict) else (mobile_analysis.get("info") or {})
        lineup_info = lineup or {}
        base = lineup_info.get("base") or {}
        current_lineups = self._parse_pc_lineups(lineup_info.get("persons") or {})
        # base.new_lineup is a change/update flag, not a reliable publication
        # flag: the endpoint can return the confirmed XI with new_lineup=0.
        # Prefer a complete matchday XI from persons; forecasts remain the
        # fallback when either side has fewer than 11 starters.
        has_confirmed_xi = bool(
            current_lineups
            and all(
                len((current_lineups.get(side) or {}).get("starting") or []) >= 11
                for side in ("home", "away")
            )
        )
        lineup_source = "matchday" if has_confirmed_xi else None
        if not lineup_source and base.get("forecast_lineup"):
            current_lineups = self._parse_pc_lineups(lineup_info.get("forecasts") or {})
            if current_lineups:
                lineup_source = "predicted_lineup"

        recent = info.get("recent_record") or {}
        recent_a = (recent.get("team_A") or [])[:50]
        recent_b = (recent.get("team_B") or [])[:50]
        h2h = info.get("battle_history") or {}
        standings = info.get("league_table") or {}
        absences = (lineup_info.get("sideline") or info.get("sideline") or {})

        previous = None
        if not lineup_source:
            previous_persons = ((lineup_info.get("recent_match_persons") or {}).get("persons") or {})
            previous = self._parse_pc_lineups(previous_persons)
            if previous:
                current_lineups = previous
                lineup_source = "previous_match"
            else:
                previous = self._previous_match_lineups(recent_a, recent_b)
            if previous:
                current_lineups = previous.get("lineups") or previous
                lineup_source = "previous_match"

        parsed = {
            "source": "懂球帝",
            "match_id": str(match_id),
            "match_url": f"https://pc.dongqiudi.com/match/{match_id}",
            "api_match": analysis.get("match") or {},
            "analysis": {
                "recent_matches_used": {"home": len(recent_a), "away": len(recent_b)},
                "recent_record": {"home": recent_a, "away": recent_b},
                "recent_xg": {
                    "home": self._recent_xg(recent_a), "away": self._recent_xg(recent_b),
                },
                "schedule_fitness": {
                    "home": self._schedule_summary(recent_a, target_date),
                    "away": self._schedule_summary(recent_b, target_date),
                },
                "goal_time_distribution": {
                    "home": self._goal_time_distribution(recent_a[:20]),
                    "away": self._goal_time_distribution(recent_b[:20]),
                    "sample": 20,
                },
                "half_full_time": {
                    "home": self._half_full_time(recent_a),
                    "away": self._half_full_time(recent_b),
                },
                "technical_comparison": {
                    "home": self._technical_summary(recent_a[:5]),
                    "away": self._technical_summary(recent_b[:5]),
                },
                "head_to_head": h2h,
                "head_to_head_summary": self._summarize_h2h(h2h),
                "league_table": standings,
                "injury_news": {"home": absences.get("team_A") or [],
                                "away": absences.get("team_B") or []},
            },
            "injuries": {
                "home_count": len(absences.get("team_A") or []),
                "away_count": len(absences.get("team_B") or []),
                "home_attackers": self._attacker_absences(absences.get("team_A") or []),
                "away_attackers": self._attacker_absences(absences.get("team_B") or []),
            },
            "lineups": current_lineups,
            "lineup_source": lineup_source,
            "lineup_fallback": previous.get("source_matches") if isinstance(previous, dict) and previous.get("source_matches") else None,
            "weather": {k: base.get(k) for k in ("weather", "temperature", "field", "referee", "weather_info") if base.get(k)},
            "updated": base.get("new_lineup"),
        }
        self._match_cache[match_id] = parsed
        return parsed

    def enrich_fixtures(self, fixtures: list, target_date: date) -> dict[str, dict]:
        enriched: dict[str, dict] = {}
        for fixture in fixtures:
            row = self.find_match(fixture.home_team, fixture.away_team, target_date)
            if not row:
                print(f"    [懂球帝] 赛程未匹配：{fixture.home_team} vs {fixture.away_team}")
                continue
            try:
                match_id = int(row.get("match_id") or row.get("relate_id"))
            except (TypeError, ValueError):
                continue
            data = None
            # The public PC endpoints occasionally return an empty/non-JSON
            # response during a multi-fixture scrape. Team names and the match
            # row may already be valid, so retry that exact match before
            # treating it as unmatched.
            for attempt in range(3):
                data = self.match_data(match_id, target_date)
                if data:
                    break
                if attempt < 2:
                    time.sleep(0.6 * (attempt + 1))
            if not data:
                print(f"    [懂球帝] 比赛 {match_id} 已匹配，但详情接口连续失败：{fixture.home_team} vs {fixture.away_team}")
                continue
            competition_id = str(row.get("competition_id") or "")
            competition_rules = self._motivation_formats.get(competition_id)
            if competition_rules and (competition_rules.get("race_zones") or competition_rules.get("danger_start_from_bottom")):
                data["analysis"]["full_standings"] = self._full_standings(competition_id)
            data["match_context"] = {
                "competition_id": competition_id,
                "competition_name": row.get("competition_name") or row.get("competition"),
                "round_name": row.get("round_name"),
                "match_title": row.get("match_title"),
                "gameweek": row.get("gameweek"),
                "score_info": row.get("score_info"),
                "aggregate_scores": {
                    "home": self._to_optional_int(row.get("ags_A")),
                    "away": self._to_optional_int(row.get("ags_B")),
                },
                "format_rules": competition_rules,
            }
            analysis = data.get("analysis") or {}
            recent = analysis.get("recent_record") or {}
            form = {
                "sample_requested": 50,
                "source_sample_max": max(len(recent.get("home") or []), len(recent.get("away") or [])),
                "home": self._summarize_recent(recent.get("home") or []),
                "away": self._summarize_recent(recent.get("away") or []),
                "xg": analysis.get("recent_xg") or {},
            }
            form["home"]["half_full_time"] = (analysis.get("half_full_time") or {}).get("home")
            form["away"]["half_full_time"] = (analysis.get("half_full_time") or {}).get("away")
            xg = form.get("xg") or {}
            data["form_xg"] = {
                "home_avg": (xg.get("home") or {}).get("average"),
                "home_n": (xg.get("home") or {}).get("matches", 0),
                "away_avg": (xg.get("away") or {}).get("average"),
                "away_n": (xg.get("away") or {}).get("matches", 0),
            }
            enriched[fixture.match_id] = {
                **data,
                "fixture": row,
                "form_analysis": form,
            }
        return enriched

    @staticmethod
    def _to_optional_int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _schedule_summary(rows: list, target_date: date) -> dict:
        """Summarize the club's recent 14-day match load from DQD match dates."""
        dated: list[tuple[date, dict]] = []
        for row in rows:
            raw_day = str(row.get("date") or "").strip()
            raw_year = str(row.get("year") or target_date.year)
            try:
                match_day = date.fromisoformat(raw_day) if len(raw_day) == 10 else date.fromisoformat(f"{raw_year}-{raw_day}")
            except ValueError:
                continue
            days_ago = (target_date - match_day).days
            if 0 <= days_ago <= 14:
                dated.append((match_day, row))
        last_day = max((day for day, _ in dated), default=None)
        own_away = sum(1 for _, row in dated if str(row.get("main_team") or "") == "team_B")
        return {
            "available": bool(dated),
            "sample_matches": len(rows),
            "matches_last_14d": len(dated),
            "days_since_last_match": (target_date - last_day).days if last_day else None,
            "away_matches_last_14d": own_away,
            "source": "懂球帝近况赛程",
        }

    @staticmethod
    def _summarize_recent(rows: list) -> dict:
        # The PC pre-analysis API returns up to ~50 games; use all returned rows.
        matches = rows[:50]
        points = goals_for = goals_against = 0
        for row in matches:
            result = str(row.get("color") or "").lower()
            points += {"win": 3, "draw": 1, "lose": 0}.get(result, 0)
            score = str(row.get("score") or "")
            m = re.match(r"\s*(\d+)\s*[-:：]\s*(\d+)\s*", score)
            if m:
                a, b = int(m.group(1)), int(m.group(2))
                own, opp = (a, b) if row.get("main_team") == "team_A" else (b, a)
                goals_for += own
                goals_against += opp
        return {
            "matches": len(matches),
            "results_count": sum(1 for row in matches if row.get("color") in ("win", "draw", "lose")),
            "points_per_game": round(points / len(matches), 3) if matches else None,
            "goals_for_per_game": round(goals_for / len(matches), 3) if matches else None,
            "goals_against_per_game": round(goals_against / len(matches), 3) if matches else None,
        }

    def _recent_xg(self, rows: list) -> dict:
        # Match xG is served by the official per-match overview endpoint, not by
        # the recent-record summary. Compute the last-five sample from actual xG.
        vals: list[float] = []
        for row in rows[:5]:
            match_id = self._match_id_from_row(row)
            if not match_id:
                continue
            overview = self._overview(match_id)
            stats = overview.get("statistics") or {}
            side = str(row.get("main_team") or "team_A")
            for stat in stats.get("list") or []:
                label = str(stat.get("type") or "").lower()
                if "xg" not in label and "Ԥ�ڽ���" not in label:
                    continue
                try:
                    val = float((stat.get(side) or {}).get("value"))
                except (TypeError, ValueError):
                    continue
                if val >= 0:
                    vals.append(val)
                break
        return {"matches": len(vals), "average": round(sum(vals) / len(vals), 3) if vals else None,
                "values": vals}

    @staticmethod
    def _match_id_from_row(row: dict) -> int | None:
        href = str(row.get("match_href") or "")
        match = re.search(r"(\d+)$", href)
        if not match:
            return None
        try:
            return int(match.group(1))
        except ValueError:
            return None

    def _goal_time_distribution(self, rows: list) -> dict:
        ranges = ((1, 15), (16, 30), (31, 45), (46, 60), (61, 75), (76, 90))
        out = {f"{a}-{b}": {"scored": 0, "conceded": 0} for a, b in ranges}
        games = 0
        for row in rows:
            match_id = self._match_id_from_row(row)
            if not match_id:
                continue
            events = self._overview(match_id).get("events") or {}
            if not events:
                continue
            games += 1
            own_side = "teamAEvents" if row.get("main_team") == "team_A" else "teamBEvents"
            for group in events.values():
                minute = group.get("minute")
                try:
                    minute = int(re.match(r"\d+", str(minute)).group())
                except (AttributeError, ValueError, TypeError):
                    continue
                bucket = next((f"{a}-{b}" for a, b in ranges if a <= minute <= b), None)
                if not bucket:
                    continue
                for side in ("teamAEvents", "teamBEvents"):
                    goals = sum(1 for event in group.get(side, []) if event.get("code") == "G")
                    out[bucket]["scored" if side == own_side else "conceded"] += goals
        return {"sample_matches": games, "buckets": out}

    @staticmethod
    def _half_full_time(rows: list) -> dict:
        counts: dict[str, int] = {}
        for row in rows[:50]:
            ht = re.match(r"\s*(\d+)\s*[-:：]\s*(\d+)", str(row.get("ht_score") or ""))
            ft = re.match(r"\s*(\d+)\s*[-:：]\s*(\d+)", str(row.get("score") or ""))
            if not ht or not ft:
                continue
            own = row.get("main_team") == "team_A"
            h1, h2 = (int(ht[1]), int(ht[2])) if own else (int(ht[2]), int(ht[1]))
            f1, f2 = (int(ft[1]), int(ft[2])) if own else (int(ft[2]), int(ft[1]))
            half = "W" if h1 > h2 else "D" if h1 == h2 else "L"
            full = "W" if f1 > f2 else "D" if f1 == f2 else "L"
            key = half + full
            counts[key] = counts.get(key, 0) + 1
        return {"matches": sum(counts.values()), "counts": counts}

    def _technical_summary(self, rows: list) -> dict:
        totals: dict[str, list[float]] = {}
        for row in rows:
            match_id = self._match_id_from_row(row)
            if not match_id:
                continue
            stats = (self._overview(match_id).get("statistics") or {}).get("list") or []
            side = str(row.get("main_team") or "team_A")
            for item in stats:
                label = str(item.get("type") or "")
                if not label or "xg" in label.lower() or "Ԥ�ڽ���" in label:
                    continue
                raw = (item.get(side) or {}).get("value")
                try:
                    val = float(raw)
                except (TypeError, ValueError):
                    continue
                totals.setdefault(label, []).append(val)
        return {k: round(sum(v) / len(v), 2) for k, v in totals.items() if v}

    @staticmethod
    def _summarize_h2h(data: dict) -> dict:
        side_a = data.get("team_A") or {}
        wins = int(side_a.get("win") or 0)
        draws = int(side_a.get("draw") or 0)
        losses = int(side_a.get("lose") or 0)
        sample = wins + draws + losses
        return {
            "matches": sample,
            "home_wins": wins,
            "draws": draws,
            "away_wins": losses,
            "home_points_per_game": round((3 * wins + draws) / sample, 3) if sample else None,
            "away_points_per_game": round((3 * losses + draws) / sample, 3) if sample else None,
        }

    def _previous_match_lineups(self, recent_a: list, recent_b: list) -> dict | None:
        # Use every recent match returned by the analysis tab, newest first,
        # until each team's latest available XI has been found.
        result: dict[str, Any] = {"home": None, "away": None}
        source_matches: dict[str, str] = {}
        rows_by_side = (("home", recent_a), ("away", recent_b))
        for team_side, rows in rows_by_side:
            for row in rows:
                href = str(row.get("match_href") or "")
                match = re.search(r"(\d+)$", href)
                if not match:
                    continue
                historical = self._get(f"/mobile/match/lineup/{match.group(1)}")
                hist_info = (historical or {}).get("info") or {}
                parsed = self._parse_lineups(hist_info, historical or {})
                if not parsed:
                    continue
                # recent_record tells which side the target club played on.
                target_key = str(row.get("main_team") or "")
                historical_side = "home" if target_key == "team_A" else "away"
                result[team_side] = parsed.get(historical_side)
                source_matches[team_side] = str(match.group(1))
                break
        if not any(result.values()):
            return None
        return {"lineups": result, "source_matches": source_matches}

    @staticmethod
    def _parse_lineups(info: dict, root: dict) -> dict | None:
        raw = info.get("lineup") or []
        if not raw:
            return None
        sides: dict[str, list[dict]] = {"home": [], "away": []}
        for row in raw:
            for api_side, side in (("team_A", "home"), ("team_B", "away")):
                player = row.get(api_side)
                if not isinstance(player, dict) or not player.get("person"):
                    continue
                sides[side].append({
                    "name": player.get("person"),
                    "player_id": player.get("person_id"),
                    "number": player.get("shirtnumber"),
                    "position": player.get("position") or player.get("position_x"),
                    "formation_position": player.get("formation_place"),
                })
        if not sides["home"] and not sides["away"]:
            return None
        subs = info.get("sub") or []
        bench = {"home": [], "away": []}
        for row in subs:
            for api_side, side in (("team_A", "home"), ("team_B", "away")):
                player = row.get(api_side)
                if isinstance(player, dict) and player.get("person"):
                    bench[side].append({"name": player.get("person"), "number": player.get("shirtnumber")})
        for side in ("home", "away"):
            sides[side] = {"starting": sides[side], "bench": bench[side]}
        return sides

    @staticmethod
    def _parse_pc_lineups(teams: dict) -> dict | None:
        result: dict[str, dict] = {}
        for api_side, side in (("team_A", "home"), ("team_B", "away")):
            team = teams.get(api_side) or {}
            raw = team.get("lineups") or team.get("lineup") or []
            if not isinstance(raw, list):
                continue
            starters = []
            for player in raw:
                if not isinstance(player, dict):
                    continue
                name = player.get("person") or player.get("name")
                if not name:
                    continue
                starters.append({
                    "name": name,
                    "player_id": player.get("person_id"),
                    "number": player.get("shirtnumber"),
                    "position": player.get("position") or player.get("position_x"),
                    "formation_position": player.get("formation_place"),
                })
            subs = []
            for player in team.get("sub") or []:
                if isinstance(player, dict) and (player.get("person") or player.get("name")):
                    subs.append({"name": player.get("person") or player.get("name"),
                                 "number": player.get("shirtnumber")})
            if starters:
                result[side] = {"starting": starters, "bench": subs}
        return result or None

    @staticmethod
    def _attacker_absences(players: list) -> int:
        return sum(1 for player in players
                   if any(word in str(player.get("position") or player.get("position_name") or "").lower()
                          for word in ("forward", "attacker", "striker", "前锋", "前场", "fw")))
