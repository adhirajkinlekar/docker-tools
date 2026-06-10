"""
Seed script – generates valid NACHA ACH files and uploads them to MinIO.

Folder structure in the 'payments' bucket:
  Active/   ← live payment files (ACME CORP + PAYROLL EXPRESS)
  Archive/  ← RAG-indexed historical files (NEXUS PAYMENTS + DIGITAL MERCHANTS)

Each folder contains current files (June 2026) plus 5 months of historical files
(January–May 2026) to give the agent context for trend analysis.

Historical patterns baked in:
  ACME CORP        – steady credits, ~5% month-on-month growth
  PAYROLL EXPRESS  – consistent large payroll; March has anomalous spike (bonus run)

Company IDs match the clients seeded in PaymentDB.
"""

import math
import os
import time

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

MINIO_ENDPOINT   = os.environ.get("MINIO_ENDPOINT",   "http://minio:9000")
MINIO_ACCESS_KEY = os.environ.get("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.environ.get("MINIO_SECRET_KEY", "minioadmin")
BUCKET = "payments"


def _s3_client():
    return boto3.client(
        "s3",
        endpoint_url=MINIO_ENDPOINT,
        aws_access_key_id=MINIO_ACCESS_KEY,
        aws_secret_access_key=MINIO_SECRET_KEY,
        config=Config(signature_version="s3v4"),
        region_name="us-east-1",
    )


# ─────────────────────────────────────────────────────────────────────────────
# NACHA record builders (all records must be exactly 94 characters)
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
        "1"                           # Record Type Code
        + "01"                        # Priority Code
        + " " + _pad(dest_routing, 9) # Immediate Destination (space + 9 digits)
        + _pad(origin_routing, 10)    # Immediate Origin
        + _pad(creation_date, 6)      # File Creation Date
        + "    "                      # File Creation Time (blank)
        + file_id[0]                  # File ID Modifier
        + "094"                       # Record Size
        + "10"                        # Blocking Factor
        + "1"                         # Format Code
        + _pad(dest_name, 23)         # Immediate Destination Name
        + _pad(origin_name, 23)       # Immediate Origin Name
        + "        "                  # Reference Code
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
        "5"                              # Record Type
        + _pad(service_class, 3)         # Service Class Code
        + _pad(company_name, 16)         # Company Name
        + _pad("", 20)                   # Company Discretionary Data
        + _pad(company_id, 10)           # Company Identification
        + _pad(entry_class, 3)           # Standard Entry Class
        + _pad(entry_desc, 10)           # Company Entry Description
        + "      "                        # Company Descriptive Date
        + _pad(effective_date, 6)         # Effective Entry Date
        + "   "                           # Settlement Date (bank fills)
        + "1"                             # Originator Status Code
        + _pad(orig_dfi, 8)              # Originating DFI Identification
        + _pad(str(batch_number), 7, align="right", fill="0")  # Batch Number
    )
    assert len(line) == 94, f"Batch header wrong length: {len(line)}"
    return line


def build_entry_detail(
    transaction_code: str,
    recv_routing: str,     # 8 digits
    check_digit: str,      # 1 digit
    amount_cents: int,
    seq: int,
    orig_dfi_short: str,   # 8 digits, used in trace number
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


def generate_ach(
    company_name: str,
    company_id: str,
    effective_date: str,   # YYMMDD
    entries: list[dict],   # [{amount: float, transaction_code: str}, ...]
    dest_routing: str = "076000120",
    origin_routing: str = "0000000000",
    orig_dfi: str = "07600012",
) -> str:
    DEBIT_CODES  = {"27", "28", "29", "37", "38", "55"}
    recv_routing = "121000248"
    recv_check   = "6"

    creation_date = effective_date
    lines = []

    lines.append(
        build_file_header(dest_routing, origin_routing, creation_date,
                          "BANK OF AMERICA", company_name[:23])
    )
    lines.append(
        build_batch_header("200", company_name, company_id,
                           "PPD", "PAYROLL", effective_date, orig_dfi, 1)
    )

    entry_hash   = 0
    total_debit  = 0
    total_credit = 0

    for i, e in enumerate(entries, start=1):
        tc           = e.get("transaction_code", "22")
        amount_cents = int(round(e["amount"] * 100))
        lines.append(
            build_entry_detail(tc, recv_routing[:8], recv_check,
                               amount_cents, i, orig_dfi, 1)
        )
        entry_hash += int(recv_routing[:8])
        if tc in DEBIT_CODES:
            total_debit  += amount_cents
        else:
            total_credit += amount_cents

    n_entries = len(entries)
    lines.append(
        build_batch_control("200", n_entries, entry_hash,
                            total_debit, total_credit, company_id, orig_dfi, 1)
    )
    lines.append(
        build_file_control(1, len(lines), n_entries, entry_hash,
                           total_debit, total_credit)
    )

    while len(lines) % 10 != 0:
        lines.append("9" * 94)

    return "\n".join(lines) + "\n"


# ─────────────────────────────────────────────────────────────────────────────
# Sample data – current (June 2026) + historical (Jan–May 2026)
# ─────────────────────────────────────────────────────────────────────────────

SAMPLES = [

    # ═══════════════════════════════════════════════════════════════
    # Active
    # ═══════════════════════════════════════════════════════════════

    # ── ACME CORP: steady credits, ~5% month-on-month growth ─────
    {
        "folder": "Active", "name": "ACH_20260115_ACME.ach",
        "company_name": "ACME CORP", "company_id": "1234567890",
        "effective_date": "260115",
        "entries": [
            {"amount":  950.00, "transaction_code": "22"},
            {"amount": 2800.00, "transaction_code": "22"},
            {"amount":  700.00, "transaction_code": "27"},
            {"amount": 4100.00, "transaction_code": "22"},
            {"amount":  880.00, "transaction_code": "22"},
        ],
    },
    {
        "folder": "Active", "name": "ACH_20260215_ACME.ach",
        "company_name": "ACME CORP", "company_id": "1234567890",
        "effective_date": "260215",
        "entries": [
            {"amount": 1000.00, "transaction_code": "22"},
            {"amount": 2950.00, "transaction_code": "22"},
            {"amount":  730.00, "transaction_code": "27"},
            {"amount": 4350.00, "transaction_code": "22"},
            {"amount":  930.00, "transaction_code": "22"},
        ],
    },
    {
        "folder": "Active", "name": "ACH_20260315_ACME.ach",
        "company_name": "ACME CORP", "company_id": "1234567890",
        "effective_date": "260315",
        "entries": [
            {"amount": 1060.00, "transaction_code": "22"},
            {"amount": 3100.00, "transaction_code": "22"},
            {"amount":  760.00, "transaction_code": "27"},
            {"amount": 4580.00, "transaction_code": "22"},
            {"amount":  980.00, "transaction_code": "22"},
        ],
    },
    {
        "folder": "Active", "name": "ACH_20260415_ACME.ach",
        "company_name": "ACME CORP", "company_id": "1234567890",
        "effective_date": "260415",
        "entries": [
            {"amount": 1120.00, "transaction_code": "22"},
            {"amount": 3250.00, "transaction_code": "22"},
            {"amount":  810.00, "transaction_code": "27"},
            {"amount": 4820.00, "transaction_code": "22"},
            {"amount": 1040.00, "transaction_code": "22"},
        ],
    },
    {
        "folder": "Active", "name": "ACH_20260515_ACME.ach",
        "company_name": "ACME CORP", "company_id": "1234567890",
        "effective_date": "260515",
        "entries": [
            {"amount": 1180.00, "transaction_code": "22"},
            {"amount": 3320.00, "transaction_code": "22"},
            {"amount":  845.00, "transaction_code": "27"},
            {"amount": 5050.00, "transaction_code": "22"},
            {"amount": 1070.00, "transaction_code": "22"},
        ],
    },
    # June 2026 – current
    {
        "folder": "Active", "name": "ACH_20260601_ACME.ach",
        "company_name": "ACME CORP", "company_id": "1234567890",
        "effective_date": "260601",
        "entries": [
            {"amount": 1250.00, "transaction_code": "22"},
            {"amount": 3400.50, "transaction_code": "22"},
            {"amount":  875.25, "transaction_code": "27"},
            {"amount": 5200.00, "transaction_code": "22"},
            {"amount": 1100.75, "transaction_code": "22"},
        ],
    },

    # ── PAYROLL EXPRESS: consistent payroll; March bonus spike ───
    {
        "folder": "Active", "name": "ACH_20260115_PAYROLL.ach",
        "company_name": "PAYROLL EXPRESS", "company_id": "9988776655",
        "effective_date": "260115",
        "entries": [
            {"amount": 42000.00, "transaction_code": "22"},
            {"amount": 30000.00, "transaction_code": "22"},
            {"amount": 26500.00, "transaction_code": "22"},
            {"amount": 14000.00, "transaction_code": "27"},
        ],
    },
    {
        "folder": "Active", "name": "ACH_20260215_PAYROLL.ach",
        "company_name": "PAYROLL EXPRESS", "company_id": "9988776655",
        "effective_date": "260215",
        "entries": [
            {"amount": 43000.00, "transaction_code": "22"},
            {"amount": 30500.00, "transaction_code": "22"},
            {"amount": 27000.00, "transaction_code": "22"},
            {"amount": 14200.00, "transaction_code": "27"},
        ],
    },
    # March – annual bonus run: amounts ~50% higher than normal
    {
        "folder": "Active", "name": "ACH_20260315_PAYROLL.ach",
        "company_name": "PAYROLL EXPRESS", "company_id": "9988776655",
        "effective_date": "260315",
        "entries": [
            {"amount": 67500.00, "transaction_code": "22"},
            {"amount": 48000.00, "transaction_code": "22"},
            {"amount": 41000.00, "transaction_code": "22"},
            {"amount": 14500.00, "transaction_code": "27"},
            {"amount": 21000.00, "transaction_code": "22"},  # extra bonus batch entry
        ],
    },
    {
        "folder": "Active", "name": "ACH_20260415_PAYROLL.ach",
        "company_name": "PAYROLL EXPRESS", "company_id": "9988776655",
        "effective_date": "260415",
        "entries": [
            {"amount": 44000.00, "transaction_code": "22"},
            {"amount": 31000.00, "transaction_code": "22"},
            {"amount": 27500.00, "transaction_code": "22"},
            {"amount": 14800.00, "transaction_code": "27"},
        ],
    },
    {
        "folder": "Active", "name": "ACH_20260515_PAYROLL.ach",
        "company_name": "PAYROLL EXPRESS", "company_id": "9988776655",
        "effective_date": "260515",
        "entries": [
            {"amount": 44500.00, "transaction_code": "22"},
            {"amount": 31500.00, "transaction_code": "22"},
            {"amount": 28000.00, "transaction_code": "22"},
            {"amount": 15000.00, "transaction_code": "27"},
        ],
    },
    # June 2026 – current
    {
        "folder": "Active", "name": "ACH_20260602_PAYROLL.ach",
        "company_name": "PAYROLL EXPRESS", "company_id": "9988776655",
        "effective_date": "260602",
        "entries": [
            {"amount": 45000.00, "transaction_code": "22"},
            {"amount": 32000.00, "transaction_code": "22"},
            {"amount": 28500.00, "transaction_code": "22"},
            {"amount": 15000.00, "transaction_code": "27"},
        ],
    },

    # Payment-Files-Archive  (RAG-indexed folder)
    # ═══════════════════════════════════════════════════════════════

    # ── NEXUS PAYMENTS: steady B2B vendor payments, ~8% MoM growth ─
    {
        "folder": "Archive", "name": "ACH_20260115_NEXUS.ach",
        "company_name": "NEXUS PAYMENTS", "company_id": "7654321098",
        "effective_date": "260115",
        "entries": [
            {"amount": 15200.00, "transaction_code": "22"},
            {"amount": 22500.00, "transaction_code": "22"},
            {"amount":  4800.00, "transaction_code": "27"},
        ],
    },
    {
        "folder": "Archive", "name": "ACH_20260215_NEXUS.ach",
        "company_name": "NEXUS PAYMENTS", "company_id": "7654321098",
        "effective_date": "260215",
        "entries": [
            {"amount": 16400.00, "transaction_code": "22"},
            {"amount": 24300.00, "transaction_code": "22"},
            {"amount":  5100.00, "transaction_code": "27"},
        ],
    },
    {
        "folder": "Archive", "name": "ACH_20260315_NEXUS.ach",
        "company_name": "NEXUS PAYMENTS", "company_id": "7654321098",
        "effective_date": "260315",
        "entries": [
            {"amount": 17700.00, "transaction_code": "22"},
            {"amount": 26200.00, "transaction_code": "22"},
            {"amount":  5500.00, "transaction_code": "27"},
        ],
    },
    {
        "folder": "Archive", "name": "ACH_20260415_NEXUS.ach",
        "company_name": "NEXUS PAYMENTS", "company_id": "7654321098",
        "effective_date": "260415",
        "entries": [
            {"amount": 19100.00, "transaction_code": "22"},
            {"amount": 28300.00, "transaction_code": "22"},
            {"amount":  5900.00, "transaction_code": "27"},
        ],
    },
    {
        "folder": "Archive", "name": "ACH_20260515_NEXUS.ach",
        "company_name": "NEXUS PAYMENTS", "company_id": "7654321098",
        "effective_date": "260515",
        "entries": [
            {"amount": 20600.00, "transaction_code": "22"},
            {"amount": 30500.00, "transaction_code": "22"},
            {"amount":  6400.00, "transaction_code": "27"},
        ],
    },
    {
        "folder": "Archive", "name": "ACH_20260601_NEXUS.ach",
        "company_name": "NEXUS PAYMENTS", "company_id": "7654321098",
        "effective_date": "260601",
        "entries": [
            {"amount": 22200.00, "transaction_code": "22"},
            {"amount": 33000.00, "transaction_code": "22"},
            {"amount":  6900.00, "transaction_code": "27"},
        ],
    },

    # ── DIGITAL MERCHANTS: high-volume but volatile – big debits spike in Apr/May
    {
        "folder": "Archive", "name": "ACH_20260115_DIGMERCH.ach",
        "company_name": "DIGITAL MERCHANTS", "company_id": "3344556677",
        "effective_date": "260115",
        "entries": [
            {"amount": 98000.00, "transaction_code": "22"},
            {"amount": 71500.00, "transaction_code": "22"},
            {"amount": 12000.00, "transaction_code": "27"},
        ],
    },
    {
        "folder": "Archive", "name": "ACH_20260215_DIGMERCH.ach",
        "company_name": "DIGITAL MERCHANTS", "company_id": "3344556677",
        "effective_date": "260215",
        "entries": [
            {"amount": 103000.00, "transaction_code": "22"},
            {"amount":  75000.00, "transaction_code": "22"},
            {"amount":  13500.00, "transaction_code": "27"},
        ],
    },
    {
        "folder": "Archive", "name": "ACH_20260315_DIGMERCH.ach",
        "company_name": "DIGITAL MERCHANTS", "company_id": "3344556677",
        "effective_date": "260315",
        "entries": [
            {"amount": 108000.00, "transaction_code": "22"},
            {"amount":  79000.00, "transaction_code": "22"},
            {"amount":  15000.00, "transaction_code": "27"},
        ],
    },
    # April: debit spike – large reversal batch
    {
        "folder": "Archive", "name": "ACH_20260415_DIGMERCH.ach",
        "company_name": "DIGITAL MERCHANTS", "company_id": "3344556677",
        "effective_date": "260415",
        "entries": [
            {"amount": 112000.00, "transaction_code": "22"},
            {"amount":  80000.00, "transaction_code": "22"},
            {"amount":  55000.00, "transaction_code": "27"},   # reversal spike
            {"amount":  22000.00, "transaction_code": "27"},
        ],
    },
    # May: recovery but still elevated debits
    {
        "folder": "Archive", "name": "ACH_20260515_DIGMERCH.ach",
        "company_name": "DIGITAL MERCHANTS", "company_id": "3344556677",
        "effective_date": "260515",
        "entries": [
            {"amount": 115000.00, "transaction_code": "22"},
            {"amount":  82000.00, "transaction_code": "22"},
            {"amount":  28000.00, "transaction_code": "27"},
        ],
    },
    # June: stabilising
    {
        "folder": "Archive", "name": "ACH_20260601_DIGMERCH.ach",
        "company_name": "DIGITAL MERCHANTS", "company_id": "3344556677",
        "effective_date": "260601",
        "entries": [
            {"amount": 118000.00, "transaction_code": "22"},
            {"amount":  85000.00, "transaction_code": "22"},
            {"amount":  14000.00, "transaction_code": "27"},
        ],
    },


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def _wait_for_minio(s3, retries: int = 20, delay: float = 3.0):
    """Poll MinIO until it responds, with retries."""
    for attempt in range(1, retries + 1):
        try:
            s3.list_buckets()
            print(f"MinIO ready (attempt {attempt})")
            return
        except Exception as exc:
            print(f"  MinIO not ready yet ({exc}) – retrying in {delay}s …")
            time.sleep(delay)
    raise RuntimeError(f"MinIO did not become ready after {retries} attempts")


def main():
    s3 = _s3_client()
    _wait_for_minio(s3)

    # Create bucket
    try:
        s3.create_bucket(Bucket=BUCKET)
        print(f"Created bucket: {BUCKET}")
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("BucketAlreadyExists", "BucketAlreadyOwnedByYou"):
            print(f"Bucket '{BUCKET}' already exists – skipping creation.")
        else:
            raise RuntimeError(f"Bucket create error: {e}") from e

    uploaded = 0
    failed   = 0
    for spec in SAMPLES:
        key = f"{spec['folder']}/{spec['name']}"
        try:
            content = generate_ach(
                company_name=spec["company_name"],
                company_id=spec["company_id"],
                effective_date=spec["effective_date"],
                entries=spec["entries"],
            )
            s3.put_object(Bucket=BUCKET, Key=key, Body=content.encode("ascii"))
            print(f"  Uploaded: {key}  ({len(content)} bytes)")
            uploaded += 1
        except Exception as exc:
            print(f"  ERROR uploading {key}: {exc}")
            failed += 1

    print(f"\nSeed complete – {uploaded} ACH files uploaded, {failed} failed.")
    if failed:
        raise SystemExit(f"{failed} file(s) failed to upload – check logs above")
    print("\nFolders available:")
    for folder in sorted({s["folder"] for s in SAMPLES}):
        count = sum(1 for s in SAMPLES if s["folder"] == folder)
        print(f"  payments/{folder}/  ({count} files: Jan–Jun 2026)")


if __name__ == "__main__":
    main()
