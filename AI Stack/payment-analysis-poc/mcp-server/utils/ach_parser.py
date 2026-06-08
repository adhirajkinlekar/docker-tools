"""
NACHA ACH file parser.
Extracts non-PII fields only. All personal identifiers are replaced with [REDACTED].

NACHA record layout (all records are exactly 94 chars):
  1 = File Header
  5 = Batch Header
  6 = Entry Detail    ← account#, individual ID, individual name → REDACTED
  7 = Addenda
  8 = Batch Control
  9 = File Control / padding
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Transaction code descriptions (no PII)
TRANSACTION_CODES: dict[str, str] = {
    "22": "Checking Credit – Live",
    "23": "Checking Credit – Prenote",
    "24": "Checking Credit – Zero-dollar",
    "27": "Checking Debit – Live",
    "28": "Checking Debit – Prenote",
    "29": "Checking Debit – Zero-dollar",
    "32": "Savings Credit – Live",
    "33": "Savings Credit – Prenote",
    "37": "Savings Debit – Live",
    "38": "Savings Debit – Prenote",
    "52": "Loan Credit – Live",
    "55": "Loan Debit – Live",
}

DEBIT_CODES = {"27", "28", "29", "37", "38", "55"}


class AchParser:
    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def parse(self, content: str) -> dict:
        """Parse a full NACHA file string; return non-PII structured data."""
        lines = self._extract_records(content)

        result: dict = {
            "file_header": None,
            "batches": [],
            "file_control": None,
            "summary": {
                "total_entries": 0,
                "total_debit_dollars": 0.0,
                "total_credit_dollars": 0.0,
                "company_names": [],
                "entry_class_codes": [],
                "effective_dates": [],
            },
        }

        current_batch: Optional[dict] = None

        for line in lines:
            rt = line[0]

            if rt == "1":
                result["file_header"] = self._parse_file_header(line)

            elif rt == "5":
                current_batch = self._parse_batch_header(line)
                current_batch["entries"] = []
                result["batches"].append(current_batch)

            elif rt == "6":
                entry = self._parse_entry_detail(line)
                if current_batch is not None:
                    current_batch["entries"].append(entry)

                result["summary"]["total_entries"] += 1
                amount = entry["amount_dollars"]
                if entry["transaction_code"] in DEBIT_CODES:
                    result["summary"]["total_debit_dollars"] = round(
                        result["summary"]["total_debit_dollars"] + amount, 2
                    )
                else:
                    result["summary"]["total_credit_dollars"] = round(
                        result["summary"]["total_credit_dollars"] + amount, 2
                    )

            elif rt == "8" and current_batch is not None:
                current_batch["control"] = self._parse_batch_control(line)

            elif rt == "9" and line.strip("9") != "":  # skip padding rows of 9s
                result["file_control"] = self._parse_file_control(line)

        # Build high-level summary
        for batch in result["batches"]:
            cn = (batch.get("company_name") or "").strip()
            if cn and cn not in result["summary"]["company_names"]:
                result["summary"]["company_names"].append(cn)

            ecc = (batch.get("standard_entry_class") or "").strip()
            if ecc and ecc not in result["summary"]["entry_class_codes"]:
                result["summary"]["entry_class_codes"].append(ecc)

            ed = batch.get("effective_entry_date")
            if ed and ed not in result["summary"]["effective_dates"]:
                result["summary"]["effective_dates"].append(ed)

        return result

    # ------------------------------------------------------------------ #
    # Record parsers                                                       #
    # ------------------------------------------------------------------ #

    def _parse_file_header(self, line: str) -> dict:
        return {
            "record_type": "File Header",
            "priority_code": line[1:3].strip(),
            "immediate_destination": line[3:13].strip(),  # receiving bank routing – not PII
            "immediate_origin": line[13:23].strip(),      # originating bank routing – not PII
            "file_creation_date": self._fmt_date(line[23:29]),
            "file_creation_time": line[29:33].strip() or None,
            "file_id_modifier": line[33],
            "destination_name": line[40:63].strip(),      # bank name – not PII
            "origin_name": line[63:86].strip(),           # company name – not PII
        }

    def _parse_batch_header(self, line: str) -> dict:
        return {
            "record_type": "Batch Header",
            "service_class_code": line[1:4].strip(),
            "company_name": line[4:20].strip(),           # originating company – not PII
            "company_identification": line[40:50].strip(),# company tax/ID – not personal PII
            "standard_entry_class": line[50:53].strip(),
            "company_entry_description": line[53:63].strip(),
            "effective_entry_date": self._fmt_date(line[69:75]),
            "originating_dfi": line[79:87].strip(),       # bank routing – not PII
            "batch_number": line[87:94].strip(),
        }

    def _parse_entry_detail(self, line: str) -> dict:
        """Parse entry detail record. PII fields are replaced with [REDACTED]."""
        tc = line[1:3].strip()
        raw_amount = line[29:39].strip()
        amount_cents = int(raw_amount) if raw_amount.isdigit() else 0

        return {
            "record_type": "Entry Detail",
            "transaction_code": tc,
            "transaction_description": TRANSACTION_CODES.get(tc, f"Code {tc}"),
            # Bank routing number of the receiving financial institution – not personal PII
            "receiving_dfi_routing": line[3:11].strip(),
            "check_digit": line[11],
            "amount_cents": amount_cents,
            "amount_dollars": round(amount_cents / 100.0, 2),
            # ── PII fields – intentionally redacted ────────────────────
            "dfi_account_number": "[REDACTED]",
            "individual_id": "[REDACTED]",
            "individual_name": "[REDACTED]",
            # ────────────────────────────────────────────────────────────
            "addenda_indicator": line[78],
            "trace_number": line[79:94].strip(),
        }

    def _parse_batch_control(self, line: str) -> dict:
        debit_raw = line[20:32].strip()
        credit_raw = line[32:44].strip()
        return {
            "service_class_code": line[1:4].strip(),
            "entry_count": int(line[4:10]) if line[4:10].strip().isdigit() else 0,
            "entry_hash": line[10:20].strip(),
            "total_debit_cents": int(debit_raw) if debit_raw.isdigit() else 0,
            "total_credit_cents": int(credit_raw) if credit_raw.isdigit() else 0,
            "total_debit_dollars": round(int(debit_raw) / 100.0, 2) if debit_raw.isdigit() else 0.0,
            "total_credit_dollars": round(int(credit_raw) / 100.0, 2) if credit_raw.isdigit() else 0.0,
            "company_identification": line[44:54].strip(),
            "originating_dfi": line[79:87].strip(),
            "batch_number": line[87:94].strip(),
        }

    def _parse_file_control(self, line: str) -> dict:
        def safe_int(s: str) -> int:
            s = s.strip()
            return int(s) if s.isdigit() else 0

        return {
            "batch_count": safe_int(line[1:7]),
            "block_count": safe_int(line[7:13]),
            "entry_addenda_count": safe_int(line[13:21]),
            "entry_hash": line[21:31].strip(),
            "total_debit_cents": safe_int(line[31:43]),
            "total_credit_cents": safe_int(line[43:55]),
            "total_debit_dollars": round(safe_int(line[31:43]) / 100.0, 2),
            "total_credit_dollars": round(safe_int(line[43:55]) / 100.0, 2),
        }

    # ------------------------------------------------------------------ #
    # Helpers                                                              #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _extract_records(content: str) -> list[str]:
        """Return lines that are valid 94-char NACHA records."""
        records = []
        for raw in content.splitlines():
            line = raw.rstrip("\r")
            if len(line) >= 94:
                records.append(line[:94])
            elif len(line) > 0:
                records.append(line.ljust(94))
        return records

    @staticmethod
    def _fmt_date(yymmdd: str) -> Optional[str]:
        s = yymmdd.strip()
        if len(s) == 6 and s.isdigit():
            yy, mm, dd = s[:2], s[2:4], s[4:6]
            year = f"20{yy}" if int(yy) < 70 else f"19{yy}"
            return f"{year}-{mm}-{dd}"
        return s or None
