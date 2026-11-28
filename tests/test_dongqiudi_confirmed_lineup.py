from datetime import date

from engine.sources.dongqiudi import DongqiudiSource
from engine.sources.team_match import match_pair


def test_confirmed_persons_lineup_wins_even_when_new_lineup_is_zero(monkeypatch):
    source = DongqiudiSource()
    starters = lambda prefix: [
        {"person": f"{prefix}{i}", "shirtnumber": i} for i in range(1, 12)
    ]
    lineup = {
        "base": {"new_lineup": 0, "forecast_lineup": 1},
        "persons": {
            "team_A": {"lineups": starters("H")},
            "team_B": {"lineups": starters("A")},
        },
        "forecasts": {
            "team_A": {"lineups": [{"person": "forecast home"}]},
            "team_B": {"lineups": [{"person": "forecast away"}]},
        },
    }
    monkeypatch.setattr(source, "_get", lambda path, params=None: {"info": {}})
    monkeypatch.setattr(source, "_get_pc", lambda path, params=None: lineup)

    result = source.match_data(12345, date(2026, 11, 28))

    assert result["lineup_source"] == "matchday"
    assert [p["name"] for p in result["lineups"]["home"]["starting"]] == [
        f"H{i}" for i in range(1, 12)
    ]


def test_sporttery_short_names_match_dongqiudi_schedule_names():
    pairs = [
        ("弗洛西诺", "弗罗西诺内"),
        ("谢菲联", "谢菲尔德联"),
        ("伊普斯", "伊普斯维奇"),
        ("维拉", "阿斯顿维拉"),
        ("马竞", "马德里竞技"),
        ("鹿斯巴达", "鹿特丹斯巴达"),
        ("帕梅拉斯", "帕尔梅拉斯"),
    ]
    for sporttery, dongqiudi in pairs:
        assert match_pair(sporttery, "对手甲", dongqiudi, "对手甲")

