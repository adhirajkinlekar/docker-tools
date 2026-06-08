"""
MinIO / S3 storage tools.
Lists folders (key prefixes) and fetches + parses ACH payment files.
"""

import json
import logging
import boto3
from botocore.client import Config
from botocore.exceptions import ClientError
from utils.ach_parser import AchParser

logger = logging.getLogger(__name__)

ACH_EXTENSIONS = {".ach", ".txt", ".dat", ""}


class BlobTools:
    def __init__(self, endpoint: str, access_key: str, secret_key: str):
        self.s3 = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=Config(signature_version="s3v4"),
            region_name="us-east-1",
        )
        self.parser = AchParser()

    # ------------------------------------------------------------------ #

    def list_folders(self, bucket: str) -> str:
        """List virtual folders and files in a MinIO bucket."""
        try:
            paginator = self.s3.get_paginator("list_objects_v2")
            pages = paginator.paginate(Bucket=bucket, Delimiter="/")

            folders, files = [], []
            for page in pages:
                for prefix in page.get("CommonPrefixes") or []:
                    folders.append(prefix["Prefix"].rstrip("/"))
                for obj in page.get("Contents") or []:
                    files.append({
                        "name": obj["Key"],
                        "size_bytes": obj["Size"],
                        "last_modified": obj["LastModified"].isoformat(),
                    })

            return json.dumps({
                "bucket": bucket,
                "folders": sorted(folders),
                "total_files": len(files),
                "files": files,
            }, indent=2)
        except ClientError as e:
            return json.dumps({"error": e.response["Error"]["Message"]})
        except Exception as e:
            logger.exception("list_folders error")
            return json.dumps({"error": str(e)})

    # ------------------------------------------------------------------ #

    def fetch_and_parse_ach(self, bucket: str, folder_path: str) -> str:
        """
        Fetch every ACH file under folder_path in the bucket,
        parse them, and return compact non-PII summaries.

        Returns one record per file with:
          - file name, effective date, company name/ID
          - per-batch totals (credits, debits, entry count)
          - folder-level aggregate
        Individual entry rows and raw headers are omitted to keep the
        response size manageable.
        """
        try:
            prefix = folder_path.rstrip("/") + "/"
            paginator = self.s3.get_paginator("list_objects_v2")
            pages = paginator.paginate(Bucket=bucket, Prefix=prefix)

            objects = sorted(
                [obj for page in pages for obj in (page.get("Contents") or [])],
                key=lambda o: o["Key"],
            )

            if not objects:
                return json.dumps({
                    "error": f"No files found at '{folder_path}' in bucket '{bucket}'"
                })

            results = []
            folder_credits = 0.0
            folder_debits  = 0.0
            folder_entries = 0

            for obj in objects:
                key = obj["Key"]
                ext = "." + key.rsplit(".", 1)[-1].lower() if "." in key else ""
                if ext not in ACH_EXTENSIONS:
                    continue

                body    = self.s3.get_object(Bucket=bucket, Key=key)["Body"]
                content = body.read().decode("utf-8", errors="replace")
                parsed  = self.parser.parse(content)

                # Compact batch summaries – drop individual entry rows
                batch_summaries = []
                for b in parsed.get("batches", []):
                    ctrl = b.get("control") or {}
                    batch_summaries.append({
                        "company_name":        b.get("company_name", "").strip(),
                        "company_id":          b.get("company_identification", "").strip(),
                        "entry_class":         b.get("standard_entry_class", "").strip(),
                        "effective_date":      b.get("effective_entry_date"),
                        "entry_count":         ctrl.get("entry_count", len(b.get("entries", []))),
                        "total_credit_dollars": ctrl.get("total_credit_dollars", 0.0),
                        "total_debit_dollars":  ctrl.get("total_debit_dollars",  0.0),
                    })

                summary = parsed.get("summary", {})
                folder_credits += summary.get("total_credit_dollars", 0.0)
                folder_debits  += summary.get("total_debit_dollars",  0.0)
                folder_entries += summary.get("total_entries", 0)

                results.append({
                    "file":            key.split("/")[-1],
                    "effective_dates": summary.get("effective_dates", []),
                    "company_names":   summary.get("company_names", []),
                    "company_ids": [
                        b.get("company_identification", "").strip()
                        for b in parsed.get("batches", [])
                    ],
                    "total_entries":         summary.get("total_entries", 0),
                    "total_credit_dollars":  round(summary.get("total_credit_dollars", 0.0), 2),
                    "total_debit_dollars":   round(summary.get("total_debit_dollars",  0.0), 2),
                    "batches": batch_summaries,
                })

            return json.dumps({
                "folder":          folder_path,
                "bucket":          bucket,
                "files_processed": len(results),
                "folder_totals": {
                    "total_credit_dollars": round(folder_credits, 2),
                    "total_debit_dollars":  round(folder_debits,  2),
                    "total_entries":        folder_entries,
                },
                "files": results,
            }, indent=2)
        except ClientError as e:
            return json.dumps({"error": e.response["Error"]["Message"]})
        except Exception as e:
            logger.exception("fetch_and_parse_ach error")
            return json.dumps({"error": str(e)})

    # ------------------------------------------------------------------ #

    def get_container_summary(self, bucket: str) -> str:
        """High-level summary of all objects in a bucket, grouped by folder."""
        try:
            paginator = self.s3.get_paginator("list_objects_v2")
            pages = paginator.paginate(Bucket=bucket)

            folders: dict[str, int] = {}
            total, total_size = 0, 0
            for page in pages:
                for obj in page.get("Contents") or []:
                    folder = obj["Key"].split("/")[0] if "/" in obj["Key"] else "(root)"
                    folders[folder] = folders.get(folder, 0) + 1
                    total += 1
                    total_size += obj["Size"]

            return json.dumps({
                "bucket": bucket,
                "total_files": total,
                "total_size_bytes": total_size,
                "folders": folders,
            }, indent=2)
        except ClientError as e:
            return json.dumps({"error": e.response["Error"]["Message"]})
        except Exception as e:
            logger.exception("get_container_summary error")
            return json.dumps({"error": str(e)})
