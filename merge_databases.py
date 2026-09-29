from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ============================================================
# DriverFix - Signature Database Merger
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent

MASTER_DATABASE = BASE_DIR / "signature.json"
COLLECTED_DIR = PROJECT_DIR / "collected"

BACKUP_DIR = PROJECT_DIR / "data" / "database_backups"

SCHEMA_VERSION = 1


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_directories() -> None:
    COLLECTED_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    BACKUP_DIR.mkdir(
        parents=True,
        exist_ok=True
    )


def load_json(path: Path) -> dict[str, Any] | None:
    try:
        with path.open(
            "r",
            encoding="utf-8"
        ) as file:
            data = json.load(file)

        if not isinstance(data, dict):
            print(
                f"[!] Ignoring {path.name}: "
                "JSON root isn't an object."
            )
            return None

        return data

    except json.JSONDecodeError as exc:
        print(
            f"[!] Ignoring {path.name}: "
            f"invalid JSON ({exc})."
        )

    except OSError as exc:
        print(
            f"[!] Could not read {path.name}: {exc}"
        )

    return None


def save_json(
    path: Path,
    data: dict[str, Any]
) -> None:

    temporary = path.with_suffix(
        path.suffix + ".tmp"
    )

    with temporary.open(
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            data,
            file,
            indent=4,
            ensure_ascii=False
        )

        file.write("\n")

    temporary.replace(path)


def normalize_hardware_id(
    value: Any
) -> str:

    if value is None:
        return ""

    return str(value).strip().upper()


def signature_key(
    signature: dict[str, Any]
) -> str:

    return normalize_hardware_id(
        signature.get(
            "hardware_id",
            ""
        )
    )


def make_backup() -> Path | None:

    if not MASTER_DATABASE.exists():
        return None

    timestamp = datetime.now().strftime(
        "%Y%m%d_%H%M%S"
    )

    backup_path = (
        BACKUP_DIR
        / f"signature_{timestamp}.json"
    )

    shutil.copy2(
        MASTER_DATABASE,
        backup_path
    )

    return backup_path


def merge_signature(
    existing: dict[str, Any],
    incoming: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    """
    Merge a single driver signature.

    Automatically discovered information is updated.

    Authoritative information such as latest_version,
    source and download_url is preserved unless the
    incoming database actually contains a value.
    """

    result = dict(existing)

    changed = False

    automatic_fields = {
        "hardware_id",
        "compatible_ids",
        "device_name",
        "manufacturer",
        "driver_provider",
        "installed_version",
        "installed_date",
        "inf_name",
        "device_id",
        "signed",
        "signer",
        "last_seen"
    }

    authoritative_fields = {
        "latest_version",
        "release_date",
        "source",
        "download_url"
    }

    for field in automatic_fields:

        if field not in incoming:
            continue

        new_value = incoming[field]

        if new_value != result.get(field):
            result[field] = new_value
            changed = True

    for field in authoritative_fields:

        new_value = incoming.get(field)

        if new_value in (
            None,
            "",
            "unknown"
        ):
            continue

        if new_value != result.get(field):
            result[field] = new_value
            changed = True

    # Don't overwrite an authoritative status with
    # inventory_only if the master already knows more.
    incoming_status = incoming.get("status")
    existing_status = result.get("status")

    if incoming_status:
        if existing_status in (
            None,
            "",
            "inventory_only"
        ):
            if incoming_status != existing_status:
                result["status"] = incoming_status
                changed = True

    return result, changed


def load_master_database() -> dict[str, Any]:

    if not MASTER_DATABASE.exists():

        return {
            "schema_version": SCHEMA_VERSION,
            "database_type": "driver_signatures",
            "generated_at": None,
            "generator": "DriverFix Database Merger",
            "statistics": {},
            "signatures": []
        }

    database = load_json(
        MASTER_DATABASE
    )

    if database is None:
        raise RuntimeError(
            "The master signature.json is invalid."
        )

    signatures = database.get(
        "signatures",
        []
    )

    if not isinstance(signatures, list):
        signatures = []

    database["signatures"] = signatures

    return database


def index_signatures(
    signatures: list[Any]
) -> dict[str, dict[str, Any]]:

    indexed: dict[str, dict[str, Any]] = {}

    for signature in signatures:

        if not isinstance(
            signature,
            dict
        ):
            continue

        key = signature_key(
            signature
        )

        if not key:
            continue

        indexed[key] = signature

    return indexed


def discover_databases() -> list[Path]:

    if not COLLECTED_DIR.exists():
        return []

    databases = []

    for path in sorted(
        COLLECTED_DIR.glob("*.json")
    ):

        # Don't accidentally treat the master database
        # as an incoming database.
        if path.resolve() == MASTER_DATABASE.resolve():
            continue

        databases.append(path)

    return databases


def merge_database_file(
    master: dict[str, dict[str, Any]],
    database_path: Path
) -> tuple[int, int, int]:

    database = load_json(
        database_path
    )

    if database is None:
        return 0, 0, 0

    incoming = database.get(
        "signatures",
        []
    )

    if not isinstance(
        incoming,
        list
    ):
        print(
            f"[!] {database_path.name} "
            "doesn't contain a valid signatures list."
        )

        return 0, 0, 0

    added = 0
    updated = 0
    skipped = 0

    for signature in incoming:

        if not isinstance(
            signature,
            dict
        ):
            skipped += 1
            continue

        key = signature_key(
            signature
        )

        if not key:
            skipped += 1
            continue

        if key not in master:

            master[key] = dict(
                signature
            )

            added += 1
            continue

        merged, changed = merge_signature(
            master[key],
            signature
        )

        master[key] = merged

        if changed:
            updated += 1

    return added, updated, skipped


def sort_signatures(
    signatures: list[dict[str, Any]]
) -> list[dict[str, Any]]:

    return sorted(
        signatures,
        key=lambda item: (
            str(
                item.get(
                    "manufacturer",
                    ""
                )
            ).lower(),

            str(
                item.get(
                    "device_name",
                    ""
                )
            ).lower(),

            str(
                item.get(
                    "hardware_id",
                    ""
                )
            ).lower()
        )
    )


def merge_all() -> int:

    print("=" * 65)
    print("DriverFix - Signature Database Merger")
    print("=" * 65)
    print()

    create_directories()

    print("[1/4] Loading master database...")

    database = load_master_database()

    master_signatures = database.get(
        "signatures",
        []
    )

    master = index_signatures(
        master_signatures
    )

    print(
        f"      Existing signatures: "
        f"{len(master)}"
    )

    print()

    print("[2/4] Searching collected databases...")

    files = discover_databases()

    print(
        f"      Found {len(files)} "
        f"database file(s)."
    )

    if not files:

        print()
        print(
            "[!] No databases found."
        )

        print()
        print(
            f"Put PC database files into:"
        )

        print(
            f"    {COLLECTED_DIR}"
        )

        return 1

    print()

    print("[3/4] Merging databases...")

    total_added = 0
    total_updated = 0
    total_skipped = 0

    processed = 0

    for path in files:

        print(
            f"      → {path.name}"
        )

        added, updated, skipped = (
            merge_database_file(
                master,
                path
            )
        )

        print(
            f"         +{added} new"
            f"  ~{updated} updated"
            f"  !{skipped} skipped"
        )

        total_added += added
        total_updated += updated
        total_skipped += skipped

        processed += 1

    print()

    print("[4/4] Saving master database...")

    backup = make_backup()

    if backup:
        print(
            f"      Backup created:"
            f"\n      {backup}"
        )

    signatures = sort_signatures(
        list(master.values())
    )

    database["schema_version"] = (
        SCHEMA_VERSION
    )

    database["database_type"] = (
        "driver_signatures"
    )

    database["generated_at"] = (
        now_iso()
    )

    database["generator"] = (
        "DriverFix Database Merger"
    )

    database["statistics"] = {
        "total_signatures": len(
            signatures
        ),

        "source_databases": processed,

        "new_signatures": total_added,

        "updated_signatures": total_updated,

        "skipped_records": total_skipped
    }

    database["signatures"] = signatures

    save_json(
        MASTER_DATABASE,
        database
    )

    print()
    print("=" * 65)
    print("MERGE COMPLETE")
    print("=" * 65)
    print()
    print(
        f"Total signatures: "
        f"{len(signatures)}"
    )

    print(
        f"New signatures:   "
        f"{total_added}"
    )

    print(
        f"Updated:           "
        f"{total_updated}"
    )

    print(
        f"Skipped:           "
        f"{total_skipped}"
    )

    print()
    print(
        f"Master database:"
    )

    print(
        f"    {MASTER_DATABASE}"
    )

    print()

    return 0


def main() -> int:

    try:
        return merge_all()

    except KeyboardInterrupt:

        print()
        print("[!] Merge cancelled.")

        return 1

    except Exception as exc:

        print()
        print(
            f"[!] Merge failed: {exc}"
        )

        return 1


if __name__ == "__main__":
    raise SystemExit(
        main()
    )