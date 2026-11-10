"""control/guard.py (fno-options-service) - the Calibration page refuses a time that cannot work with the other times (round 286, `clear` round 288).

control/kit.py (byte-identical in the three services) checks each switch on its own: a time must be HH:MM, a number must be inside its range. It cannot know that
"Entry window closes 15:20" is impossible next to "Square off all at 15:15". Round 284 made the ENGINE refuse such an entry (gates.param_problems), but the page still saved the value, said
"applied now", and the engine then skipped every entry with a reason in the log. This subclass asks the same `param_problems` BEFORE the value is saved: a change that would add a
problem is refused with the reason (HTTP 400) and nothing is written. A change that leaves an already-bad combination as it was (or fixes it) is allowed, so the page can always be used to repair it."""
from __future__ import annotations

from typing import Any, Dict, List

import config
from control.kit import Controller, ControlError

# catalog key -> the Params field it sets (only the times: they are the only switches whose validity depends on another switch)
TIME_FIELDS: Dict[str, str] = {"FNO_ENTRY_START": "entry_start", "FNO_ENTRY_END": "entry_end", "FNO_SQUAREOFF_AT": "squareoff_at"}


def new_time_problems(key: str, canon: str) -> List[str]:
    """Problems that setting `key` to `canon` would ADD to what the running values already have. Empty = fine (or not a time switch)."""
    field = TIME_FIELDS.get(key)
    if field is None:
        return []
    from fno import gates
    from fno.params import Params
    p = Params.from_config(config)
    before = set(gates.param_problems(p))
    after = gates.param_problems(p.with_(**{field: canon}))
    return [x for x in after if x not in before]


def restored_time(f, rec: Dict[str, Any] | None) -> str | None:
    """The value `clear` would put back for a time switch: the environment value from before the page saved it, else the shipped default (the same rule as `Controller.clear`)."""
    from control.kit import canonical
    orig = (rec or {}).get("orig_env")
    for raw in (orig, f.shipped):
        if raw is None or not str(raw).strip():
            continue
        try:
            return canonical(f, raw)
        except ValueError:
            continue
    return None


class FnoController(Controller):
    def clear(self, key: str, by: str) -> Dict[str, Any]:
        """Round 288: "back to the .env / default value" is a change too. If that value cannot work with the times now running, it is refused (400, nothing changed)."""
        f = self.by_key.get((key or "").strip())
        if f is not None and f.key in TIME_FIELDS:
            with self._lock:
                rec = self.store.overrides().get(f.key)
                back = restored_time(f, rec) if rec is not None else None
                bad = new_time_problems(f.key, back) if back is not None else []
                if bad:
                    raise ControlError(400, f"{f.key}: going back to {back} cannot work with the other times and was NOT done: " + "; ".join(bad)
                                            + ". Change the other time first, then clear this one.")
                return super().clear(key, by)
        return super().clear(key, by)

    def set(self, key: str, value: Any, by: str, confirm: bool = False, note: str = "") -> Dict[str, Any]:
        f = self.by_key.get((key or "").strip())
        if f is not None and f.key in TIME_FIELDS:
            try:
                from control.kit import canonical
                new = canonical(f, value)
            except ValueError:
                new = None                                           # the base class refuses it with its own message
            if new is not None:
                with self._lock:                                     # the same lock the base class takes, so two page changes cannot slip past each other
                    bad = new_time_problems(f.key, new)
                    if bad:
                        raise ControlError(400, f"{f.key}: {new} cannot work with the other times and was NOT saved: " + "; ".join(bad))
                    return super().set(key, value, by, confirm=confirm, note=note)
        return super().set(key, value, by, confirm=confirm, note=note)
