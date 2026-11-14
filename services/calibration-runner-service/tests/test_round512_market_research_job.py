"""Round 512: the runner's Market research job, weekly pack and Saturday 09:00 slot."""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import jobs as J        # noqa: E402
import scheduler as S   # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
CTX = {"rt": "http://real-trade-service:8004"}


def test_job_posts_to_real_trade_with_the_admin_token_and_saves_the_record():
    st = J.JOBS["market_research"]["builder"](CTX, J.coerce_params(J.JOBS["market_research"], {"trigger": "runner"}))
    assert len(st) == 1 and st[0]["kind"] == "http" and st[0]["method"] == "POST" and st[0]["auth"] is True
    assert st[0]["url"] == "http://real-trade-service:8004/adaptive/research?trigger=runner" and st[0]["expect"] == [200]
    assert st[0]["save"] == "market_research.json"
    assert J.JOBS["market_research"]["builder"](CTX, {})[0]["url"].endswith("trigger=manual")


def test_trigger_parameter_is_bounded():
    j = J.JOBS["market_research"]
    assert J.coerce_params(j, {})["trigger"] == "manual"
    try:
        J.coerce_params(j, {"trigger": "x; rm -rf"})
        assert False, "must be rejected"
    except ValueError:
        pass


def test_pack_and_saturday_slot():
    pack = J.PACK_MAP["weekly_market_research"]
    assert pack["jobs"] == [["market_research", {"trigger": "runner"}]]
    slot = next(e for e in J.SCHEDULE_DEFAULT if e["id"] == "s_market_research")
    assert slot["pack"] == "weekly_market_research" and slot["days"] == [5] and slot["time"] == "09:00" and slot["enabled"] is True


def test_scheduler_fires_it_on_saturday_0900_only(tmp_path):
    got = []
    sat = S.Scheduler(str(tmp_path / "s.json"), J.SCHEDULE_DEFAULT, lambda *a, **k: got.append(a[1]), list(J.PACK_MAP), True,
                      now_fn=lambda: datetime(2026, 11, 14, 9, 2, tzinfo=IST))
    assert sat.tick() == ["s_market_research"] and got == ["weekly_market_research"]
    got.clear()
    tue = S.Scheduler(str(tmp_path / "t.json"), J.SCHEDULE_DEFAULT, lambda *a, **k: got.append(a[1]), list(J.PACK_MAP), True,
                      now_fn=lambda: datetime(2026, 11, 17, 9, 2, tzinfo=IST))
    assert "s_market_research" not in tue.tick()


def test_a_posting_job_serialises_with_itself():
    locks = J.derive_locks(J.JOBS["market_research"], J.JOBS["market_research"]["builder"](CTX, {}), {"cal": "/x/cal", "exp": "/x/exp"})
    assert locks["excl"] == ["post:real-trade-service:8004/adaptive/research"] and locks["solo"] is False
