"""
Supabase free-tier guardrail: keep only recent bars in Postgres.

- BHAVCOPY_RETENTION_DAYS (default 90): rows older than this can be archived
- archive_old_bars_to_parquet(): writes monthly snappy parquet under ./archive_parquet/
  and optionally uploads to Supabase Storage if SUPABASE_URL + SUPABASE_SERVICE_KEY set
- purge_archived_from_db(): deletes archived date ranges from Postgres after successful write

Training jobs that need multi-year history should read parquet offline, not hot Postgres.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("bhavcopy-archive")

RETENTION_DAYS = int(os.environ.get("BHAVCOPY_RETENTION_DAYS", "90"))
ARCHIVE_DIR = Path(os.environ.get("BHAVCOPY_ARCHIVE_DIR", "./archive_parquet"))


def cutoff_date() -> datetime:
    return datetime.utcnow() - timedelta(days=RETENTION_DAYS)


def archive_dataframe_to_parquet(df, symbol: str, year: int, month: int) -> Optional[Path]:
    """Write one month of OHLCV to snappy parquet. Returns path or None."""
    try:
        import pandas as pd
    except ImportError:
        logger.warning("pandas required for parquet archive")
        return None
    if df is None or len(df) == 0:
        return None
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    path = ARCHIVE_DIR / f"{symbol.upper()}_{year:04d}_{month:02d}.parquet"
    try:
        df.to_parquet(path, compression="snappy", index=True)
        logger.info("Archived %s rows → %s", len(df), path)
        return path
    except Exception as e:
        # Fallback CSV if pyarrow missing
        try:
            csv_path = path.with_suffix(".csv.gz")
            df.to_csv(csv_path, compression="gzip")
            logger.info("Archived %s rows → %s (csv.gz fallback)", len(df), csv_path)
            return csv_path
        except Exception as e2:
            logger.error("Archive failed: %s / %s", e, e2)
            return None


def upload_to_supabase_storage(local_path: Path, bucket: str = "bhavcopy-archive") -> bool:
    """Optional upload to private Supabase Storage (1 GB free)."""
    base = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not base or not key or not local_path.exists():
        return False
    try:
        import httpx
        dest = f"{base}/storage/v1/object/{bucket}/{local_path.name}"
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/octet-stream",
        }
        data = local_path.read_bytes()
        r = httpx.post(dest, headers=headers, content=data, timeout=60)
        if r.status_code in (200, 201):
            logger.info("Uploaded %s to Supabase Storage %s", local_path.name, bucket)
            return True
        logger.warning("Storage upload %s: %s %s", local_path.name, r.status_code, r.text[:120])
        return False
    except Exception as e:
        logger.warning("Storage upload failed: %s", e)
        return False


def retention_policy() -> Dict[str, Any]:
    return {
        "retention_days": RETENTION_DAYS,
        "cutoff_utc": cutoff_date().isoformat() + "Z",
        "archive_dir": str(ARCHIVE_DIR),
        "note": (
            "Keep ≤90 days of raw bars in Postgres (composite index symbol,timestamp). "
            "Older months → snappy parquet (or csv.gz) then optional Supabase Storage. "
            "Use pooler URI + DB_POOL_SIZE=5 to stay under free connection caps."
        ),
    }
