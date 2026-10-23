"""control/boot.py - import this BEFORE `config` (first service-local import in main.py): it writes the stored switch overrides into os.environ so
config.py reads them like any .env value. It must never stop the service from booting, so every error is swallowed."""
try:  # noqa: SIM105
    from control.catalog import CATALOG
    from control.kit import boot_apply

    APPLIED = boot_apply(CATALOG)
except Exception as _e:  # noqa: BLE001
    APPLIED = {}
    print(f"control.boot: overrides not applied ({type(_e).__name__}: {_e})")
