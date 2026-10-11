"""group320: main.py re-exports the names it moved to pick_ranking.py, as the very same objects."""
import main as gw
import pick_ranking as pr


def test_main_reexports_the_same_objects():
    for name in ("VALUE_PRICE_CAP", "VALUE_BONUS_MAX", "VALUE_MIN_FUNDAMENTAL_FOR_BONUS",
                 "_value_adjusted_score", "_select_top_picks", "_board_rank_key"):
        assert getattr(gw, name) is getattr(pr, name)


def test_the_moved_block_has_no_dependency_on_main():
    import ast
    src = open(pr.__file__, encoding="utf-8").read()
    imported = {n.module or "" for n in ast.walk(ast.parse(src)) if isinstance(n, ast.ImportFrom)}
    imported |= {a.name for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Import) for a in n.names}
    assert "main" not in imported


def test_ranking_behaviour_is_unchanged():
    rows = [{"symbol": "A", "close": 200, "combined_score": 70, "fundamental_score": 60},
            {"symbol": "B", "close": 2500, "combined_score": 72, "fundamental_score": 90}]
    assert [r["symbol"] for r in pr._select_top_picks(rows)] == ["A", "B"]
