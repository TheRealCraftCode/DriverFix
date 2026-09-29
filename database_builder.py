from __future__ import annotations

import csv
import json
import os
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ============================================================
# DriverFix - Automatic Driver Signature Database Builder
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
DATABASE_FILE = BASE_DIR / "signature.json"

SCHEMA_VERSION = 1


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_command(
    command: list[str],
    timeout: int = 60
) -> tuple[int, str, str]:
    """
    Run a Windows command safely and return:
        return_code, stdout, stderr
    """

    try:
        process = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )

        return (
            process.returncode,
            process.stdout,
            process.stderr
        )

    except subprocess.TimeoutExpired:
        return -1, "", "Command timed out."

    except Exception as exc:
        return -1, "", str(exc)


def load_database() -> dict[str, Any]:
    """
    Load the existing signature database.

    If the file doesn't exist or is invalid, create
    a fresh database structure.
    """

    if not DATABASE_FILE.exists():
        return {
            "schema_version": SCHEMA_VERSION,
            "database_type": "driver_signatures",
            "generated_at": None,
            "generator": "DriverFix Database Builder",
            "signatures": []
        }

    try:
        with DATABASE_FILE.open(
            "r",
            encoding="utf-8"
        ) as file:
            data = json.load(file)

        if not isinstance(data, dict):
            raise ValueError("Database root must be an object.")

        signatures = data.get("signatures")

        if not isinstance(signatures, list):
            data["signatures"] = []

        data["schema_version"] = SCHEMA_VERSION
        data["database_type"] = "driver_signatures"
        data["generator"] = "DriverFix Database Builder"

        return data

    except Exception as exc:
        print(f"[!] Existing database could not be loaded: {exc}")
        print("[*] Creating a new database.")

        return {
            "schema_version": SCHEMA_VERSION,
            "database_type": "driver_signatures",
            "generated_at": None,
            "generator": "DriverFix Database Builder",
            "signatures": []
        }


def save_database(database: dict[str, Any]) -> None:
    """
    Save the database atomically.

    A temporary file is written first so that an interrupted
    write doesn't normally destroy the existing database.
    """

    temp_file = DATABASE_FILE.with_suffix(".json.tmp")

    with temp_file.open(
        "w",
        encoding="utf-8"
    ) as file:
        json.dump(
            database,
            file,
            indent=4,
            ensure_ascii=False
        )

        file.write("\n")

    os.replace(temp_file, DATABASE_FILE)


def normalize_hardware_id(value: str) -> str:
    if not value:
        return ""

    return value.strip().upper()


def normalize_text(value: Any) -> str:
    if value is None:
        return ""

    return str(value).strip()


def get_pnp_signed_drivers() -> list[dict[str, Any]]:
    """
    Get installed PnP signed-driver information through CIM.

    This provides device-level information such as:

        DeviceID
        HardwareID
        CompatibleID
        DriverVersion
        DriverProviderName
        InfName
        DeviceName
        Manufacturer
        Signer
    """

    powershell_script = r"""
$ErrorActionPreference = 'SilentlyContinue'

Get-CimInstance Win32_PnPSignedDriver |
    Select-Object `
        DeviceID,
        HardwareID,
        CompatibleID,
        DeviceName,
        Manufacturer,
        DriverVersion,
        DriverDate,
        DriverProviderName,
        InfName,
        IsSigned,
        Signer |
    ConvertTo-Json -Depth 6 -Compress
"""

    code, stdout, stderr = run_command(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            powershell_script
        ],
        timeout=90
    )

    if code != 0:
        print("[!] PowerShell driver query failed.")
        if stderr:
            print(stderr.strip())

        return []

    if not stdout.strip():
        return []

    try:
        data = json.loads(stdout)

    except json.JSONDecodeError as exc:
        print(f"[!] Could not parse PowerShell driver data: {exc}")
        return []

    if isinstance(data, dict):
        return [data]

    if isinstance(data, list):
        return data

    return []


def get_pnputil_inventory() -> list[dict[str, str]]:
    """
    Obtain the Windows third-party driver-store inventory.

    Modern Windows versions support structured PnPUtil output.
    XML is used here because it is easier to preserve fields
    without relying on localized console text.
    """

    with tempfile.TemporaryDirectory(
        prefix="DriverFix_"
    ) as temp_dir:

        output_file = Path(temp_dir) / "drivers.xml"

        command = [
            "pnputil.exe",
            "/enum-drivers",
            "/files",
            "/format",
            "xml",
            "/output-file",
            str(output_file)
        ]

        code, stdout, stderr = run_command(
            command,
            timeout=120
        )

        if code != 0:
            print("[!] PnPUtil inventory failed.")

            if stderr:
                print(stderr.strip())

            return []

        if not output_file.exists():
            print("[!] PnPUtil did not create an XML inventory.")
            return []

        try:
            tree = ET.parse(output_file)
            root = tree.getroot()

        except Exception as exc:
            print(f"[!] Could not parse PnPUtil XML: {exc}")
            return []

        results: list[dict[str, str]] = []

        # PnPUtil's XML structure can vary between Windows releases,
        # so this intentionally searches by element name rather than
        # depending on one exact nesting layout.

        for driver_node in root.iter():

            if driver_node.tag.lower().split("}")[-1].lower() != "driver":
                continue

            entry: dict[str, str] = {}

            for child in driver_node.iter():
                tag = child.tag.lower().split("}")[-1].lower()

                if child is driver_node:
                    continue

                value = (child.text or "").strip()

                if not value:
                    continue

                if tag in {
                    "publishedname",
                    "originalname",
                    "providername",
                    "classname",
                    "classguid",
                    "version",
                    "date",
                    "signername"
                }:
                    entry[tag] = value

            if entry:
                results.append(entry)

        return results


def extract_hardware_ids(driver: dict[str, Any]) -> list[str]:
    """
    Return normalized hardware IDs from a CIM driver object.
    """

    hardware_ids = driver.get("HardwareID")

    if not hardware_ids:
        return []

    if isinstance(hardware_ids, str):
        hardware_ids = [hardware_ids]

    if not isinstance(hardware_ids, list):
        return []

    result = []

    for hardware_id in hardware_ids:
        normalized = normalize_hardware_id(hardware_id)

        if normalized and normalized not in result:
            result.append(normalized)

    return result


def extract_compatible_ids(driver: dict[str, Any]) -> list[str]:
    """
    Return normalized compatible IDs.
    """

    compatible_ids = driver.get("CompatibleID")

    if not compatible_ids:
        return []

    if isinstance(compatible_ids, str):
        compatible_ids = [compatible_ids]

    if not isinstance(compatible_ids, list):
        return []

    result = []

    for compatible_id in compatible_ids:
        normalized = normalize_hardware_id(compatible_id)

        if normalized and normalized not in result:
            result.append(normalized)

    return result


def make_signature(
    driver: dict[str, Any],
    hardware_id: str
) -> dict[str, Any]:
    """
    Convert Windows driver information into DriverFix's
    normalized signature format.
    """

    return {
        "hardware_id": hardware_id,

        "compatible_ids": extract_compatible_ids(driver),

        "device_name": normalize_text(
            driver.get("DeviceName")
        ),

        "manufacturer": normalize_text(
            driver.get("Manufacturer")
        ),

        "driver_provider": normalize_text(
            driver.get("DriverProviderName")
        ),

        "installed_version": normalize_text(
            driver.get("DriverVersion")
        ),

        "installed_date": normalize_text(
            driver.get("DriverDate")
        ),

        "inf_name": normalize_text(
            driver.get("InfName")
        ),

        "device_id": normalize_text(
            driver.get("DeviceID")
        ),

        "signed": bool(
            driver.get("IsSigned")
        ),

        "signer": normalize_text(
            driver.get("Signer")
        ),

        # These are deliberately empty until an authoritative
        # source supplies current-version information.
        "latest_version": None,
        "release_date": None,
        "source": None,
        "download_url": None,

        # Possible values:
        #
        # inventory_only
        # verified
        # update_available
        # unknown
        #
        "status": "inventory_only",

        "last_seen": now_iso()
    }


def merge_signature(
    old: dict[str, Any],
    new: dict[str, Any]
) -> dict[str, Any]:
    """
    Update automatically discovered fields while preserving
    authoritative fields that may have been added later.

    This is important: rebuilding the database must NOT erase
    manually/externally verified latest-version information.
    """

    merged = dict(old)

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

    for field in automatic_fields:
        if field in new:
            merged[field] = new[field]

    # Preserve these if they already exist.
    for field in {
        "latest_version",
        "release_date",
        "source",
        "download_url",
        "status"
    }:
        if field not in merged:
            merged[field] = new.get(field)

    return merged


def build_database() -> dict[str, Any]:
    print("=" * 60)
    print("DriverFix Automatic Database Builder")
    print("=" * 60)
    print()

    database = load_database()

    old_signatures = database.get(
        "signatures",
        []
    )

    existing: dict[str, dict[str, Any]] = {}

    for signature in old_signatures:

        if not isinstance(signature, dict):
            continue

        hardware_id = normalize_hardware_id(
            signature.get("hardware_id", "")
        )

        if hardware_id:
            existing[hardware_id] = signature

    print("[1/3] Reading Windows driver information...")

    cim_drivers = get_pnp_signed_drivers()

    print(
        f"      Found {len(cim_drivers)} "
        f"driver/device records."
    )

    print()
    print("[2/3] Reading third-party driver store...")

    pnputil_drivers = get_pnputil_inventory()

    print(
        f"      Found {len(pnputil_drivers)} "
        f"driver-store records."
    )

    print()
    print("[3/3] Building signature database...")

    discovered = 0
    added = 0
    updated = 0

    for driver in cim_drivers:

        hardware_ids = extract_hardware_ids(driver)

        if not hardware_ids:
            continue

        for hardware_id in hardware_ids:

            discovered += 1

            new_signature = make_signature(
                driver,
                hardware_id
            )

            if hardware_id in existing:

                existing[hardware_id] = merge_signature(
                    existing[hardware_id],
                    new_signature
                )

                updated += 1

            else:

                existing[hardware_id] = new_signature

                added += 1

    # Sort alphabetically for stable JSON output.
    signatures = sorted(
        existing.values(),
        key=lambda item: (
            str(item.get("manufacturer", "")).lower(),
            str(item.get("device_name", "")).lower(),
            str(item.get("hardware_id", "")).lower()
        )
    )

    database["schema_version"] = SCHEMA_VERSION
    database["database_type"] = "driver_signatures"
    database["generated_at"] = now_iso()
    database["generator"] = "DriverFix Database Builder"
    database["statistics"] = {
        "hardware_id_records": len(signatures),
        "discovered_this_run": discovered,
        "new_records": added,
        "updated_records": updated,
        "pnputil_driver_store_records": len(pnputil_drivers)
    }
    database["signatures"] = signatures

    save_database(database)

    print()
    print("=" * 60)
    print("Database build complete!")
    print("=" * 60)
    print()
    print(f"Database:       {DATABASE_FILE}")
    print(f"Signatures:     {len(signatures)}")
    print(f"Discovered:     {discovered}")
    print(f"New:            {added}")
    print(f"Updated:        {updated}")
    print(f"Driver store:   {len(pnputil_drivers)}")
    print()

    return database


def main() -> int:

    try:
        build_database()
        return 0

    except KeyboardInterrupt:
        print()
        print("[!] Cancelled.")
        return 1

    except Exception as exc:
        print()
        print(f"[!] Database builder failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())