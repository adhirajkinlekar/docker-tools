"""
Production-grade seed script – generates realistic NACHA ACH files and uploads
them to MinIO.  Archive files trigger the RAG indexer via MinIO webhook.

Folder layout in the 'payments' bucket:
  Active/   – current drop folder (~50 files, last 60 days)
  Archive/  – 12-month historical files (~195 files, Jun 2025-May 2026)

Companies
─────────
  ACME MANUFACTURING   CLT001  bi-weekly payroll       ACTIVE   (Active + Archive)
  SWIFT PAYROLL LLC    CLT002  weekly payroll proc.    ACTIVE   (Active + Archive)
  NEXUS SUPPLY CHAIN   CLT003  twice-monthly vendor    ACTIVE   (Active + Archive)
  MERIDIAN HEALTH SYS  CLT004  monthly insurance       ACTIVE   (Active + Archive)
  COASTAL RETAIL       CLT005  tri-weekly settlements  ACTIVE   (Active + Archive)
  VERTEX TECHNOLOGIES  CLT006  twice-monthly SaaS      ACTIVE   (Active + Archive)
  HARBOR LOGISTICS LLC CLT007  weekly freight (susp.)  SUSPENDED (Archive only – last drop Feb 2026)
  PINNACLE TRADING CO  CLT008  monthly large batches   CLOSED   (Archive only – last drop Oct 2025)
"""

import math
import os
import random
import time
from datetime import date, timedelta

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

MINIO_ENDPOINT   = os.environ.get("MINIO_ENDPOINT",   "http://minio:9000")
MINIO_ACCESS_KEY = os.environ.get("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.environ.get("MINIO_SECRET_KEY", "minioadmin")
BUCKET = "payments"

# Deterministic RNG so every seed run produces identical files
_rng = random.Random(20260610)

# ─────────────────────────────────────────────────────────────────────────────
# Company profiles
# ─────────────────────────────────────────────────────────────────────────────

PROFILES = {
    "ACME": {
        "name": "ACME MANUFACTURING",
        "id":   "1234567890",
        "code": "ACME",
        "dfi":  "02100002",
        "entry_class": "PPD",
        "entry_desc":  "PAYROLL",
        "drop_hour_range": (2, 5),          # overnight payroll batch
        "entry_count_range": (22, 38),
        "amount_range": (1850.0, 6200.0),   # per employee
        "debit_ratio": 0.07,                # 7% adjustments/returns
        "monthly_growth": 0.03,
        "base_month": date(2025, 6, 1),
        "base_total": 95_000,               # approx file total at base_month
    },
    "SWIFT": {
        "name": "SWIFT PAYROLL LLC",
        "id":   "9988776655",
        "code": "SWFT",
        "dfi":  "02600002",
        "entry_class": "PPD",
        "entry_desc":  "PAYROLL",
        "drop_hour_range": (3, 6),
        "entry_count_range": (90, 160),
        "amount_range": (1200.0, 9400.0),
        "debit_ratio": 0.04,
        "monthly_growth": 0.015,
        "base_month": date(2025, 6, 1),
        "base_total": 520_000,
        "bonus_months": [3],                # March bonus run – 1.45x multiplier
    },
    "NEXUS": {
        "name": "NEXUS SUPPLY CHAIN",
        "id":   "7654321098",
        "code": "NXSC",
        "dfi":  "09100002",
        "entry_class": "CCD",
        "entry_desc":  "VENDOR PMT",
        "drop_hour_range": (8, 11),
        "entry_count_range": (14, 28),
        "amount_range": (9500.0, 72_000.0),
        "debit_ratio": 0.10,
        "monthly_growth": 0.02,
        "base_month": date(2025, 6, 1),
        "base_total": 280_000,
        "seasonal_multiplier": {10: 1.30, 11: 1.42, 12: 1.45, 1: 1.12},  # Q4 peak
    },
    "MERIDIAN": {
        "name": "MERIDIAN HEALTH SYS",
        "id":   "5566778899",
        "code": "MRDH",
        "dfi":  "02100002",
        "entry_class": "CCD",
        "entry_desc":  "INS DISBMT",
        "drop_hour_range": (6, 9),
        "entry_count_range": (50, 95),
        "amount_range": (480.0, 17_500.0),
        "debit_ratio": 0.08,
        "monthly_growth": 0.01,
        "base_month": date(2025, 6, 1),
        "base_total": 1_400_000,
        "seasonal_multiplier": {1: 1.35, 2: 1.28, 3: 1.18},  # Q1 billing heavy
    },
    "COASTAL": {
        "name": "COASTAL RETAIL PRTNRS",
        "id":   "1122334455",
        "code": "CRPL",
        "dfi":  "12100002",
        "entry_class": "PPD",
        "entry_desc":  "SETTLEMENT",
        "drop_hour_range": (22, 24),        # end-of-day processing (maps to 22:xx-23:xx)
        "entry_count_range": (18, 32),
        "amount_range": (1300.0, 13_500.0),
        "debit_ratio": 0.12,                # chargebacks
        "monthly_growth": 0.01,
        "base_month": date(2025, 6, 1),
        "base_total": 65_000,
        "seasonal_multiplier": {11: 1.38, 12: 1.55, 1: 1.25},  # Q4 + January returns
    },
    "VERTEX": {
        "name": "VERTEX TECHNOLOGIES",
        "id":   "0987654321",
        "code": "VRTX",
        "dfi":  "02600002",
        "entry_class": "CCD",
        "entry_desc":  "SAAS PYMT",
        "drop_hour_range": (7, 10),
        "entry_count_range": (40, 72),
        "amount_range": (2800.0, 21_000.0),
        "debit_ratio": 0.06,
        "monthly_growth": 0.08,             # high-growth tech company
        "base_month": date(2025, 6, 1),
        "base_total": 185_000,
    },
    "HARBOR": {
        "name": "HARBOR LOGISTICS LLC",
        "id":   "4455667788",
        "code": "HBLG",
        "dfi":  "09100002",
        "entry_class": "CCD",
        "entry_desc":  "FREIGHT",
        "drop_hour_range": (9, 12),
        "entry_count_range": (10, 22),
        "amount_range": (5500.0, 44_000.0),
        "debit_ratio": 0.12,
        "monthly_growth": -0.06,            # declining (going out of business)
        "base_month": date(2025, 6, 1),
        "base_total": 110_000,
        "last_active": date(2026, 2, 15),   # suspended Feb 2026
    },
    "PINNACLE": {
        "name": "PINNACLE TRADING CO",
        "id":   "2233445566",
        "code": "PNTC",
        "dfi":  "02100002",
        "entry_class": "CTX",
        "entry_desc":  "TRADE SETL",
        "drop_hour_range": (10, 14),
        "entry_count_range": (4, 10),
        "amount_range": (55_000.0, 320_000.0),
        "debit_ratio": 0.25,                # erratic – high debit ratio
        "monthly_growth": 0.0,
        "base_month": date(2025, 6, 1),
        "base_total": 750_000,
        "spike_month": 9,                   # Sep 2025: massive debit spike (fraud pattern)
        "last_active": date(2025, 10, 31),  # closed Oct 2025
    },
}

# ─────────────────────────────────────────────────────────────────────────────
# NACHA record builders (all records exactly 94 characters)
# ─────────────────────────────────────────────────────────────────────────────

def _pad(s: str, width: int, align: str = "left", fill: str = " ") -> str:
    s = str(s)[:width]
    return s.ljust(width, fill) if align == "left" else s.rjust(width, fill)


def build_file_header(
    dest_routing: str,
    origin_routing: str,
    creation_date: str,   # YYMMDD
    dest_name: str,
    origin_name: str,
    file_id: str = "A",
) -> str:
    line = (
        "1"
        + "01"
        + " " + _pad(dest_routing, 9)
        + _pad(origin_routing, 10)
        + _pad(creation_date, 6)
        + "    "
        + file_id[0]
        + "094"
        + "10"
        + "1"
        + _pad(dest_name, 23)
        + _pad(origin_name, 23)
        + "        "
    )
    assert len(line) == 94, f"File header wrong length: {len(line)}"
    return line


def build_batch_header(
    service_class: str,
    company_name: str,
    company_id: str,
    entry_class: str,
    entry_desc: str,
    effective_date: str,   # YYMMDD
    orig_dfi: str,
    batch_number: int,
) -> str:
    line = (
        "5"
        + _pad(service_class, 3)
        + _pad(company_name, 16)
        + _pad("", 20)
        + _pad(company_id, 10)
        + _pad(entry_class, 3)
        + _pad(entry_desc, 10)
        + "      "
        + _pad(effective_date, 6)
        + "   "
        + "1"
        + _pad(orig_dfi, 8)
        + _pad(str(batch_number), 7, align="right", fill="0")
    )
    assert len(line) == 94, f"Batch header wrong length: {len(line)}"
    return line


def build_entry_detail(
    transaction_code: str,
    recv_routing: str,
    check_digit: str,
    amount_cents: int,
    seq: int,
    orig_dfi_short: str,
    batch_number: int,
) -> str:
    acct     = _pad(f"ACCT{seq:013d}", 17)
    ind_id   = _pad(f"CUST{seq:011d}", 15)
    ind_name = _pad(f"CUSTOMER {seq:013d}", 22)
    trace    = _pad(orig_dfi_short, 8) + _pad(str(seq), 7, align="right", fill="0")
    line = (
        "6"
        + _pad(transaction_code, 2)
        + _pad(recv_routing, 8)
        + check_digit[0]
        + acct
        + _pad(str(amount_cents), 10, align="right", fill="0")
        + ind_id
        + ind_name
        + "  "
        + "0"
        + trace
    )
    assert len(line) == 94, f"Entry detail wrong length: {len(line)}"
    return line


def build_batch_control(
    service_class: str,
    entry_count: int,
    entry_hash: int,
    total_debit_cents: int,
    total_credit_cents: int,
    company_id: str,
    orig_dfi: str,
    batch_number: int,
) -> str:
    hash_str = str(entry_hash)[-10:].rjust(10, "0")
    line = (
        "8"
        + _pad(service_class, 3)
        + _pad(str(entry_count), 6, align="right", fill="0")
        + hash_str
        + _pad(str(total_debit_cents), 12, align="right", fill="0")
        + _pad(str(total_credit_cents), 12, align="right", fill="0")
        + _pad(company_id, 10)
        + _pad("", 19)
        + _pad("", 6)
        + _pad(orig_dfi, 8)
        + _pad(str(batch_number), 7, align="right", fill="0")
    )
    assert len(line) == 94, f"Batch control wrong length: {len(line)}"
    return line


def build_file_control(
    batch_count: int,
    total_lines: int,
    entry_count: int,
    entry_hash: int,
    total_debit_cents: int,
    total_credit_cents: int,
) -> str:
    block_count = math.ceil((total_lines + 1) / 10)
    hash_str    = str(entry_hash)[-10:].rjust(10, "0")
    line = (
        "9"
        + _pad(str(batch_count), 6, align="right", fill="0")
        + _pad(str(block_count), 6, align="right", fill="0")
        + _pad(str(entry_count), 8, align="right", fill="0")
        + hash_str
        + _pad(str(total_debit_cents), 12, align="right", fill="0")
        + _pad(str(total_credit_cents), 12, align="right", fill="0")
        + "9" * 39
    )
    assert len(line) == 94, f"File control wrong length: {len(line)}"
    return line


DEBIT_CODES = {"27", "28", "29", "37", "38", "55"}
_RECV_BANKS = [
    ("02100002", "1"),  # JPMorgan Chase
    ("02600002", "9"),  # Citibank
    ("12100002", "4"),  # Wells Fargo
    ("09100002", "2"),  # US Bank
    ("07600012", "6"),  # Bank of America
]


def generate_ach(
    profile: dict,
    batches_entries: list[list[dict]],   # list of batch → list of entry dicts
    drop_date: date,
    drop_hour: int,
    drop_minute: int,
) -> str:
    """Build a complete NACHA file from one or more batches."""
    eff_str      = drop_date.strftime("%y%m%d")
    create_str   = eff_str
    orig_dfi     = profile["dfi"]
    company_name = profile["name"]
    company_id   = profile["id"]
    entry_class  = profile["entry_class"]
    entry_desc   = profile["entry_desc"]

    all_lines: list[str] = []
    all_lines.append(
        build_file_header(
            dest_routing="021000021",
            origin_routing="0" + company_id[:9],
            creation_date=create_str,
            dest_name="JPMORGAN CHASE",
            origin_name=company_name[:23],
        )
    )

    file_entry_count    = 0
    file_entry_hash     = 0
    file_total_debit    = 0
    file_total_credit   = 0

    for batch_idx, entries in enumerate(batches_entries, start=1):
        has_debits  = any(e["transaction_code"] in DEBIT_CODES for e in entries)
        has_credits = any(e["transaction_code"] not in DEBIT_CODES for e in entries)
        svc_class   = "200" if (has_debits and has_credits) else ("225" if has_debits else "220")

        all_lines.append(
            build_batch_header(
                svc_class, company_name, company_id,
                entry_class, entry_desc, eff_str, orig_dfi, batch_idx,
            )
        )

        batch_hash  = 0
        batch_debit = 0
        batch_credit= 0

        recv_bank, recv_check = _rng.choice(_RECV_BANKS)
        for seq, e in enumerate(entries, start=1):
            tc           = e["transaction_code"]
            amount_cents = int(round(e["amount"] * 100))
            all_lines.append(
                build_entry_detail(tc, recv_bank, recv_check, amount_cents,
                                   seq, orig_dfi, batch_idx)
            )
            batch_hash += int(recv_bank)
            if tc in DEBIT_CODES:
                batch_debit  += amount_cents
            else:
                batch_credit += amount_cents

        all_lines.append(
            build_batch_control(svc_class, len(entries), batch_hash,
                                batch_debit, batch_credit, company_id, orig_dfi, batch_idx)
        )
        file_entry_count  += len(entries)
        file_entry_hash   += batch_hash
        file_total_debit  += batch_debit
        file_total_credit += batch_credit

    all_lines.append(
        build_file_control(
            len(batches_entries), len(all_lines), file_entry_count,
            file_entry_hash, file_total_debit, file_total_credit,
        )
    )

    while len(all_lines) % 10 != 0:
        all_lines.append("9" * 94)

    return "\n".join(all_lines) + "\n"


# ─────────────────────────────────────────────────────────────────────────────
# Entry / amount generators
# ─────────────────────────────────────────────────────────────────────────────

def _month_offset(d: date, profile: dict) -> int:
    base = profile["base_month"]
    return (d.year - base.year) * 12 + (d.month - base.month)


def _growth_factor(d: date, profile: dict) -> float:
    mo  = _month_offset(d, profile)
    g   = (1 + profile["monthly_growth"]) ** mo
    sm  = profile.get("seasonal_multiplier", {}).get(d.month, 1.0)
    # HARBOR: clamp to near-zero as it approaches last_active
    if "last_active" in profile:
        days_left = (profile["last_active"] - d).days
        fade = max(0.05, min(1.0, days_left / 180))
        g *= fade
    return g * sm


def _make_entries(profile: dict, d: date) -> list[dict]:
    """Generate a single batch of entries for a drop on date d."""
    gf     = _growth_factor(d, profile)
    # March bonus for SWIFT
    if profile.get("bonus_months") and d.month in profile["bonus_months"]:
        gf *= 1.45
    # PINNACLE spike month – mostly debits
    spike = profile.get("spike_month") and d.month == profile["spike_month"]

    count      = _rng.randint(*profile["entry_count_range"])
    lo, hi     = profile["amount_range"]
    debit_ratio = 0.80 if spike else profile["debit_ratio"]

    entries = []
    for _ in range(count):
        is_debit = _rng.random() < debit_ratio
        tc       = "27" if is_debit else "22"
        # Realistic non-round amounts
        raw      = _rng.uniform(lo, hi) * gf * _rng.uniform(0.88, 1.14)
        # Salary-style: round to nearest dollar, then add cents
        amount   = round(raw, 2)
        entries.append({"amount": amount, "transaction_code": tc})

    return entries


def _split_batches(entries: list[dict], max_per_batch: int = 120) -> list[list[dict]]:
    """Split a large entry list into multiple batches (realistic for high-volume processors)."""
    if len(entries) <= max_per_batch:
        return [entries]
    batches = []
    for i in range(0, len(entries), max_per_batch):
        batches.append(entries[i:i + max_per_batch])
    return batches


# ─────────────────────────────────────────────────────────────────────────────
# Drop-schedule generators
# ─────────────────────────────────────────────────────────────────────────────

def _is_business_day(d: date) -> bool:
    return d.weekday() < 5


def _next_biz(d: date) -> date:
    while not _is_business_day(d):
        d += timedelta(days=1)
    return d


def _biweekly_dates(start: date, end: date, day1: int = 1, day2: int = 15) -> list[date]:
    """1st and 15th of each month, adjusted to next business day."""
    result = []
    cur = date(start.year, start.month, 1)
    while cur <= end:
        for dom in (day1, day2):
            try:
                d = _next_biz(date(cur.year, cur.month, dom))
            except ValueError:
                continue
            if start <= d <= end:
                result.append(d)
        cur = date(cur.year + (cur.month // 12), cur.month % 12 + 1, 1)
    return sorted(set(result))


def _weekly_dates(start: date, end: date, weekday: int = 4) -> list[date]:
    """Every specified weekday (0=Mon … 4=Fri)."""
    result = []
    d = start
    while d.weekday() != weekday:
        d += timedelta(days=1)
    while d <= end:
        result.append(d)
        d += timedelta(weeks=1)
    return result


def _every_n_biz_days(start: date, end: date, n: int = 3) -> list[date]:
    """Every n business days."""
    result, d = [], start
    if not _is_business_day(d):
        d = _next_biz(d)
    while d <= end:
        result.append(d)
        count, nxt = 0, d + timedelta(days=1)
        while count < n:
            if _is_business_day(nxt):
                count += 1
            nxt += timedelta(days=1)
        d = nxt - timedelta(days=1)
    return result


def _monthly_dates(start: date, end: date, dom: int = 5) -> list[date]:
    result = []
    cur = date(start.year, start.month, 1)
    while cur <= end:
        try:
            d = _next_biz(date(cur.year, cur.month, dom))
        except ValueError:
            d = _next_biz(date(cur.year, cur.month, 28))
        if start <= d <= end:
            result.append(d)
        cur = date(cur.year + (cur.month // 12), cur.month % 12 + 1, 1)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# File spec builder
# ─────────────────────────────────────────────────────────────────────────────

def _drop_time(profile: dict) -> tuple[int, int]:
    lo, hi = profile["drop_hour_range"]
    h = _rng.randint(lo, hi - 1)
    m = _rng.randint(0, 59)
    return h % 24, m


def _file_spec(profile: dict, folder: str, d: date) -> dict:
    h, m     = _drop_time(profile)
    entries  = _make_entries(profile, d)
    batches  = _split_batches(entries)
    filename = f"{profile['code']}_ACH_{d.strftime('%Y%m%d')}_{h:02d}{m:02d}{_rng.randint(0,59):02d}.ach"
    return {
        "folder":   folder,
        "name":     filename,
        "profile":  profile,
        "batches":  batches,
        "drop_date": d,
        "drop_hour": h,
        "drop_min":  m,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Build file lists
# ─────────────────────────────────────────────────────────────────────────────

TODAY        = date(2026, 6, 10)
ACTIVE_START = TODAY - timedelta(days=60)   # Apr 11, 2026
ACTIVE_END   = TODAY

ARCHIVE_START = date(2025, 6, 1)
ARCHIVE_END   = date(2026, 5, 31)


def _build_active_specs() -> list[dict]:
    specs = []
    p = PROFILES

    # ACME: bi-weekly (1st + 15th)
    for d in _biweekly_dates(ACTIVE_START, ACTIVE_END):
        specs.append(_file_spec(p["ACME"], "Active", d))

    # SWIFT: every Friday
    for d in _weekly_dates(ACTIVE_START, ACTIVE_END, weekday=4):
        specs.append(_file_spec(p["SWIFT"], "Active", d))

    # NEXUS: 5th and 20th
    for d in _biweekly_dates(ACTIVE_START, ACTIVE_END, day1=5, day2=20):
        specs.append(_file_spec(p["NEXUS"], "Active", d))

    # MERIDIAN: 1st and 15th
    for d in _biweekly_dates(ACTIVE_START, ACTIVE_END, day1=1, day2=15):
        specs.append(_file_spec(p["MERIDIAN"], "Active", d))

    # COASTAL: every 3 business days
    for d in _every_n_biz_days(ACTIVE_START, ACTIVE_END, n=3):
        specs.append(_file_spec(p["COASTAL"], "Active", d))

    # VERTEX: 10th and 25th
    for d in _biweekly_dates(ACTIVE_START, ACTIVE_END, day1=10, day2=25):
        specs.append(_file_spec(p["VERTEX"], "Active", d))

    return sorted(specs, key=lambda s: (s["drop_date"], s["folder"]))


def _build_archive_specs() -> list[dict]:
    specs = []
    p = PROFILES

    # ACME: bi-weekly
    for d in _biweekly_dates(ARCHIVE_START, ARCHIVE_END):
        specs.append(_file_spec(p["ACME"], "Archive", d))

    # SWIFT: weekly (Friday)
    for d in _weekly_dates(ARCHIVE_START, ARCHIVE_END, weekday=4):
        specs.append(_file_spec(p["SWIFT"], "Archive", d))

    # NEXUS: 5th and 20th
    for d in _biweekly_dates(ARCHIVE_START, ARCHIVE_END, day1=5, day2=20):
        specs.append(_file_spec(p["NEXUS"], "Archive", d))

    # MERIDIAN: monthly on the 3rd
    for d in _monthly_dates(ARCHIVE_START, ARCHIVE_END, dom=3):
        specs.append(_file_spec(p["MERIDIAN"], "Archive", d))

    # COASTAL: every 3 business days
    for d in _every_n_biz_days(ARCHIVE_START, ARCHIVE_END, n=3):
        specs.append(_file_spec(p["COASTAL"], "Archive", d))

    # VERTEX: 10th and 25th
    for d in _biweekly_dates(ARCHIVE_START, ARCHIVE_END, day1=10, day2=25):
        specs.append(_file_spec(p["VERTEX"], "Archive", d))

    # HARBOR: weekly until last_active (Feb 2026)
    harbor_end = min(ARCHIVE_END, p["HARBOR"]["last_active"])
    for d in _weekly_dates(ARCHIVE_START, harbor_end, weekday=2):  # Wednesdays
        specs.append(_file_spec(p["HARBOR"], "Archive", d))

    # PINNACLE: monthly until last_active (Oct 2025)
    pinnacle_end = min(ARCHIVE_END, p["PINNACLE"]["last_active"])
    for d in _monthly_dates(ARCHIVE_START, pinnacle_end, dom=8):
        specs.append(_file_spec(p["PINNACLE"], "Archive", d))

    return sorted(specs, key=lambda s: (s["drop_date"], s["folder"]))


# ─────────────────────────────────────────────────────────────────────────────
# MinIO helpers
# ─────────────────────────────────────────────────────────────────────────────

def _s3_client():
    return boto3.client(
        "s3",
        endpoint_url=MINIO_ENDPOINT,
        aws_access_key_id=MINIO_ACCESS_KEY,
        aws_secret_access_key=MINIO_SECRET_KEY,
        config=Config(signature_version="s3v4"),
        region_name="us-east-1",
    )


def _wait_for_minio(s3, retries: int = 20, delay: float = 3.0):
    for attempt in range(1, retries + 1):
        try:
            s3.list_buckets()
            print(f"MinIO ready (attempt {attempt})")
            return
        except Exception as exc:
            print(f"  MinIO not ready yet ({exc}) – retrying in {delay}s …")
            time.sleep(delay)
    raise RuntimeError(f"MinIO did not become ready after {retries} attempts")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    s3 = _s3_client()
    _wait_for_minio(s3)

    try:
        s3.create_bucket(Bucket=BUCKET)
        print(f"Created bucket: {BUCKET}")
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("BucketAlreadyExists", "BucketAlreadyOwnedByYou"):
            print(f"Bucket '{BUCKET}' already exists – skipping.")
        else:
            raise RuntimeError(f"Bucket create error: {e}") from e

    active_specs  = _build_active_specs()
    archive_specs = _build_archive_specs()
    all_specs     = active_specs + archive_specs

    uploaded = failed = 0
    folder_counts: dict[str, int] = {}

    for spec in all_specs:
        key = f"{spec['folder']}/{spec['name']}"
        try:
            content = generate_ach(
                profile=spec["profile"],
                batches_entries=spec["batches"],
                drop_date=spec["drop_date"],
                drop_hour=spec["drop_hour"],
                drop_minute=spec["drop_min"],
            )
            s3.put_object(Bucket=BUCKET, Key=key, Body=content.encode("ascii"))
            print(f"  ✓ {key}")
            uploaded += 1
            folder_counts[spec["folder"]] = folder_counts.get(spec["folder"], 0) + 1
        except Exception as exc:
            print(f"  ✗ ERROR {key}: {exc}")
            failed += 1

    print(f"\nSeed complete – {uploaded} files uploaded, {failed} failed.")
    for folder, count in sorted(folder_counts.items()):
        print(f"  payments/{folder}/  →  {count} files")

    if failed:
        raise SystemExit(f"{failed} file(s) failed – check logs above")


if __name__ == "__main__":
    main()
