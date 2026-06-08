"""
MSSQL database tools.
Queries client records and transaction history matched against ACH company data.
"""

import json
import logging
from typing import Any
import pymssql

logger = logging.getLogger(__name__)


def _serialize(row: dict) -> dict:
    """Make every value JSON-serialisable."""
    out = {}
    for k, v in row.items():
        if hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        elif isinstance(v, (int, float, str, bool, type(None))):
            out[k] = v
        else:
            out[k] = float(v) if hasattr(v, "__float__") else str(v)
    return out


class DbTools:
    def __init__(self, server: str, database: str, username: str, password: str):
        self._cfg = dict(server=server, user=username, password=password, database=database)

    def _conn(self):
        return pymssql.connect(**self._cfg, as_dict=True, login_timeout=10)

    # ------------------------------------------------------------------ #

    def get_client_info(self, company_identifiers: list[str]) -> str:
        """
        Lookup clients by ACH company ID or company name.
        Returns profile + lifetime transaction totals.
        """
        if not company_identifiers:
            return json.dumps({"error": "No identifiers provided"})

        placeholders = ", ".join(["%s"] * len(company_identifiers))
        query = f"""
            SELECT
                c.client_id,
                c.company_name,
                c.ach_company_id,
                c.account_status,
                c.credit_limit,
                c.industry,
                c.risk_rating,
                c.onboarded_date,
                c.last_activity_date,
                COUNT(t.transaction_id)                                         AS total_transactions,
                SUM(CASE WHEN t.transaction_type = 'CREDIT' THEN t.amount
                         ELSE 0 END)                                            AS lifetime_credits,
                SUM(CASE WHEN t.transaction_type = 'DEBIT'  THEN t.amount
                         ELSE 0 END)                                            AS lifetime_debits
            FROM clients c
            LEFT JOIN client_transactions t ON c.client_id = t.client_id
            WHERE c.ach_company_id IN ({placeholders})
               OR c.company_name    IN ({placeholders})
            GROUP BY
                c.client_id, c.company_name, c.ach_company_id,
                c.account_status, c.credit_limit, c.industry,
                c.risk_rating, c.onboarded_date, c.last_activity_date
        """
        try:
            with self._conn() as conn:
                cur = conn.cursor()
                cur.execute(query, company_identifiers * 2)
                rows = [_serialize(r) for r in cur.fetchall()]

            return json.dumps(
                {
                    "query": "client_info",
                    "identifiers_searched": company_identifiers,
                    "match_count": len(rows),
                    "clients": rows,
                },
                indent=2,
            )
        except Exception as e:
            logger.exception("get_client_info error")
            return json.dumps({"error": str(e)})

    # ------------------------------------------------------------------ #

    def get_client_recent_transactions(self, client_id: str, days: int = 30) -> str:
        """Return recent transactions for a single client."""
        query = """
            SELECT
                t.transaction_id,
                t.client_id,
                CONVERT(VARCHAR, t.transaction_date, 23) AS transaction_date,
                t.amount,
                t.transaction_type,
                t.status,
                t.description,
                t.reference_number
            FROM client_transactions t
            WHERE t.client_id = %s
              AND t.transaction_date >= DATEADD(day, -%d, GETDATE())
            ORDER BY t.transaction_date DESC
        """
        try:
            with self._conn() as conn:
                cur = conn.cursor()
                cur.execute(query % ("%s", days), (client_id,))
                rows = [_serialize(r) for r in cur.fetchall()]

            total_credit = sum(float(r["amount"]) for r in rows if r["transaction_type"] == "CREDIT")
            total_debit  = sum(float(r["amount"]) for r in rows if r["transaction_type"] == "DEBIT")

            return json.dumps(
                {
                    "client_id": client_id,
                    "period_days": days,
                    "transaction_count": len(rows),
                    "total_credit": round(total_credit, 2),
                    "total_debit": round(total_debit, 2),
                    "net_position": round(total_credit - total_debit, 2),
                    "transactions": rows,
                },
                indent=2,
            )
        except Exception as e:
            logger.exception("get_client_recent_transactions error")
            return json.dumps({"error": str(e)})

    # ------------------------------------------------------------------ #

    def get_all_clients(self) -> str:
        """Return a full list of clients (no transaction detail)."""
        query = """
            SELECT client_id, company_name, ach_company_id,
                   account_status, credit_limit, industry, risk_rating
            FROM clients
            ORDER BY company_name
        """
        try:
            with self._conn() as conn:
                cur = conn.cursor()
                cur.execute(query)
                rows = [_serialize(r) for r in cur.fetchall()]
            return json.dumps({"clients": rows, "count": len(rows)}, indent=2)
        except Exception as e:
            logger.exception("get_all_clients error")
            return json.dumps({"error": str(e)})
