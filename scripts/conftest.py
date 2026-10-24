"""E127: `python3 -m pytest scripts -q` must not die at collection on the VM (plain Python 3.14, no project dependencies).

Three behaviour scripts fake sqlalchemy / the broker in sys.modules and read files relative to services/position-stocks-service, so they MUST run in
their own interpreter from that folder; importing them into pytest broke collection (`No module named orders.plain_short`, FileNotFoundError). They are run
for real, as subprocesses, by test_standalone_behaviour_scripts.py. test_angelone_movers.py needs httpx and is skipped where httpx is not installed.
"""
import importlib.util

STANDALONE = ["test_plain_short.py", "test_short_exit_paths.py", "test_short_live.py"]
collect_ignore = list(STANDALONE)
if importlib.util.find_spec("httpx") is None:
    collect_ignore.append("test_angelone_movers.py")
