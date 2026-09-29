# DriverFix 1.0
# Windows driver inventory, backup, repair and verified-package updater.
#
# Requirements:
#   Python 3.10+
#   Windows 10/11
#
# No third-party Python packages are required.

from __future__ import annotations

import csv
import ctypes
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import zipfile
import webbrowser
import tkinter as tk

from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk


# ============================================================
# APP CONFIGURATION
# ============================================================

APP_NAME = "DriverFix"
APP_VERSION = "1.0"

BASE_DIR = Path(__file__).resolve().parent
RESOURCES_DIR = BASE_DIR / "resources"
DATA_DIR = BASE_DIR / "data"
BACKUPS_DIR = BASE_DIR / "backups"
LOGS_DIR = BASE_DIR / "logs"
TEMP_DIR = DATA_DIR / "packages"

SIGNATURE_DATABASE = RESOURCES_DIR / "signature.json"
INTERNET_DATABASE = RESOURCES_DIR / "internet_catalog.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
LOG_FILE = LOGS_DIR / "driverfix.log"

for directory in (
    RESOURCES_DIR,
    DATA_DIR,
    BACKUPS_DIR,
    LOGS_DIR,
    TEMP_DIR,
):
    directory.mkdir(parents=True, exist_ok=True)


# ============================================================
# COLORS / UI
# ============================================================

BG = "#111318"
PANEL = "#191C22"
PANEL_2 = "#20242C"
PANEL_3 = "#272C35"
BORDER = "#303641"

TEXT = "#F4F7FA"
MUTED = "#9AA5B1"
ACCENT = "#0099CC"
ACCENT_HOVER = "#00AEEA"

GREEN = "#39C878"
YELLOW = "#F0C75E"
RED = "#F05B67"
BLUE = "#4DA3FF"

FONT = "Comic Sans MS"


# ============================================================
# OFFICIAL DOWNLOAD DOMAINS
# ============================================================

OFFICIAL_DOMAINS = {
    "microsoft.com",
    "windowsupdate.com",
    "update.microsoft.com",
    "download.windowsupdate.com",
    "amd.com",
    "nvidia.com",
    "intel.com",
    "realtek.com",
    "asus.com",
    "msi.com",
    "gigabyte.com",
    "dell.com",
    "hp.com",
    "lenovo.com",
    "acer.com",
    "qualcomm.com",
    "broadcom.com",
    "mediatek.com",
    "synaptics.com",
}

INSTALLABLE_EXTENSIONS = {
    ".inf",
    ".cab",
    ".zip",
}

# EXE files are intentionally excluded from automatic installation.
# DriverFix can open an official vendor page, but it will never
# silently execute a downloaded EXE.


# ============================================================
# DEFAULT SETTINGS
# ============================================================

DEFAULT_SETTINGS = {
    "comic_sans": True,
    "hide_duplicates": True,
    "show_signed": True,
    "show_problem_devices": True,
    "auto_save_reports": False,
    "confirm_changes": True,
    "backup_before_changes": True,
}


# ============================================================
# BASIC HELPERS
# ============================================================

def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def log(message: str) -> None:
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {message}\n")
    except Exception:
        pass


def load_settings() -> dict:
    settings = DEFAULT_SETTINGS.copy()

    try:
        if SETTINGS_FILE.exists():
            with SETTINGS_FILE.open("r", encoding="utf-8") as f:
                loaded = json.load(f)

            if isinstance(loaded, dict):
                settings.update(loaded)
    except Exception as exc:
        log(f"Settings load failed: {exc}")

    return settings


def save_settings(settings: dict) -> None:
    try:
        with SETTINGS_FILE.open("w", encoding="utf-8") as f:
            json.dump(settings, f, indent=4)
    except Exception as exc:
        log(f"Settings save failed: {exc}")


def run_command(
    args: list[str],
    timeout: int = 120,
    cwd: str | None = None,
) -> tuple[int, str, str]:
    try:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        process = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=cwd,
            creationflags=creationflags,
        )

        return (
            process.returncode,
            process.stdout or "",
            process.stderr or "",
        )

    except subprocess.TimeoutExpired:
        return -2, "", "Operation timed out."

    except Exception as exc:
        return -1, "", str(exc)


def run_powershell(
    command: str,
    timeout: int = 120,
) -> tuple[int, str, str]:
    return run_command(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ],
        timeout=timeout,
    )


def normalize(value) -> str:
    if value is None:
        return ""

    return str(value).strip()


def version_parts(version: str) -> list[int]:
    result = []

    for part in normalize(version).replace("-", ".").split("."):
        digits = ""

        for char in part:
            if char.isdigit():
                digits += char
            elif digits:
                break

        if digits:
            result.append(int(digits))

    return result


def compare_versions(installed: str, latest: str) -> int:
    a = version_parts(installed)
    b = version_parts(latest)

    if not a or not b:
        return 0

    length = max(len(a), len(b))

    a += [0] * (length - len(a))
    b += [0] * (length - len(b))

    if a < b:
        return -1

    if a > b:
        return 1

    return 0


def version_status(driver) -> str:
    if not driver.latest_version:
        return "Unknown"

    comparison = compare_versions(
        driver.driver_version,
        driver.latest_version,
    )

    if comparison < 0:
        return "Outdated"

    if comparison == 0:
        return "Current"

    return "Current"


def safe_filename(name: str) -> str:
    name = normalize(name)

    if not name:
        return "driver_package"

    invalid = '<>:"/\\|?*'

    for char in invalid:
        name = name.replace(char, "_")

    return name[:180]


def official_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)

        if parsed.scheme.lower() != "https":
            return False

        hostname = (parsed.hostname or "").lower().rstrip(".")

        if not hostname:
            return False

        for domain in OFFICIAL_DOMAINS:
            if hostname == domain or hostname.endswith("." + domain):
                return True

        return False

    except Exception:
        return False


def looks_like_installable_package(url: str) -> bool:
    try:
        path = urllib.parse.urlparse(url).path.lower()

        return any(
            path.endswith(extension)
            for extension in INSTALLABLE_EXTENSIONS
        )

    except Exception:
        return False


def launch_as_admin() -> None:
    try:
        ctypes.windll.shell32.ShellExecuteW(
            None,
            "runas",
            sys.executable,
            " ".join(f'"{x}"' for x in sys.argv),
            None,
            1,
        )
    except Exception as exc:
        messagebox.showerror(
            APP_NAME,
            f"Could not request administrator privileges.\n\n{exc}",
        )


# ============================================================
# DRIVER MODEL
# ============================================================

@dataclass
class Driver:
    device_name: str = ""
    manufacturer: str = ""
    driver_provider: str = ""
    driver_version: str = ""
    driver_date: str = ""
    inf_name: str = ""
    device_id: str = ""
    hardware_id: str = ""
    compatible_ids: str = ""
    signed: str = ""
    signer: str = ""
    class_name: str = ""
    problem_code: str = ""
    status: str = ""
    latest_version: str = ""
    source: str = ""
    download_url: str = ""
    installed_date: str = ""

    def search_text(self) -> str:
        return " ".join(
            normalize(value)
            for value in asdict(self).values()
        ).lower()

    @property
    def has_problem(self) -> bool:
        code = normalize(self.problem_code)

        return bool(
            code
            and code not in {"0", "OK", "None", "No Problem"}
        )

    @property
    def is_outdated(self) -> bool:
        return version_status(self) == "Outdated"

    @property
    def can_auto_update(self) -> bool:
        if not self.is_outdated:
            return False

        if not self.download_url:
            return False

        if not official_url(self.download_url):
            return False

        return looks_like_installable_package(
            self.download_url
        )


# ============================================================
# DRIVER SCANNER
# ============================================================

class DriverScanner:

    @staticmethod
    def scan(progress=None) -> list[Driver]:
        if progress:
            progress("Collecting signed driver inventory...")

        ps_driver_command = r"""
$items = Get-CimInstance Win32_PnPSignedDriver |
    Select-Object DeviceName,
                  Manufacturer,
                  DriverProviderName,
                  DriverVersion,
                  DriverDate,
                  InfName,
                  DeviceID,
                  HardwareID,
                  CompatibleID,
                  IsSigned,
                  Signer,
                  ClassGuid

$items | ConvertTo-Json -Depth 5 -Compress
"""

        ps_entity_command = r"""
$items = Get-CimInstance Win32_PnPEntity |
    Select-Object Name,
                  DeviceID,
                  ConfigManagerErrorCode,
                  Status,
                  PNPClass,
                  HardwareID,
                  CompatibleID

$items | ConvertTo-Json -Depth 5 -Compress
"""

        driver_result = [None]
        entity_result = [None]

        def get_drivers():
            driver_result[0] = run_powershell(
                ps_driver_command,
                timeout=120,
            )

        def get_entities():
            entity_result[0] = run_powershell(
                ps_entity_command,
                timeout=120,
            )

        t1 = threading.Thread(
            target=get_drivers,
            daemon=True,
        )

        t2 = threading.Thread(
            target=get_entities,
            daemon=True,
        )

        t1.start()
        t2.start()

        t1.join(130)
        t2.join(130)

        if progress:
            progress("Processing Windows device information...")

        if not driver_result[0]:
            raise RuntimeError(
                "Windows driver query did not return a result."
            )

        code, stdout, stderr = driver_result[0]

        if code != 0:
            raise RuntimeError(
                stderr.strip()
                or "Failed to query Win32_PnPSignedDriver."
            )

        entity_map = {}

        if entity_result[0]:
            entity_code, entity_stdout, _ = entity_result[0]

            if entity_code == 0 and entity_stdout.strip():
                try:
                    entities = json.loads(entity_stdout)

                    if isinstance(entities, dict):
                        entities = [entities]

                    if isinstance(entities, list):
                        for entity in entities:
                            device_id = normalize(
                                entity.get("DeviceID")
                            ).upper()

                            if device_id:
                                entity_map[device_id] = entity

                except Exception as exc:
                    log(f"Entity JSON parse failed: {exc}")

        try:
            records = json.loads(stdout)

            if isinstance(records, dict):
                records = [records]

        except Exception as exc:
            raise RuntimeError(
                f"Could not parse Windows driver data: {exc}"
            )

        drivers = []

        for record in records:
            device_id = normalize(
                record.get("DeviceID")
            )

            entity = entity_map.get(
                device_id.upper(),
                {},
            )

            hardware_ids = record.get("HardwareID")
            compatible_ids = record.get("CompatibleID")

            if isinstance(hardware_ids, list):
                hardware_ids = "; ".join(
                    normalize(x)
                    for x in hardware_ids
                )

            if isinstance(compatible_ids, list):
                compatible_ids = "; ".join(
                    normalize(x)
                    for x in compatible_ids
                )

            problem_code = normalize(
                entity.get("ConfigManagerErrorCode")
            )

            status = normalize(
                entity.get("Status")
            )

            class_name = normalize(
                entity.get("PNPClass")
            )

            if not class_name:
                class_name = normalize(
                    record.get("ClassGuid")
                )

            driver = Driver(
                device_name=normalize(
                    record.get("DeviceName")
                )
                or normalize(entity.get("Name")),

                manufacturer=normalize(
                    record.get("Manufacturer")
                ),

                driver_provider=normalize(
                    record.get("DriverProviderName")
                ),

                driver_version=normalize(
                    record.get("DriverVersion")
                ),

                driver_date=normalize(
                    record.get("DriverDate")
                ),

                inf_name=normalize(
                    record.get("InfName")
                ),

                device_id=device_id,

                hardware_id=normalize(
                    hardware_ids
                ),

                compatible_ids=normalize(
                    compatible_ids
                ),

                signed=str(
                    record.get("IsSigned", "")
                ),

                signer=normalize(
                    record.get("Signer")
                ),

                class_name=class_name,

                problem_code=problem_code,

                status=status,
            )

            drivers.append(driver)

        log(
            f"Scanner discovered {len(drivers)} driver records."
        )

        return drivers


# ============================================================
# DATABASE
# ============================================================

class DriverDatabase:

    def __init__(self):
        self.signatures = []
        self.internet = []

        self.load()

    def load(self):
        self.signatures = self._load_json(
            SIGNATURE_DATABASE
        )

        self.internet = self._load_json(
            INTERNET_DATABASE
        )

    @staticmethod
    def _load_json(path: Path):
        if not path.exists():
            return []

        try:
            with path.open(
                "r",
                encoding="utf-8",
            ) as f:
                data = json.load(f)

            if isinstance(data, dict):
                data = data.get(
                    "signatures",
                    data.get("drivers", [])
                )

            if not isinstance(data, list):
                return []

            return data

        except Exception as exc:
            log(f"Database load failed: {path}: {exc}")
            return []

    def enrich(self, drivers: list[Driver]) -> None:
        if not self.signatures and not self.internet:
            return

        records = (
            self.signatures +
            self.internet
        )

        hardware_map = {}

        for record in records:
            hardware_ids = []

            for key in (
                "hardware_id",
                "hardware_ids",
                "HardwareID",
            ):
                value = record.get(key)

                if isinstance(value, list):
                    hardware_ids.extend(value)

                elif value:
                    hardware_ids.append(value)

            compatible = record.get(
                "compatible_ids",
                record.get("CompatibleID", [])
            )

            if isinstance(compatible, list):
                hardware_ids.extend(compatible)

            elif compatible:
                hardware_ids.append(compatible)

            for hardware_id in hardware_ids:
                normalized = normalize(
                    hardware_id
                ).upper()

                if normalized:
                    hardware_map[normalized] = record

        for driver in drivers:
            candidates = []

            for value in (
                driver.hardware_id.split(";"),
                driver.compatible_ids.split(";"),
            ):
                for item in value:
                    item = normalize(item).upper()

                    if item:
                        candidates.append(item)

            record = None

            for candidate in candidates:
                if candidate in hardware_map:
                    record = hardware_map[candidate]
                    break

            if not record:
                continue

            driver.latest_version = normalize(
                record.get("latest_version")
                or record.get("LatestVersion")
            )

            driver.source = normalize(
                record.get("source")
                or record.get("Source")
            )

            driver.download_url = normalize(
                record.get("download_url")
                or record.get("DownloadURL")
                or record.get("url")
            )

            if not driver.manufacturer:
                driver.manufacturer = normalize(
                    record.get("manufacturer")
                )

    def catalog_matches(
        self,
        driver: Driver,
    ) -> list[dict]:

        results = []

        text = driver.search_text()

        for record in (
            self.signatures +
            self.internet
        ):
            record_text = json.dumps(
                record,
                ensure_ascii=False,
            ).lower()

            if (
                normalize(driver.hardware_id).lower()
                and normalize(driver.hardware_id).lower()
                in record_text
            ):
                results.append(record)
                continue

            if (
                normalize(driver.device_name).lower()
                and normalize(driver.device_name).lower()
                in record_text
            ):
                results.append(record)

        return results


# ============================================================
# PACKAGE MANAGER
# ============================================================

class DriverPackageManager:

    @staticmethod
    def download_package(
        url: str,
        progress=None,
    ) -> Path:

        if not official_url(url):
            raise RuntimeError(
                "The download source is not an approved official domain."
            )

        if not looks_like_installable_package(url):
            raise RuntimeError(
                "The source is not a direct INF, CAB or ZIP package."
            )

        parsed = urllib.parse.urlparse(url)

        filename = Path(
            urllib.parse.unquote(parsed.path)
        ).name

        if not filename:
            filename = "driver_package"

        filename = safe_filename(filename)

        destination = (
            TEMP_DIR /
            f"{int(time.time())}_{filename}"
        )

        log(f"Downloading verified package: {url}")

        request = urllib.request.Request(
            url,
            headers={
                "User-Agent":
                    "DriverFix/1.2 Windows Driver Utility"
            },
        )

        with urllib.request.urlopen(
            request,
            timeout=60,
        ) as response:

            total = response.headers.get(
                "Content-Length"
            )

            total_bytes = (
                int(total)
                if total and total.isdigit()
                else 0
            )

            downloaded = 0

            with destination.open("wb") as output:

                while True:
                    chunk = response.read(
                        1024 * 1024
                    )

                    if not chunk:
                        break

                    output.write(chunk)
                    downloaded += len(chunk)

                    if progress:
                        if total_bytes:
                            percent = (
                                downloaded /
                                total_bytes
                            ) * 100
                        else:
                            percent = 0

                        progress(
                            f"Downloading driver package... "
                            f"{percent:.0f}%"
                        )

        if not destination.exists():
            raise RuntimeError(
                "The driver package was not downloaded."
            )

        if destination.stat().st_size == 0:
            destination.unlink(
                missing_ok=True
            )

            raise RuntimeError(
                "The downloaded package was empty."
            )

        return destination

    @staticmethod
    def extract_package(
        package: Path,
        progress=None,
    ) -> tuple[Path, Path | None]:

        extension = package.suffix.lower()

        if extension == ".inf":
            return package, None

        extraction_dir = Path(
            tempfile.mkdtemp(
                prefix="DriverFix_",
                dir=str(TEMP_DIR),
            )
        )

        if progress:
            progress(
                f"Preparing {package.name}..."
            )

        if extension == ".zip":

            with zipfile.ZipFile(
                package,
                "r",
            ) as archive:

                archive.extractall(
                    extraction_dir
                )

        elif extension == ".cab":

            code, stdout, stderr = run_command(
                [
                    "expand.exe",
                    "-F:*",
                    str(package),
                    str(extraction_dir),
                ],
                timeout=120,
            )

            if code != 0:
                shutil.rmtree(
                    extraction_dir,
                    ignore_errors=True,
                )

                raise RuntimeError(
                    stderr.strip()
                    or stdout.strip()
                    or "Could not extract CAB package."
                )

        else:
            raise RuntimeError(
                f"Unsupported package type: {extension}"
            )

        inf_files = list(
            extraction_dir.rglob("*.inf")
        )

        if not inf_files:
            shutil.rmtree(
                extraction_dir,
                ignore_errors=True,
            )

            raise RuntimeError(
                "The package does not contain an INF driver file."
            )

        return inf_files[0], extraction_dir

    @staticmethod
    def install_package(
        package: Path,
        progress=None,
    ) -> tuple[bool, str]:

        extension = package.suffix.lower()

        if extension not in INSTALLABLE_EXTENSIONS:
            return False, (
                "DriverFix only automatically installs "
                "INF, CAB and ZIP packages."
            )

        extraction_dir = None

        try:
            inf_path, extraction_dir = (
                DriverPackageManager.extract_package(
                    package,
                    progress,
                )
            )

            if progress:
                progress(
                    f"Installing {inf_path.name}..."
                )

            code, stdout, stderr = run_command(
                [
                    "pnputil.exe",
                    "/add-driver",
                    str(inf_path),
                    "/install",
                ],
                timeout=180,
            )

            output = (
                stdout.strip()
                or stderr.strip()
                or "No output."
            )

            if code == 0:
                return True, output

            return False, output

        finally:
            if extraction_dir:
                shutil.rmtree(
                    extraction_dir,
                    ignore_errors=True,
                )


# ============================================================
# DEVICE OPERATIONS
# ============================================================

class DriverOperations:

    @staticmethod
    def scan_devices() -> tuple[bool, str]:
        code, stdout, stderr = run_command(
            [
                "pnputil.exe",
                "/scan-devices",
            ],
            timeout=120,
        )

        output = (
            stdout.strip()
            or stderr.strip()
            or "Device scan completed."
        )

        return code == 0, output

    @staticmethod
    def restart_device(
        device_id: str,
    ) -> tuple[bool, str]:

        if not device_id:
            return False, (
                "The device does not have a usable "
                "instance ID."
            )

        code, stdout, stderr = run_command(
            [
                "pnputil.exe",
                "/restart-device",
                device_id,
            ],
            timeout=120,
        )

        output = (
            stdout.strip()
            or stderr.strip()
            or "Device restart completed."
        )

        return code == 0, output

    @staticmethod
    def reinstall_from_backup(
        driver: Driver,
        backup_directory: Path,
        progress=None,
    ) -> tuple[bool, str]:

        if not driver.inf_name:
            return False, (
                "This device does not have an INF name."
            )

        candidates = list(
            backup_directory.rglob(
                driver.inf_name
            )
        )

        if not candidates:
            return False, (
                f"{driver.inf_name} was not found in the backup."
            )

        inf = candidates[0]

        if progress:
            progress(
                f"Reinstalling {inf.name}..."
            )

        code, stdout, stderr = run_command(
            [
                "pnputil.exe",
                "/add-driver",
                str(inf),
                "/install",
            ],
            timeout=180,
        )

        output = (
            stdout.strip()
            or stderr.strip()
            or "No output."
        )

        return code == 0, output


# ============================================================
# BACKUP MANAGER
# ============================================================

class BackupManager:

    @staticmethod
    def create_backup(
        progress=None,
    ) -> Path:

        if not is_admin():
            raise PermissionError(
                "Administrator privileges are required "
                "to create a driver backup."
            )

        timestamp = datetime.now().strftime(
            "%Y%m%d_%H%M%S"
        )

        backup_path = (
            BACKUPS_DIR /
            f"DriverBackup_{timestamp}"
        )

        backup_path.mkdir(
            parents=True,
            exist_ok=True,
        )

        if progress:
            progress(
                "Creating driver inventory..."
            )

        inventory_file = (
            backup_path /
            "driver_inventory.xml"
        )

        code, stdout, stderr = run_command(
            [
                "pnputil.exe",
                "/enum-drivers",
                "/files",
                "/format",
                "xml",
                "/output-file",
                str(inventory_file),
            ],
            timeout=180,
        )

        if code != 0:
            log(
                "Driver inventory failed: "
                + stderr
            )

        if progress:
            progress(
                "Exporting driver packages..."
            )

        code, stdout, stderr = run_command(
            [
                "pnputil.exe",
                "/export-driver",
                "*",
                str(backup_path),
            ],
            timeout=600,
        )

        if code != 0:
            raise RuntimeError(
                stderr.strip()
                or stdout.strip()
                or "Driver export failed."
            )

        text_file = (
            backup_path /
            "backup_info.txt"
        )

        with text_file.open(
            "w",
            encoding="utf-8",
        ) as f:
            f.write(
                f"{APP_NAME} driver backup\n"
            )
            f.write(
                f"Created: {datetime.now()}\n"
            )
            f.write(
                f"Computer: {platform.node()}\n"
            )
            f.write(
                f"Windows: {platform.platform()}\n"
            )

        log(
            f"Created driver backup: {backup_path}"
        )

        return backup_path

    @staticmethod
    def restore_backup(
        backup_path: Path,
        progress=None,
    ) -> tuple[int, int]:

        if not is_admin():
            raise PermissionError(
                "Administrator privileges are required "
                "to restore drivers."
            )

        inf_files = list(
            backup_path.rglob("*.inf")
        )

        total = len(inf_files)
        success = 0

        for index, inf in enumerate(
            inf_files,
            start=1,
        ):

            if progress:
                progress(
                    f"Restoring {inf.name} "
                    f"({index}/{total})..."
                )

            code, _, _ = run_command(
                [
                    "pnputil.exe",
                    "/add-driver",
                    str(inf),
                    "/install",
                ],
                timeout=180,
            )

            if code == 0:
                success += 1

        return success, total


# ============================================================
# STORAGE / NTFS MAINTENANCE
# ============================================================

@dataclass
class CleanupItem:
    path: str
    size: int = 0
    is_directory: bool = False


@dataclass
class CleanupResult:
    scanned_files: int = 0
    scanned_directories: int = 0
    removable_files: int = 0
    removable_size: int = 0
    deleted_files: int = 0
    deleted_directories: int = 0
    freed_bytes: int = 0
    skipped_files: int = 0
    errors: int = 0


@dataclass
class DiskCheckResult:
    drive: str
    exit_code: int
    output: str
    errors_found: bool = False
    requires_reboot: bool = False
    repaired: bool = False


def format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 ** 2:
        return f"{size / 1024:.1f} KB"
    if size < 1024 ** 3:
        return f"{size / (1024 ** 2):.1f} MB"
    if size < 1024 ** 4:
        return f"{size / (1024 ** 3):.2f} GB"
    return f"{size / (1024 ** 4):.2f} TB"


class MaintenanceManager:
    """Safe temporary-data cleanup and Windows NTFS maintenance."""

    PROTECTED_NAMES = {
        "Windows", "System32", "WinSxS", "Program Files",
        "Program Files (x86)", "ProgramData", "Users", "Boot", "Recovery",
    }

    def __init__(self, log_callback=None):
        self.log_callback = log_callback
        self.temp_locations = self._build_temp_locations()

    def log(self, message: str):
        if self.log_callback:
            self.log_callback(message)

    @staticmethod
    def _build_temp_locations() -> list[Path]:
        values = [
            os.environ.get("TEMP", ""),
            os.environ.get("TMP", ""),
            os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Temp"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Temp"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Windows", "INetCache"),
        ]
        result = []
        for value in values:
            if not value:
                continue
            try:
                path = Path(value).resolve()
                if path.exists() and path.is_dir() and path not in result:
                    result.append(path)
            except Exception:
                pass
        return result

    def _inside_allowed_location(self, path: Path) -> bool:
        try:
            target = path.resolve()
            for root in self.temp_locations:
                try:
                    target.relative_to(root.resolve())
                    return True
                except ValueError:
                    continue
        except Exception:
            pass
        return False

    def scan_temp(self, callback=None) -> tuple[list[CleanupItem], CleanupResult]:
        result = CleanupResult()
        items: list[CleanupItem] = []
        seen: set[str] = set()

        def report(message):
            self.log(message)
            if callback:
                callback(message)

        report("Scanning temporary data...")

        for root in self.temp_locations:
            report(f"Scanning: {root}")
            try:
                for current_root, dirs, files in os.walk(root, topdown=True, followlinks=False):
                    dirs[:] = [d for d in dirs if d not in self.PROTECTED_NAMES]
                    current = Path(current_root)

                    for filename in files:
                        path = current / filename
                        try:
                            if path.is_symlink():
                                result.skipped_files += 1
                                continue
                            resolved = path.resolve()
                            key = str(resolved).lower()
                            if key in seen:
                                continue
                            seen.add(key)
                            size = path.stat().st_size
                            result.scanned_files += 1
                            result.removable_files += 1
                            result.removable_size += size
                            items.append(CleanupItem(str(path), size, False))
                        except (OSError, PermissionError):
                            result.skipped_files += 1
                            result.errors += 1

                    for dirname in dirs:
                        path = current / dirname
                        try:
                            if path.is_symlink():
                                continue
                            if not any(path.iterdir()):
                                result.scanned_directories += 1
                                items.append(CleanupItem(str(path), 0, True))
                        except (OSError, PermissionError):
                            result.errors += 1
            except (OSError, PermissionError) as exc:
                self.log(f"Could not scan {root}: {exc}")
                result.errors += 1

        report(
            f"Temporary scan complete: {result.removable_files:,} files, "
            f"{format_size(result.removable_size)} available."
        )
        return items, result

    def clean_temp(self, items: list[CleanupItem], callback=None) -> CleanupResult:
        result = CleanupResult()
        files = [x for x in items if not x.is_directory]
        directories = [x for x in items if x.is_directory]

        def report(message):
            self.log(message)
            if callback:
                callback(message)

        for index, item in enumerate(files, 1):
            try:
                path = Path(item.path)
                if not self._inside_allowed_location(path) or not path.exists() or path.is_symlink():
                    result.skipped_files += 1
                    continue
                size = path.stat().st_size
                path.unlink()
                result.deleted_files += 1
                result.freed_bytes += size
                if index % 25 == 0:
                    report(f"Cleaned {index}/{len(files)} temporary files...")
            except (OSError, PermissionError):
                result.skipped_files += 1

        directories.sort(key=lambda x: len(Path(x.path).parts), reverse=True)
        for item in directories:
            try:
                path = Path(item.path)
                if self._inside_allowed_location(path) and path.exists() and path.is_dir():
                    path.rmdir()
                    result.deleted_directories += 1
            except (OSError, PermissionError):
                pass

        report(
            f"Cleanup complete: freed {format_size(result.freed_bytes)} "
            f"from {result.deleted_files:,} files."
        )
        return result

    @staticmethod
    def fixed_drives() -> list[str]:
        drives = []
        if os.name != "nt":
            return drives
        try:
            for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
                root = f"{letter}:\\"
                if os.path.exists(root) and ctypes.windll.kernel32.GetDriveTypeW(root) == 3:
                    drives.append(f"{letter}:")
        except Exception:
            pass
        return drives

    @staticmethod
    def _normalize_drive(drive: str) -> str:
        drive = normalize(drive)
        if not drive:
            drive = os.environ.get("SystemDrive", "C:")
        drive = drive.rstrip("\\/")
        if not drive.endswith(":"):
            drive += ":"
        return drive.upper()

    def scan_disk(self, drive: str, callback=None) -> DiskCheckResult:
        drive = self._normalize_drive(drive)
        if not is_admin():
            return DiskCheckResult(drive, -1, "Administrator privileges are required for CHKDSK.")

        if callback:
            callback(f"Running NTFS online scan on {drive}...")

        code, stdout, stderr = run_command(
            ["chkdsk.exe", drive, "/scan"],
            timeout=900,
        )
        output = (stdout.strip() or stderr.strip() or "CHKDSK returned no output.")
        lowered = output.lower()
        errors_found = any(x in lowered for x in (
            "found problems", "found errors", "errors found", "corruption",
            "corrupt", "problems that require",
        ))
        self.log(f"CHKDSK scan {drive}: exit={code}, errors_found={errors_found}")
        return DiskCheckResult(drive, code, output, errors_found=errors_found)

    def repair_disk(self, drive: str, callback=None) -> DiskCheckResult:
        drive = self._normalize_drive(drive)
        if not is_admin():
            return DiskCheckResult(drive, -1, "Administrator privileges are required for disk repair.")

        if callback:
            callback(f"Running filesystem repair on {drive}...")

        code, stdout, stderr = run_command(
            ["chkdsk.exe", drive, "/f"],
            timeout=1800,
        )
        output = (stdout.strip() or stderr.strip() or "CHKDSK returned no output.")
        lowered = output.lower()
        requires_reboot = any(x in lowered for x in (
            "cannot lock current drive",
            "would you like to schedule",
            "next time the system restarts",
            "chkdsk cannot run because",
        ))
        repaired = code == 0 and not requires_reboot
        self.log(
            f"CHKDSK repair {drive}: exit={code}, "
            f"reboot_required={requires_reboot}"
        )
        return DiskCheckResult(
            drive, code, output,
            errors_found=True,
            requires_reboot=requires_reboot,
            repaired=repaired,
        )


# ============================================================
# PROGRESS WINDOW
# ============================================================

class ProgressWindow:

    def __init__(
        self,
        parent,
        title: str,
        maximum: int = 100,
    ):
        self.window = tk.Toplevel(parent)

        self.window.title(title)
        self.window.geometry("620x260")
        self.window.resizable(False, False)
        self.window.configure(
            bg=BG
        )

        self.window.transient(parent)
        self.window.grab_set()

        tk.Label(
            self.window,
            text=title,
            bg=BG,
            fg=TEXT,
            font=(FONT, 19, "bold"),
        ).pack(
            anchor="w",
            padx=25,
            pady=(25, 5),
        )

        self.status = tk.Label(
            self.window,
            text="Starting...",
            bg=BG,
            fg=MUTED,
            font=(FONT, 11),
            wraplength=560,
            justify="left",
        )

        self.status.pack(
            anchor="w",
            padx=25,
            pady=(5, 15),
        )

        self.progress = ttk.Progressbar(
            self.window,
            mode="determinate",
            maximum=maximum,
        )

        self.progress.pack(
            fill="x",
            padx=25,
        )

        self.output = tk.Text(
            self.window,
            height=5,
            bg=PANEL,
            fg=MUTED,
            insertbackground=TEXT,
            relief="flat",
            font=(FONT, 9),
        )

        self.output.pack(
            fill="both",
            expand=True,
            padx=25,
            pady=15,
        )

        self.closed = False

        self.window.protocol(
            "WM_DELETE_WINDOW",
            lambda: None,
        )

    def set_status(
        self,
        text: str,
        value: float | None = None,
    ):
        if self.closed:
            return

        try:
            self.status.config(
                text=text
            )

            if value is not None:
                self.progress["value"] = value

            self.window.update_idletasks()

        except tk.TclError:
            self.closed = True

    def write(self, text: str):
        if self.closed:
            return

        try:
            self.output.insert(
                "end",
                text + "\n",
            )
            self.output.see("end")
            self.window.update_idletasks()
        except tk.TclError:
            self.closed = True

    def finish(self):
        if self.closed:
            return

        try:
            self.progress["value"] = 100
            self.window.grab_release()
            self.window.destroy()
            self.closed = True
        except tk.TclError:
            self.closed = True


# ============================================================
# MAIN APPLICATION
# ============================================================

class DriverFixApp(tk.Tk):

    def __init__(self):
        super().__init__()

        self.title(
            f"{APP_NAME} {APP_VERSION}"
        )

        self.geometry("1420x860")
        self.minsize(1180, 720)

        self.configure(
            bg=BG
        )

        self.settings = load_settings()

        self.drivers: list[Driver] = []
        self.visible_drivers: list[Driver] = []

        self.database = DriverDatabase()
        self.maintenance = MaintenanceManager(log_callback=log)
        self.cleanup_items: list[CleanupItem] = []
        self.cleanup_result = CleanupResult()
        self.disk_results: dict[str, DiskCheckResult] = {}

        self.current_page = "dashboard"
        self.sort_column = None
        self.sort_reverse = False

        self.scan_running = False

        self.style = ttk.Style(self)

        try:
            self.style.theme_use("clam")
        except Exception:
            pass

        self._configure_styles()
        self._build_ui()

        self.protocol(
            "WM_DELETE_WINDOW",
            self.destroy,
        )

        self.after(
            250,
            self.start_scan,
        )

    # --------------------------------------------------------
    # STYLES
    # --------------------------------------------------------

    def _configure_styles(self):
        self.style.configure(
            "Treeview",
            background=PANEL,
            foreground=TEXT,
            fieldbackground=PANEL,
            rowheight=38,
            borderwidth=0,
            font=(FONT, 10),
            relief="flat",
        )

        self.style.configure(
            "Treeview.Heading",
            background=PANEL_3,
            foreground=TEXT,
            relief="flat",
            padding=(8, 8),
            font=(FONT, 10, "bold"),
        )

        self.style.map(
            "Treeview",
            background=[
                ("selected", ACCENT)
            ],
            foreground=[
                ("selected", "white")
            ],
        )

        self.style.configure(
            "Horizontal.TProgressbar",
            troughcolor=PANEL_3,
            background=ACCENT,
            bordercolor=PANEL_3,
            lightcolor=ACCENT,
            darkcolor=ACCENT,
        )

    # --------------------------------------------------------
    # GENERAL UI
    # --------------------------------------------------------

    def _button(
        self,
        parent,
        text,
        command,
        width=None,
        bg=PANEL_2,
        fg=TEXT,
    ):
        kwargs = {
            "text": text,
            "command": command,
            "bg": bg,
            "fg": fg,
            "activebackground": ACCENT_HOVER,
            "activeforeground": "white",
            "font": (FONT, 10, "bold"),
            "relief": "flat",
            "bd": 0,
            "cursor": "hand2",
            "padx": 15,
            "pady": 9,
        }
        if width:
            kwargs["width"] = width

        button = tk.Button(parent, **kwargs)
        normal = bg

        def enter(_):
            button.configure(bg=ACCENT_HOVER)

        def leave(_):
            button.configure(bg=normal)

        button.bind("<Enter>", enter)
        button.bind("<Leave>", leave)
        return button

    def _build_ui(self):
        # Main navigation rail
        self.sidebar = tk.Frame(self, bg=PANEL, width=250)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)

        brand = tk.Frame(self.sidebar, bg=PANEL)
        brand.pack(fill="x", padx=20, pady=(22, 24))

        mark = tk.Frame(brand, bg=ACCENT, width=42, height=42)
        mark.pack(side="left")
        mark.pack_propagate(False)
        tk.Label(mark, text="D", bg=ACCENT, fg="white",
                 font=(FONT, 22, "bold")).pack(expand=True)

        brand_text = tk.Frame(brand, bg=PANEL)
        brand_text.pack(side="left", padx=(12, 0))
        tk.Label(brand_text, text="DriverFix", bg=PANEL, fg=TEXT,
                 font=(FONT, 21, "bold")).pack(anchor="w")
        tk.Label(brand_text, text=f"Windows utility  •  v{APP_VERSION}",
                 bg=PANEL, fg=MUTED, font=(FONT, 8)).pack(anchor="w")

        tk.Frame(self.sidebar, bg=BORDER, height=1).pack(fill="x", padx=18, pady=(0, 18))

        self.nav_buttons = {}
        navigation = [
            ("dashboard", "▣", "Dashboard"),
            ("drivers", "▤", "All Drivers"),
            ("problems", "!", "Problem Devices"),
            ("outdated", "↻", "Driver Updates"),
            ("maintenance", "⚙", "Maintenance Center"),
            ("backup", "□", "Backup & Restore"),
        ]

        for key, icon, label in navigation:
            holder = tk.Frame(self.sidebar, bg=PANEL, height=46)
            holder.pack(fill="x", padx=12, pady=2)
            holder.pack_propagate(False)

            button = tk.Button(
                holder, text=f"  {icon}   {label}", command=lambda k=key: self.show_page(k),
                bg=PANEL, fg=MUTED, activebackground=PANEL_2, activeforeground=TEXT,
                font=(FONT, 10, "bold"), relief="flat", bd=0, anchor="w", cursor="hand2",
                padx=12,
            )
            button.pack(fill="both", expand=True)
            self.nav_buttons[key] = button

            def nav_enter(_, b=button, k=key):
                if self.current_page != k:
                    b.configure(bg=PANEL_2, fg=TEXT)

            def nav_leave(_, b=button, k=key):
                if self.current_page != k:
                    b.configure(bg=PANEL, fg=MUTED)

            button.bind("<Enter>", nav_enter)
            button.bind("<Leave>", nav_leave)

        spacer = tk.Frame(self.sidebar, bg=PANEL)
        spacer.pack(fill="both", expand=True)

        status_box = tk.Frame(self.sidebar, bg=PANEL_2)
        status_box.pack(fill="x", padx=14, pady=(0, 14))

        admin_ok = is_admin()
        tk.Label(status_box, text="●  Administrator" if admin_ok else "●  Standard user",
                 bg=PANEL_2, fg=GREEN if admin_ok else YELLOW,
                 font=(FONT, 9, "bold")).pack(anchor="w", padx=14, pady=(11, 2))
        tk.Label(status_box, text="Ready for driver maintenance", bg=PANEL_2, fg=MUTED,
                 font=(FONT, 8)).pack(anchor="w", padx=14, pady=(0, 11))

        self.content = tk.Frame(self, bg=BG)
        self.content.pack(side="left", fill="both", expand=True)

        self.show_page("dashboard")

    def clear_content(self):
        for child in self.content.winfo_children():
            child.destroy()

    def show_page(self, page: str):
        self.current_page = page

        for key, button in self.nav_buttons.items():
            if key == page:
                button.configure(bg=ACCENT, fg="white", activebackground=ACCENT_HOVER)
            else:
                button.configure(bg=PANEL, fg=MUTED, activebackground=PANEL_2)

        self.clear_content()

        if page == "dashboard":
            self._show_dashboard()
        elif page == "drivers":
            self._show_drivers()
        elif page == "problems":
            self._show_problems()
        elif page == "outdated":
            self._show_outdated()
        elif page == "maintenance":
            self._show_maintenance()
        elif page == "backup":
            self._show_backup()

    def _page_header(self, title: str, subtitle: str):
        header = tk.Frame(self.content, bg=BG)
        header.pack(fill="x", padx=34, pady=(27, 20))

        left = tk.Frame(header, bg=BG)
        left.pack(side="left", fill="x", expand=True)

        tk.Label(left, text=title, bg=BG, fg=TEXT,
                 font=(FONT, 26, "bold")).pack(anchor="w")
        tk.Label(left, text=subtitle, bg=BG, fg=MUTED,
                 font=(FONT, 10)).pack(anchor="w", pady=(4, 0))

        return header

    def _show_dashboard(self):
        header = self._page_header(
            "Dashboard",
            "Inspect, repair, back up and maintain Windows drivers.",
        )

        self._button(
            header,
            "↻  Scan Now",
            self.start_scan,
            bg=ACCENT,
        ).pack(
            side="right"
        )

        stats_frame = tk.Frame(
            self.content,
            bg=BG,
        )

        stats_frame.pack(
            fill="x",
            padx=32,
        )

        self.stat_total = self._stat_card(
            stats_frame,
            "Installed Drivers",
            "0",
            ACCENT,
        )

        self.stat_problem = self._stat_card(
            stats_frame,
            "Problem Devices",
            "0",
            RED,
        )

        self.stat_outdated = self._stat_card(
            stats_frame,
            "Available Updates",
            "0",
            YELLOW,
        )

        self.stat_current = self._stat_card(
            stats_frame,
            "Current",
            "0",
            GREEN,
        )

        self._dashboard_actions()

        activity = tk.Frame(
            self.content,
            bg=PANEL,
        )

        activity.pack(
            fill="both",
            expand=True,
            padx=32,
            pady=24,
        )

        tk.Label(
            activity,
            text="System Overview",
            bg=PANEL,
            fg=TEXT,
            font=(FONT, 15, "bold"),
        ).pack(
            anchor="w",
            padx=20,
            pady=(18, 3),
        )

        self.dashboard_info = tk.Label(
            activity,
            text="Waiting for scan...",
            bg=PANEL,
            fg=MUTED,
            justify="left",
            anchor="nw",
            font=(FONT, 10),
        )

        self.dashboard_info.pack(
            fill="both",
            expand=True,
            padx=20,
            pady=15,
        )

        self.update_statistics()

    def _stat_card(self, parent, title, value, accent):
        outer = tk.Frame(parent, bg=BORDER)
        outer.pack(side="left", fill="x", expand=True, padx=(0, 12))

        card = tk.Frame(outer, bg=PANEL, height=112)
        card.pack(fill="both", expand=True, padx=1, pady=1)
        card.pack_propagate(False)

        top = tk.Frame(card, bg=PANEL)
        top.pack(fill="x", padx=18, pady=(15, 0))
        tk.Frame(top, bg=accent, width=7, height=7).pack(side="left", pady=3)
        tk.Label(top, text=title.upper(), bg=PANEL, fg=MUTED,
                 font=(FONT, 8, "bold")).pack(side="left", padx=(8, 0))

        label = tk.Label(card, text=value, bg=PANEL, fg=TEXT,
                         font=(FONT, 27, "bold"))
        label.pack(anchor="w", padx=18, pady=(5, 0))
        return label

    def _dashboard_actions(self):
        actions = tk.Frame(
            self.content,
            bg=BG,
        )

        actions.pack(
            fill="x",
            padx=32,
            pady=(24, 0),
        )

        actions.columnconfigure(
            0,
            weight=1,
        )

        actions.columnconfigure(
            1,
            weight=1,
        )

        actions.columnconfigure(
            2,
            weight=1,
        )

        self._action_card(
            actions,
            0,
            "Update Drivers",
            "Install verified INF/CAB/ZIP packages.",
            "↻",
            self.update_all,
        )

        self._action_card(
            actions,
            1,
            "Fix Problems",
            "Repair devices reporting Windows errors.",
            "⚙",
            self.fix_all_problems,
        )

        self._action_card(
            actions,
            2,
            "Create Backup",
            "Export the current Windows driver store.",
            "▣",
            self.backup_drivers,
        )

    def _action_card(self, parent, column, title, description, icon, command):
        outer = tk.Frame(parent, bg=BORDER)
        outer.grid(row=0, column=column, sticky="nsew", padx=(0, 12))

        card = tk.Frame(outer, bg=PANEL, padx=18, pady=15)
        card.pack(fill="both", expand=True, padx=1, pady=1)

        icon_box = tk.Frame(card, bg=PANEL_2, width=42, height=42)
        icon_box.pack(anchor="w")
        icon_box.pack_propagate(False)
        tk.Label(icon_box, text=icon, bg=PANEL_2, fg=ACCENT,
                 font=(FONT, 18, "bold")).pack(expand=True)

        tk.Label(card, text=title, bg=PANEL, fg=TEXT,
                 font=(FONT, 13, "bold")).pack(anchor="w", pady=(11, 2))
        tk.Label(card, text=description, bg=PANEL, fg=MUTED,
                 font=(FONT, 9), wraplength=270, justify="left").pack(anchor="w")

        self._button(card, "Open", command, bg=PANEL_3).pack(anchor="w", pady=(13, 0))

    def _create_driver_table(
        self,
        parent,
        drivers,
    ):
        toolbar = tk.Frame(
            parent,
            bg=BG,
        )

        toolbar.pack(
            fill="x",
            pady=(0, 10),
        )

        self.search_var = tk.StringVar()

        search = tk.Entry(
            toolbar,
            textvariable=self.search_var,
            bg=PANEL,
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            font=(FONT, 10),
        )

        search.pack(
            side="left",
            fill="x",
            expand=True,
            ipady=9,
        )

        search.insert(
            0,
            "",
        )

        self._button(
            toolbar,
            "Search",
            self.refresh_table,
        ).pack(
            side="left",
            padx=(8, 0),
        )

        self._button(
            toolbar,
            "↻ Scan",
            self.start_scan,
            bg=ACCENT,
        ).pack(
            side="left",
            padx=(8, 0),
        )

        self._button(
            toolbar,
            "Update",
            self.update_selected,
        ).pack(
            side="left",
            padx=(8, 0),
        )

        self._button(
            toolbar,
            "Fix",
            self.fix_selected,
        ).pack(
            side="left",
            padx=(8, 0),
        )

        table_frame = tk.Frame(
            parent,
            bg=PANEL,
        )

        table_frame.pack(
            fill="both",
            expand=True,
        )

        columns = (
            "name",
            "provider",
            "version",
            "latest",
            "status",
            "problem",
        )

        self.tree = ttk.Treeview(
            table_frame,
            columns=columns,
            show="headings",
            selectmode="browse",
        )

        headings = {
            "name": "Device",
            "provider": "Provider",
            "version": "Installed",
            "latest": "Latest",
            "status": "Status",
            "problem": "Problem",
        }

        widths = {
            "name": 330,
            "provider": 190,
            "version": 120,
            "latest": 120,
            "status": 120,
            "problem": 100,
        }

        for column in columns:
            self.tree.heading(
                column,
                text=headings[column],
                command=lambda c=column:
                    self._sort_tree(c),
            )

            self.tree.column(
                column,
                width=widths[column],
                minwidth=80,
                anchor="w",
            )

        scrollbar = ttk.Scrollbar(
            table_frame,
            orient="vertical",
            command=self.tree.yview,
        )

        self.tree.configure(
            yscrollcommand=scrollbar.set
        )

        self.tree.pack(
            side="left",
            fill="both",
            expand=True,
        )

        scrollbar.pack(
            side="right",
            fill="y",
        )

        self.tree.bind(
            "<Double-1>",
            lambda _: self.show_selected_driver(),
        )

        self.tree.bind(
            "<Return>",
            lambda _: self.show_selected_driver(),
        )

        self.current_table_drivers = drivers

        self.refresh_table()

    def _populate_table(
        self,
        drivers,
    ):
        if not hasattr(self, "tree"):
            return

        for item in self.tree.get_children():
            self.tree.delete(item)

        self.current_table_drivers = list(
            drivers
        )

        for index, driver in enumerate(
            self.current_table_drivers
        ):

            status = version_status(
                driver
            )

            if driver.has_problem:
                status_text = "Problem"

            elif status == "Outdated":
                status_text = "Outdated"

            else:
                status_text = status

            self.tree.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    driver.device_name
                    or "(Unnamed device)",

                    driver.driver_provider
                    or driver.manufacturer
                    or "Unknown",

                    driver.driver_version
                    or "Unknown",

                    driver.latest_version
                    or "—",

                    status_text,

                    driver.problem_code
                    if driver.has_problem
                    else "—",
                ),
            )

    def refresh_table(self):
        if not hasattr(self, "tree"):
            return

        search = ""

        if hasattr(self, "search_var"):
            search = self.search_var.get().strip().lower()

        drivers = self.drivers

        if search:
            drivers = [
                driver
                for driver in drivers
                if search in driver.search_text()
            ]

        self.visible_drivers = drivers

        self._populate_table(
            drivers
        )

    def _sort_tree(self, column):
        if self.sort_column == column:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_column = column
            self.sort_reverse = False

        def key(driver):
            if column == "name":
                return driver.device_name.lower()

            if column == "provider":
                return (
                    driver.driver_provider
                    or driver.manufacturer
                ).lower()

            if column == "version":
                return version_parts(
                    driver.driver_version
                )

            if column == "latest":
                return version_parts(
                    driver.latest_version
                )

            if column == "status":
                return version_status(
                    driver
                )

            if column == "problem":
                return driver.problem_code

            return ""

        sorted_drivers = sorted(
            self.current_table_drivers,
            key=key,
            reverse=self.sort_reverse,
        )

        self._populate_table(
            sorted_drivers
        )

    # --------------------------------------------------------
    # DRIVER PAGES
    # --------------------------------------------------------

    def _show_drivers(self):
        self._page_header(
            "All Drivers",
            "Every driver detected by Windows.",
        )

        body = tk.Frame(
            self.content,
            bg=BG,
        )

        body.pack(
            fill="both",
            expand=True,
            padx=32,
        )

        self._create_driver_table(
            body,
            self.drivers,
        )

    def _show_problems(self):
        self._page_header(
            "Problem Devices",
            "Devices currently reporting a Windows configuration problem.",
        )

        body = tk.Frame(
            self.content,
            bg=BG,
        )

        body.pack(
            fill="both",
            expand=True,
            padx=32,
        )

        problems = [
            d
            for d in self.drivers
            if d.has_problem
        ]

        self._create_driver_table(
            body,
            problems,
        )

    def _show_outdated(self):
        self._page_header(
            "Driver Updates",
            "Drivers for which DriverFix has a newer catalog version.",
        )

        body = tk.Frame(
            self.content,
            bg=BG,
        )

        body.pack(
            fill="both",
            expand=True,
            padx=32,
        )

        outdated = [
            d
            for d in self.drivers
            if d.is_outdated
        ]

        self._create_driver_table(
            body,
            outdated,
        )

    # --------------------------------------------------------
    # MAINTENANCE PAGE
    # --------------------------------------------------------

    def _show_maintenance(self):
        self._page_header(
            "Maintenance Center",
            "Driver repair, temporary-data cleanup and NTFS health tools.",
        )

        body = tk.Frame(self.content, bg=BG)
        body.pack(fill="both", expand=True, padx=32, pady=(0, 20))
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)

        # Temporary data card
        temp_card = tk.Frame(body, bg=PANEL)
        temp_card.grid(row=0, column=0, sticky="nsew", padx=(0, 8), pady=(0, 8))

        tk.Label(temp_card, text="🧹  Temporary Data", bg=PANEL, fg=TEXT,
                 font=(FONT, 18, "bold")).pack(anchor="w", padx=22, pady=(20, 4))
        tk.Label(temp_card,
                 text="Find safe-to-remove temporary files without touching normal system files.",
                 bg=PANEL, fg=MUTED, wraplength=500, justify="left",
                 font=(FONT, 10)).pack(anchor="w", padx=22)

        self.temp_summary = tk.Label(
            temp_card,
            text=(
                f"Last scan: {format_size(self.cleanup_result.removable_size)} available\n"
                f"Files: {self.cleanup_result.removable_files:,}"
            ),
            bg=PANEL, fg=TEXT, justify="left", font=(FONT, 12, "bold")
        )
        self.temp_summary.pack(anchor="w", padx=22, pady=(20, 14))

        temp_buttons = tk.Frame(temp_card, bg=PANEL)
        temp_buttons.pack(anchor="w", padx=22, pady=(0, 20))
        self._button(temp_buttons, "Scan Temporary Data", self.scan_temp_data,
                     bg=ACCENT).pack(side="left")
        self._button(temp_buttons, "Clean", self.clean_temp_data).pack(side="left", padx=(8, 0))

        # Disk card
        disk_card = tk.Frame(body, bg=PANEL)
        disk_card.grid(row=0, column=1, sticky="nsew", padx=(8, 0), pady=(0, 8))

        tk.Label(disk_card, text="💽  NTFS / Disk Health", bg=PANEL, fg=TEXT,
                 font=(FONT, 18, "bold")).pack(anchor="w", padx=22, pady=(20, 4))
        tk.Label(disk_card,
                 text="Run Windows CHKDSK online scans and, when needed, filesystem repair.",
                 bg=PANEL, fg=MUTED, wraplength=500, justify="left",
                 font=(FONT, 10)).pack(anchor="w", padx=22)

        drive_row = tk.Frame(disk_card, bg=PANEL)
        drive_row.pack(fill="x", padx=22, pady=(20, 10))
        tk.Label(drive_row, text="Drive", bg=PANEL, fg=MUTED,
                 font=(FONT, 10, "bold")).pack(side="left")

        self.drive_var = tk.StringVar(
            value=(self.maintenance.fixed_drives()[0] if self.maintenance.fixed_drives() else "C:")
        )
        self.drive_combo = ttk.Combobox(
            drive_row, textvariable=self.drive_var, state="readonly", width=10,
            values=self.maintenance.fixed_drives()
        )
        self.drive_combo.pack(side="left", padx=(10, 0))

        self.disk_summary = tk.Label(
            disk_card,
            text="No disk scan performed yet.",
            bg=PANEL, fg=TEXT, justify="left", anchor="w",
            font=(FONT, 11), wraplength=500
        )
        self.disk_summary.pack(fill="x", padx=22, pady=(10, 14))

        disk_buttons = tk.Frame(disk_card, bg=PANEL)
        disk_buttons.pack(anchor="w", padx=22, pady=(0, 20))
        self._button(disk_buttons, "Scan NTFS", self.scan_selected_disk,
                     bg=ACCENT).pack(side="left")
        self._button(disk_buttons, "Repair", self.repair_selected_disk).pack(side="left", padx=(8, 0))

        # Driver health card
        driver_card = tk.Frame(body, bg=PANEL)
        driver_card.grid(row=1, column=0, sticky="nsew", padx=(0, 8), pady=(8, 0))

        tk.Label(driver_card, text="🛠  Driver Health", bg=PANEL, fg=TEXT,
                 font=(FONT, 18, "bold")).pack(anchor="w", padx=22, pady=(20, 4))
        tk.Label(driver_card,
                 text="Use DriverFix's tested driver repair/update pipeline.",
                 bg=PANEL, fg=MUTED, font=(FONT, 10)).pack(anchor="w", padx=22)

        driver_status = (
            f"Problem devices: {sum(1 for d in self.drivers if d.has_problem)}\n"
            f"Available updates: {sum(1 for d in self.drivers if d.is_outdated)}"
        )
        tk.Label(driver_card, text=driver_status, bg=PANEL, fg=TEXT,
                 font=(FONT, 12, "bold"), justify="left").pack(anchor="w", padx=22, pady=(20, 14))

        driver_buttons = tk.Frame(driver_card, bg=PANEL)
        driver_buttons.pack(anchor="w", padx=22, pady=(0, 20))
        self._button(driver_buttons, "Update All", self.update_all, bg=ACCENT).pack(side="left")
        self._button(driver_buttons, "Fix All Problems", self.fix_all_problems).pack(side="left", padx=(8, 0))

        # Tools card
        tools_card = tk.Frame(body, bg=PANEL)
        tools_card.grid(row=1, column=1, sticky="nsew", padx=(8, 0), pady=(8, 0))

        tk.Label(tools_card, text="⚙  Windows Tools", bg=PANEL, fg=TEXT,
                 font=(FONT, 18, "bold")).pack(anchor="w", padx=22, pady=(20, 4))
        tk.Label(tools_card,
                 text="Useful recovery and troubleshooting tools that DriverFix does not automate blindly.",
                 bg=PANEL, fg=MUTED, wraplength=500, justify="left",
                 font=(FONT, 10)).pack(anchor="w", padx=22)

        self._button(tools_card, "Create Driver Backup", self.backup_drivers,
                     bg=PANEL_3).pack(anchor="w", padx=22, pady=(20, 8))
        self._button(tools_card, "Open Device Manager", self.open_device_manager,
                     bg=PANEL_3).pack(anchor="w", padx=22, pady=(0, 8))
        self._button(tools_card, "Export Driver Report", self.export_report,
                     bg=PANEL_3).pack(anchor="w", padx=22, pady=(0, 20))

    # --------------------------------------------------------
    # TEMP CLEANER
    # --------------------------------------------------------

    def scan_temp_data(self):
        progress_window = ProgressWindow(self, "Temporary Data Scan")

        def worker():
            try:
                items, result = self.maintenance.scan_temp(
                    lambda text: progress_window.set_status(text)
                )
                self.after(0, lambda: self._finish_temp_scan(progress_window, items, result))
            except Exception as exc:
                self.after(0, lambda e=str(exc): self._operation_error(progress_window, e))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_temp_scan(self, progress_window, items, result):
        self.cleanup_items = items
        self.cleanup_result = result
        progress_window.set_status("Temporary-data scan complete.", 100)
        progress_window.write(
            f"Found {result.removable_files:,} files totaling {format_size(result.removable_size)}."
        )
        progress_window.finish()
        if self.current_page == "maintenance":
            self.show_page("maintenance")
        messagebox.showinfo(
            "Temporary Data Scan",
            f"Potentially removable data: {format_size(result.removable_size)}\n\n"
            f"Files: {result.removable_files:,}\n"
            f"Skipped/locked: {result.skipped_files:,}",
            parent=self,
        )

    def clean_temp_data(self):
        if not self.cleanup_items:
            self.scan_temp_data()
            return

        if not messagebox.askyesno(
            "Clean Temporary Data",
            f"DriverFix found {format_size(self.cleanup_result.removable_size)} of temporary data.\n\n"
            "Locked files will be skipped. Continue?",
            parent=self,
        ):
            return

        progress_window = ProgressWindow(self, "Cleaning Temporary Data")

        def worker():
            try:
                result = self.maintenance.clean_temp(
                    self.cleanup_items,
                    lambda text: progress_window.set_status(text)
                )
                self.after(0, lambda: self._finish_temp_clean(progress_window, result))
            except Exception as exc:
                self.after(0, lambda e=str(exc): self._operation_error(progress_window, e))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_temp_clean(self, progress_window, result):
        progress_window.set_status("Cleanup complete.", 100)
        progress_window.write(f"Freed {format_size(result.freed_bytes)}.")
        progress_window.finish()
        self.cleanup_items = []
        self.cleanup_result = CleanupResult()
        if self.current_page == "maintenance":
            self.show_page("maintenance")
        messagebox.showinfo(
            "Cleanup Complete",
            f"Freed {format_size(result.freed_bytes)}.\n\n"
            f"Deleted files: {result.deleted_files:,}\n"
            f"Skipped/locked: {result.skipped_files:,}",
            parent=self,
        )

    # --------------------------------------------------------
    # NTFS / DISK HEALTH
    # --------------------------------------------------------

    def scan_selected_disk(self):
        drive = self.drive_var.get().strip() if hasattr(self, "drive_var") else "C:"
        if not is_admin():
            messagebox.showwarning(
                "Administrator Required",
                "NTFS scanning requires DriverFix to run as Administrator.",
                parent=self,
            )
            return

        progress_window = ProgressWindow(self, f"NTFS Scan — {drive}")

        def worker():
            try:
                result = self.maintenance.scan_disk(
                    drive,
                    lambda text: progress_window.set_status(text)
                )
                self.after(0, lambda: self._finish_disk_scan(progress_window, result))
            except Exception as exc:
                self.after(0, lambda e=str(exc): self._operation_error(progress_window, e))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_disk_scan(self, progress_window, result):
        self.disk_results[result.drive] = result
        progress_window.write(result.output[-3000:] if result.output else "No output.")
        progress_window.set_status("NTFS scan complete.", 100)
        progress_window.finish()

        if result.exit_code != 0:
            status = f"CHKDSK returned exit code {result.exit_code}."
            color = RED
        elif result.errors_found:
            status = "Filesystem issues were reported. Repair may be required."
            color = YELLOW
        else:
            status = "NTFS scan completed without reported filesystem errors."
            color = GREEN

        if hasattr(self, "disk_summary"):
            self.disk_summary.config(text=status, fg=color)

        messagebox.showinfo(
            "NTFS Scan",
            status + "\n\nSee the operation log for the Windows CHKDSK output.",
            parent=self,
        )

    def repair_selected_disk(self):
        drive = self.drive_var.get().strip() if hasattr(self, "drive_var") else "C:"
        if not is_admin():
            messagebox.showwarning(
                "Administrator Required",
                "NTFS repair requires DriverFix to run as Administrator.",
                parent=self,
            )
            return

        previous = self.disk_results.get(drive.upper())
        if previous and not previous.errors_found:
            if not messagebox.askyesno(
                "Repair Disk",
                f"The last scan did not report filesystem errors on {drive}.\n\n"
                "Run CHKDSK /f anyway?",
                parent=self,
            ):
                return
        else:
            if not messagebox.askyesno(
                "Repair Disk",
                f"Run Windows filesystem repair on {drive}?\n\n"
                "If the volume is in use, Windows may schedule the repair for the next restart.",
                parent=self,
            ):
                return

        progress_window = ProgressWindow(self, f"NTFS Repair — {drive}")

        def worker():
            try:
                result = self.maintenance.repair_disk(
                    drive,
                    lambda text: progress_window.set_status(text)
                )
                self.after(0, lambda: self._finish_disk_repair(progress_window, result))
            except Exception as exc:
                self.after(0, lambda e=str(exc): self._operation_error(progress_window, e))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_disk_repair(self, progress_window, result):
        self.disk_results[result.drive] = result
        progress_window.write(result.output[-3000:] if result.output else "No output.")
        progress_window.set_status("Filesystem repair operation complete.", 100)
        progress_window.finish()

        if result.requires_reboot:
            message = (
                f"Windows could not lock {result.drive} because it is in use.\n\n"
                "CHKDSK can schedule the repair for the next restart."
            )
        elif result.repaired:
            message = f"CHKDSK completed the repair operation for {result.drive}."
        else:
            message = f"CHKDSK finished with exit code {result.exit_code}."

        if hasattr(self, "disk_summary"):
            self.disk_summary.config(text=message, fg=GREEN if result.repaired else YELLOW)

        messagebox.showinfo("NTFS Repair", message, parent=self)

    # --------------------------------------------------------
    # BACKUP PAGE
    # --------------------------------------------------------

    def _show_backup(self):
        self._page_header(
            "Backup & Restore",
            "Protect and restore the Windows driver store.",
        )

        body = tk.Frame(
            self.content,
            bg=BG,
        )

        body.pack(
            fill="both",
            expand=True,
            padx=32,
        )

        card = tk.Frame(
            body,
            bg=PANEL,
        )

        card.pack(
            fill="x",
            pady=(0, 15),
        )

        tk.Label(
            card,
            text="Driver Backup",
            bg=PANEL,
            fg=TEXT,
            font=(FONT, 17, "bold"),
        ).pack(
            anchor="w",
            padx=22,
            pady=(20, 3),
        )

        tk.Label(
            card,
            text=(
                "Before DriverFix changes a driver, it can export "
                "the installed third-party driver packages using "
                "Windows PnPUtil."
            ),
            bg=PANEL,
            fg=MUTED,
            wraplength=800,
            justify="left",
            font=(FONT, 10),
        ).pack(
            anchor="w",
            padx=22,
            pady=(0, 15),
        )

        self._button(
            card,
            "Create Backup",
            self.backup_drivers,
            bg=ACCENT,
        ).pack(
            anchor="w",
            padx=22,
            pady=(0, 20),
        )

        restore_card = tk.Frame(
            body,
            bg=PANEL,
        )

        restore_card.pack(
            fill="x"
        )

        tk.Label(
            restore_card,
            text="Restore Backup",
            bg=PANEL,
            fg=TEXT,
            font=(FONT, 17, "bold"),
        ).pack(
            anchor="w",
            padx=22,
            pady=(20, 3),
        )

        tk.Label(
            restore_card,
            text=(
                "Select a DriverFix backup directory containing "
                "exported INF packages."
            ),
            bg=PANEL,
            fg=MUTED,
            font=(FONT, 10),
        ).pack(
            anchor="w",
            padx=22,
            pady=(0, 15),
        )

        self._button(
            restore_card,
            "Select Backup",
            self.restore_drivers,
            bg=PANEL_3,
        ).pack(
            anchor="w",
            padx=22,
            pady=(0, 20),
        )

    # --------------------------------------------------------
    # SCANNING
    # --------------------------------------------------------

    def start_scan(self):
        if self.scan_running:
            return

        self.scan_running = True

        self._set_status_bar(
            "Scanning Windows drivers..."
        )

        thread = threading.Thread(
            target=self._scan_worker,
            daemon=True,
        )

        thread.start()

    def _scan_worker(self):
        try:
            def progress(text):
                self.after(
                    0,
                    lambda t=text:
                    self._set_status_bar(t),
                )

            drivers = DriverScanner.scan(
                progress
            )

            self.database.load()

            progress(
                "Matching drivers against DriverFix database..."
            )

            self.database.enrich(
                drivers
            )

            self.after(
                0,
                lambda: self._finish_scan(
                    drivers
                ),
            )

        except Exception as exc:
            log(
                f"Scan failed: {exc}"
            )

            self.after(
                0,
                lambda e=str(exc):
                self._scan_failed(e),
            )

    def _finish_scan(
        self,
        drivers: list[Driver],
    ):
        self.drivers = drivers
        self.scan_running = False

        self.update_statistics()
        self._set_status_bar(
            f"Scan complete — {len(drivers)} drivers found."
        )

        if self.current_page != "dashboard":
            self.show_page(
                self.current_page
            )
        else:
            self.show_page(
                "dashboard"
            )

    def _scan_failed(self, error):
        self.scan_running = False

        self._set_status_bar(
            "Scan failed."
        )

        messagebox.showerror(
            "DriverFix Scan Error",
            error,
            parent=self,
        )

    # --------------------------------------------------------
    # STATISTICS
    # --------------------------------------------------------

    def update_statistics(self):
        total = len(self.drivers)

        problems = sum(
            1
            for d in self.drivers
            if d.has_problem
        )

        outdated = sum(
            1
            for d in self.drivers
            if d.is_outdated
        )

        current = sum(
            1
            for d in self.drivers
            if version_status(d) == "Current"
        )

        if hasattr(self, "stat_total"):
            self.stat_total.config(
                text=str(total)
            )

        if hasattr(self, "stat_problem"):
            self.stat_problem.config(
                text=str(problems)
            )

        if hasattr(self, "stat_outdated"):
            self.stat_outdated.config(
                text=str(outdated)
            )

        if hasattr(self, "stat_current"):
            self.stat_current.config(
                text=str(current)
            )

        if hasattr(self, "dashboard_info"):
            self.dashboard_info.config(
                text=(
                    f"Drivers detected: {total}\n\n"
                    f"Devices reporting problems: {problems}\n"
                    f"Drivers with newer catalog versions: {outdated}\n"
                    f"Drivers currently matching their catalog version: {current}\n\n"
                    f"Database signatures: "
                    f"{len(self.database.signatures)}\n"
                    f"Internet catalog records: "
                    f"{len(self.database.internet)}\n\n"
                    f"Administrator: "
                    f"{'Yes' if is_admin() else 'No'}"
                )
            )

    # --------------------------------------------------------
    # STATUS BAR
    # --------------------------------------------------------

    def _set_status_bar(self, text):
        if not hasattr(self, "status_bar"):
            self.status_bar = tk.Label(
                self.content,
                text="",
                bg=BG,
                fg=MUTED,
                font=(FONT, 9),
            )

            self.status_bar.pack(
                side="bottom",
                fill="x",
                padx=32,
                pady=(0, 8),
            )

        self.status_bar.config(
            text=text
        )

    # --------------------------------------------------------
    # SELECTION
    # --------------------------------------------------------

    def get_selected_driver(self):
        if not hasattr(self, "tree"):
            return None

        selection = self.tree.selection()

        if not selection:
            return None

        try:
            index = int(
                selection[0]
            )

            return self.current_table_drivers[
                index
            ]

        except (
            ValueError,
            IndexError,
        ):
            return None

    # --------------------------------------------------------
    # DETAILS
    # --------------------------------------------------------

    def show_selected_driver(self):
        driver = self.get_selected_driver()

        if not driver:
            messagebox.showinfo(
                "DriverFix",
                "Select a driver first.",
                parent=self,
            )
            return

        window = tk.Toplevel(
            self
        )

        window.title(
            "Driver Details"
        )

        window.geometry(
            "760x680"
        )

        window.configure(
            bg=BG
        )

        window.transient(
            self
        )

        tk.Label(
            window,
            text=driver.device_name
            or "(Unnamed device)",
            bg=BG,
            fg=TEXT,
            font=(FONT, 19, "bold"),
            wraplength=700,
            justify="left",
        ).pack(
            anchor="w",
            padx=25,
            pady=(25, 4),
        )

        status = (
            "Problem"
            if driver.has_problem
            else version_status(driver)
        )

        status_color = (
            RED
            if driver.has_problem
            else (
                YELLOW
                if status == "Outdated"
                else GREEN
            )
        )

        tk.Label(
            window,
            text=status,
            bg=BG,
            fg=status_color,
            font=(FONT, 11, "bold"),
        ).pack(
            anchor="w",
            padx=25,
        )

        details = tk.Frame(
            window,
            bg=PANEL,
        )

        details.pack(
            fill="both",
            expand=True,
            padx=25,
            pady=20,
        )

        fields = [
            ("Manufacturer", driver.manufacturer),
            ("Provider", driver.driver_provider),
            ("Installed version", driver.driver_version),
            ("Latest version", driver.latest_version),
            ("Driver date", driver.driver_date),
            ("INF", driver.inf_name),
            ("Device ID", driver.device_id),
            ("Hardware ID", driver.hardware_id),
            ("Compatible IDs", driver.compatible_ids),
            ("Signed", driver.signed),
            ("Signer", driver.signer),
            ("Class", driver.class_name),
            ("Problem code", driver.problem_code),
            ("Windows status", driver.status),
            ("Source", driver.source),
            ("Download URL", driver.download_url),
        ]

        for row, (
            label,
            value,
        ) in enumerate(fields):

            tk.Label(
                details,
                text=label,
                bg=PANEL,
                fg=MUTED,
                font=(FONT, 9, "bold"),
            ).grid(
                row=row,
                column=0,
                sticky="nw",
                padx=15,
                pady=5,
            )

            tk.Label(
                details,
                text=value or "—",
                bg=PANEL,
                fg=TEXT,
                font=(FONT, 9),
                wraplength=520,
                justify="left",
            ).grid(
                row=row,
                column=1,
                sticky="nw",
                padx=15,
                pady=5,
            )

        buttons = tk.Frame(
            window,
            bg=BG,
        )

        buttons.pack(
            fill="x",
            padx=25,
            pady=(0, 20),
        )

        if driver.is_outdated:
            self._button(
                buttons,
                "↻ Update Driver",
                lambda: (
                    window.destroy(),
                    self.update_driver(
                        driver
                    ),
                ),
                bg=ACCENT,
            ).pack(
                side="left",
                padx=(0, 8),
            )

        if driver.has_problem:
            self._button(
                buttons,
                "⚙ Fix Problem",
                lambda: (
                    window.destroy(),
                    self.fix_driver(
                        driver
                    ),
                ),
            ).pack(
                side="left",
                padx=(0, 8),
            )

        if (
            driver.download_url
            and official_url(
                driver.download_url
            )
        ):
            self._button(
                buttons,
                "Open Official Source",
                lambda: webbrowser.open(
                    driver.download_url
                ),
            ).pack(
                side="left",
                padx=(0, 8),
            )

        self._button(
            buttons,
            "Close",
            window.destroy,
        ).pack(
            side="right"
        )

    # --------------------------------------------------------
    # UPDATE SELECTED
    # --------------------------------------------------------

    def update_selected(self):
        driver = self.get_selected_driver()

        if not driver:
            messagebox.showinfo(
                "Update Driver",
                "Select an outdated driver first.",
                parent=self,
            )
            return

        self.update_driver(
            driver
        )

    def update_driver(
        self,
        driver: Driver,
    ):
        if not is_admin():
            messagebox.showwarning(
                "Administrator Required",
                "Updating drivers requires Administrator privileges.\n\n"
                "Restart DriverFix as Administrator and try again.",
                parent=self,
            )
            return

        if not driver.is_outdated:
            messagebox.showinfo(
                "DriverFix",
                "This driver is not currently marked as outdated.",
                parent=self,
            )
            return

        if not driver.download_url:
            self._show_no_package_source(
                driver
            )
            return

        if not official_url(
            driver.download_url
        ):
            messagebox.showwarning(
                "Unverified Source",
                "DriverFix will not download a driver from this source.\n\n"
                "Only recognized official vendor/Microsoft domains are allowed.",
                parent=self,
            )
            return

        if not looks_like_installable_package(
            driver.download_url
        ):
            result = messagebox.askyesno(
                "Official Driver Source",
                "The catalog has an official source, but it is not a direct "
                "INF/CAB/ZIP package.\n\n"
                "DriverFix will NOT execute a vendor EXE automatically.\n\n"
                "Open the official source in your browser instead?",
                parent=self,
            )

            if result:
                webbrowser.open(
                    driver.download_url
                )

            return

        if self.settings.get(
            "confirm_changes",
            True,
        ):

            result = messagebox.askyesno(
                "Update Driver",
                (
                    f"Update:\n\n"
                    f"{driver.device_name}\n\n"
                    f"Installed: {driver.driver_version}\n"
                    f"Latest: {driver.latest_version}\n\n"
                    f"DriverFix will create a backup first."
                ),
                parent=self,
            )

            if not result:
                return

        self._run_update_operation(
            [driver]
        )

    def _run_update_operation(
        self,
        drivers: list[Driver],
    ):

        progress_window = ProgressWindow(
            self,
            "Driver Update",
        )

        def worker():

            results = []
            backup_path = None

            try:
                if self.settings.get(
                    "backup_before_changes",
                    True,
                ):

                    progress_window.set_status(
                        "Creating safety backup...",
                        5,
                    )

                    backup_path = (
                        BackupManager.create_backup(
                            lambda text:
                            progress_window.set_status(
                                text
                            )
                        )
                    )

                    progress_window.write(
                        f"Backup: {backup_path}"
                    )

                total = len(drivers)

                for index, driver in enumerate(
                    drivers,
                    start=1,
                ):

                    percent = (
                        index - 1
                    ) / total * 90 + 5

                    progress_window.set_status(
                        f"Updating {driver.device_name}...",
                        percent,
                    )

                    progress_window.write(
                        f"Updating: "
                        f"{driver.device_name}"
                    )

                    if not driver.can_auto_update:
                        results.append(
                            (
                                driver,
                                False,
                                "No verified direct installable package.",
                            )
                        )

                        progress_window.write(
                            "Skipped: no verified package."
                        )

                        continue

                    try:
                        package = (
                            DriverPackageManager.download_package(
                                driver.download_url,
                                lambda text:
                                progress_window.set_status(
                                    text
                                ),
                            )
                        )

                        success, output = (
                            DriverPackageManager.install_package(
                                package,
                                lambda text:
                                progress_window.set_status(
                                    text
                                ),
                            )
                        )

                        results.append(
                            (
                                driver,
                                success,
                                output,
                            )
                        )

                        progress_window.write(
                            "Success"
                            if success
                            else "Failed"
                        )

                    except Exception as exc:
                        results.append(
                            (
                                driver,
                                False,
                                str(exc),
                            )
                        )

                        progress_window.write(
                            f"Error: {exc}"
                        )

                progress_window.set_status(
                    "Rescanning devices...",
                    96,
                )

                DriverOperations.scan_devices()

                progress_window.set_status(
                    "Update operation complete.",
                    100,
                )

                self.after(
                    800,
                    lambda:
                    self._finish_operation(
                        progress_window,
                        results,
                        "Driver Update",
                    ),
                )

            except Exception as exc:

                self.after(
                    0,
                    lambda e=str(exc):
                    self._operation_error(
                        progress_window,
                        e,
                    ),
                )

        threading.Thread(
            target=worker,
            daemon=True,
        ).start()

    # --------------------------------------------------------
    # UPDATE ALL
    # --------------------------------------------------------

    def update_all(self):
        candidates = [
            d
            for d in self.drivers
            if d.is_outdated
        ]

        if not candidates:
            messagebox.showinfo(
                "Update Drivers",
                "No outdated drivers were found in the current catalog.",
                parent=self,
            )
            return

        verified = [
            d
            for d in candidates
            if d.can_auto_update
        ]

        manual = len(candidates) - len(verified)

        if not verified:
            messagebox.showinfo(
                "Update Drivers",
                (
                    f"{len(candidates)} driver(s) are marked outdated, "
                    "but none have a verified direct INF/CAB/ZIP package.\n\n"
                    "DriverFix will not execute vendor EXE installers automatically."
                ),
                parent=self,
            )
            return

        message = (
            f"DriverFix found {len(candidates)} outdated driver(s).\n\n"
            f"Automatically installable: {len(verified)}\n"
            f"Manual/vendor-source required: {manual}\n\n"
            "A driver backup will be created before changes."
        )

        if not messagebox.askyesno(
            "Update All",
            message,
            parent=self,
        ):
            return

        self._run_update_operation(
            verified
        )

    # --------------------------------------------------------
    # FIX SELECTED
    # --------------------------------------------------------

    def fix_selected(self):
        driver = self.get_selected_driver()

        if not driver:
            messagebox.showinfo(
                "Fix Problem",
                "Select a problem device first.",
                parent=self,
            )
            return

        self.fix_driver(
            driver
        )

    def fix_driver(
        self,
        driver: Driver,
    ):
        if not is_admin():
            messagebox.showwarning(
                "Administrator Required",
                "Repairing devices requires Administrator privileges.",
                parent=self,
            )
            return

        if not driver.has_problem:
            messagebox.showinfo(
                "DriverFix",
                "This device is not currently reporting a problem.",
                parent=self,
            )
            return

        if self.settings.get(
            "confirm_changes",
            True,
        ):

            if not messagebox.askyesno(
                "Fix Problem",
                (
                    f"DriverFix will attempt a safe repair for:\n\n"
                    f"{driver.device_name}\n\n"
                    f"Problem code: {driver.problem_code}\n\n"
                    "The operation will rescan the hardware and restart "
                    "the affected device. A backup will be created first."
                ),
                parent=self,
            ):
                return

        self._run_fix_operation(
            [driver]
        )

    def _run_fix_operation(
        self,
        drivers: list[Driver],
    ):

        progress_window = ProgressWindow(
            self,
            "Repairing Devices",
        )

        def worker():

            results = []

            try:
                if self.settings.get(
                    "backup_before_changes",
                    True,
                ):

                    progress_window.set_status(
                        "Creating safety backup...",
                        5,
                    )

                    backup_path = (
                        BackupManager.create_backup(
                            lambda text:
                            progress_window.set_status(
                                text
                            )
                        )
                    )

                    progress_window.write(
                        f"Backup created: {backup_path}"
                    )

                total = len(drivers)

                for index, driver in enumerate(
                    drivers,
                    start=1,
                ):

                    progress_window.set_status(
                        f"Repairing {driver.device_name}...",
                        10 + (
                            index - 1
                        ) / total * 70,
                    )

                    progress_window.write(
                        f"Rescanning device: "
                        f"{driver.device_name}"
                    )

                    scan_success, scan_output = (
                        DriverOperations.scan_devices()
                    )

                    progress_window.write(
                        "Device scan completed."
                        if scan_success
                        else "Device scan reported an error."
                    )

                    restart_success, restart_output = (
                        DriverOperations.restart_device(
                            driver.device_id
                        )
                    )

                    if restart_success:
                        results.append(
                            (
                                driver,
                                True,
                                restart_output,
                            )
                        )

                        progress_window.write(
                            "Device restarted successfully."
                        )

                    else:
                        results.append(
                            (
                                driver,
                                False,
                                restart_output,
                            )
                        )

                        progress_window.write(
                            "Device restart failed."
                        )

                progress_window.set_status(
                    "Final device rescan...",
                    92,
                )

                DriverOperations.scan_devices()

                progress_window.set_status(
                    "Repair operation complete.",
                    100,
                )

                self.after(
                    800,
                    lambda:
                    self._finish_operation(
                        progress_window,
                        results,
                        "Device Repair",
                    ),
                )

            except Exception as exc:

                self.after(
                    0,
                    lambda e=str(exc):
                    self._operation_error(
                        progress_window,
                        e,
                    ),
                )

        threading.Thread(
            target=worker,
            daemon=True,
        ).start()

    # --------------------------------------------------------
    # FIX ALL
    # --------------------------------------------------------

    def fix_all_problems(self):
        problems = [
            d
            for d in self.drivers
            if d.has_problem
        ]

        if not problems:
            messagebox.showinfo(
                "Fix Problems",
                "No problem devices were detected.",
                parent=self,
            )
            return

        if not is_admin():
            messagebox.showwarning(
                "Administrator Required",
                "Run DriverFix as Administrator to repair devices.",
                parent=self,
            )
            return

        if not messagebox.askyesno(
            "Fix All Problems",
            (
                f"DriverFix found {len(problems)} problem device(s).\n\n"
                "It will create a driver backup, rescan the hardware, "
                "and attempt to restart each affected device.\n\n"
                "Continue?"
            ),
            parent=self,
        ):
            return

        self._run_fix_operation(
            problems
        )

    # --------------------------------------------------------
    # OPERATION RESULT
    # --------------------------------------------------------

    def _finish_operation(
        self,
        progress_window,
        results,
        title,
    ):
        try:
            progress_window.finish()
        except Exception:
            pass

        success = sum(
            1
            for _, ok, _ in results
            if ok
        )

        failed = len(results) - success

        self.start_scan()

        summary = (
            f"{title} complete.\n\n"
            f"Successful: {success}\n"
            f"Failed/skipped: {failed}"
        )

        if failed:
            details = []

            for driver, ok, output in results:
                if not ok:
                    details.append(
                        f"{driver.device_name}: {output}"
                    )

            if details:
                summary += (
                    "\n\nDetails:\n"
                    + "\n".join(details[:8])
                )

        messagebox.showinfo(
            title,
            summary,
            parent=self,
        )

    def _operation_error(
        self,
        progress_window,
        error,
    ):
        try:
            progress_window.finish()
        except Exception:
            pass

        log(
            f"Operation failed: {error}"
        )

        messagebox.showerror(
            "DriverFix",
            error,
            parent=self,
        )

    def _show_no_package_source(
        self,
        driver,
    ):
        messagebox.showinfo(
            "No Verified Package",
            (
                f"No verified direct driver package is available for:\n\n"
                f"{driver.device_name}\n\n"
                "DriverFix can still show the catalog source when one exists, "
                "but it will not download or execute an unknown installer."
            ),
            parent=self,
        )

    # --------------------------------------------------------
    # BACKUP / RESTORE
    # --------------------------------------------------------

    def backup_drivers(self):
        if not is_admin():
            messagebox.showwarning(
                "Administrator Required",
                (
                    "Creating a complete driver backup requires "
                    "Administrator privileges."
                ),
                parent=self,
            )
            return

        progress_window = ProgressWindow(
            self,
            "Driver Backup",
        )

        def worker():
            try:
                backup = (
                    BackupManager.create_backup(
                        lambda text:
                        progress_window.set_status(
                            text
                        )
                    )
                )

                progress_window.write(
                    f"Backup created at:\n{backup}"
                )

                self.after(
                    800,
                    lambda: self._backup_finished(
                        progress_window,
                        backup,
                    ),
                )

            except Exception as exc:

                self.after(
                    0,
                    lambda e=str(exc):
                    self._operation_error(
                        progress_window,
                        e,
                    ),
                )

        threading.Thread(
            target=worker,
            daemon=True,
        ).start()

    def _backup_finished(
        self,
        progress_window,
        path,
    ):
        progress_window.finish()

        messagebox.showinfo(
            "Backup Complete",
            (
                "Driver backup created successfully.\n\n"
                f"{path}"
            ),
            parent=self,
        )

    def restore_drivers(self):
        if not is_admin():
            messagebox.showwarning(
                "Administrator Required",
                "Driver restoration requires Administrator privileges.",
                parent=self,
            )
            return

        path = filedialog.askdirectory(
            parent=self,
            title="Select DriverFix Backup",
            initialdir=str(
                BACKUPS_DIR
            ),
        )

        if not path:
            return

        backup_path = Path(
            path
        )

        if not list(
            backup_path.rglob("*.inf")
        ):
            messagebox.showerror(
                "Invalid Backup",
                "No INF driver packages were found in this directory.",
                parent=self,
            )
            return

        if not messagebox.askyesno(
            "Restore Drivers",
            (
                "Restore the driver packages from this backup?\n\n"
                "Existing matching devices may be updated."
            ),
            parent=self,
        ):
            return

        progress_window = ProgressWindow(
            self,
            "Restore Drivers",
        )

        def worker():
            try:
                success, total = (
                    BackupManager.restore_backup(
                        backup_path,
                        lambda text:
                        progress_window.set_status(
                            text
                        ),
                    )
                )

                progress_window.set_status(
                    "Restore complete.",
                    100,
                )

                self.after(
                    800,
                    lambda:
                    self._restore_finished(
                        progress_window,
                        success,
                        total,
                    ),
                )

            except Exception as exc:
                self.after(
                    0,
                    lambda e=str(exc):
                    self._operation_error(
                        progress_window,
                        e,
                    ),
                )

        threading.Thread(
            target=worker,
            daemon=True,
        ).start()

    def _restore_finished(
        self,
        progress_window,
        success,
        total,
    ):
        progress_window.finish()

        messagebox.showinfo(
            "Restore Complete",
            (
                f"Restore finished.\n\n"
                f"Successful packages: {success}\n"
                f"Total packages: {total}"
            ),
            parent=self,
        )

        self.start_scan()

    # --------------------------------------------------------
    # DEVICE MANAGER
    # --------------------------------------------------------

    def open_device_manager(self):
        try:
            subprocess.Popen(
                [
                    "devmgmt.msc"
                ]
            )
        except Exception as exc:
            messagebox.showerror(
                "Device Manager",
                str(exc),
                parent=self,
            )

    # --------------------------------------------------------
    # REPORT
    # --------------------------------------------------------

    def export_report(self):
        if not self.drivers:
            messagebox.showinfo(
                "Export Report",
                "Scan the system before exporting a report.",
                parent=self,
            )
            return

        path = filedialog.asksaveasfilename(
            parent=self,
            title="Export Driver Report",
            defaultextension=".csv",
            filetypes=[
                ("CSV files", "*.csv"),
                ("JSON files", "*.json"),
                ("Text files", "*.txt"),
            ],
        )

        if not path:
            return

        extension = Path(
            path
        ).suffix.lower()

        try:
            if extension == ".json":

                with open(
                    path,
                    "w",
                    encoding="utf-8",
                ) as f:

                    json.dump(
                        [
                            asdict(driver)
                            for driver in self.drivers
                        ],
                        f,
                        indent=4,
                        ensure_ascii=False,
                    )

            elif extension == ".txt":

                with open(
                    path,
                    "w",
                    encoding="utf-8",
                ) as f:

                    for driver in self.drivers:
                        f.write(
                            "=" * 70
                            + "\n"
                        )

                        for key, value in asdict(
                            driver
                        ).items():

                            f.write(
                                f"{key}: {value}\n"
                            )

            else:

                fields = list(
                    asdict(
                        self.drivers[0]
                    ).keys()
                )

                with open(
                    path,
                    "w",
                    encoding="utf-8-sig",
                    newline="",
                ) as f:

                    writer = csv.DictWriter(
                        f,
                        fieldnames=fields,
                    )

                    writer.writeheader()

                    for driver in self.drivers:
                        writer.writerow(
                            asdict(driver)
                        )

            log(
                f"Report exported: {path}"
            )

            messagebox.showinfo(
                "Report Exported",
                f"Report saved to:\n\n{path}",
                parent=self,
            )

        except Exception as exc:
            messagebox.showerror(
                "Export Error",
                str(exc),
                parent=self,
            )

    # --------------------------------------------------------
    # SETTINGS
    # --------------------------------------------------------

    def open_settings(self):
        window = tk.Toplevel(
            self
        )

        window.title(
            "DriverFix Settings"
        )

        window.geometry(
            "520x500"
        )

        window.configure(
            bg=BG
        )

        variables = {}

        options = [
            (
                "hide_duplicates",
                "Hide duplicate driver entries",
            ),
            (
                "show_signed",
                "Show signed drivers",
            ),
            (
                "show_problem_devices",
                "Show problem devices",
            ),
            (
                "auto_save_reports",
                "Automatically save reports",
            ),
            (
                "confirm_changes",
                "Confirm before driver changes",
            ),
            (
                "backup_before_changes",
                "Create backup before changes",
            ),
        ]

        tk.Label(
            window,
            text="Settings",
            bg=BG,
            fg=TEXT,
            font=(FONT, 22, "bold"),
        ).pack(
            anchor="w",
            padx=25,
            pady=(25, 5),
        )

        tk.Label(
            window,
            text="DriverFix behavior",
            bg=BG,
            fg=MUTED,
            font=(FONT, 10),
        ).pack(
            anchor="w",
            padx=25,
            pady=(0, 20),
        )

        container = tk.Frame(
            window,
            bg=PANEL,
        )

        container.pack(
            fill="both",
            expand=True,
            padx=25,
        )

        for key, label in options:
            variable = tk.BooleanVar(
                value=bool(
                    self.settings.get(
                        key,
                        False,
                    )
                )
            )

            variables[key] = variable

            tk.Checkbutton(
                container,
                text=label,
                variable=variable,
                bg=PANEL,
                fg=TEXT,
                selectcolor=PANEL_3,
                activebackground=PANEL,
                activeforeground=TEXT,
                font=(FONT, 10),
                anchor="w",
            ).pack(
                fill="x",
                padx=18,
                pady=9,
            )

        bottom = tk.Frame(
            window,
            bg=BG,
        )

        bottom.pack(
            fill="x",
            padx=25,
            pady=20,
        )

        def save():
            for key, variable in variables.items():
                self.settings[key] = variable.get()

            save_settings(
                self.settings
            )

            window.destroy()

        self._button(
            bottom,
            "Save Settings",
            save,
            bg=ACCENT,
        ).pack(
            side="right"
        )

        self._button(
            bottom,
            "Cancel",
            window.destroy,
        ).pack(
            side="right",
            padx=(0, 8),
        )


# ============================================================
# APPLICATION START
# ============================================================

def main():
    if platform.system() != "Windows":
        print(
            "DriverFix requires Windows."
        )
        return

    app = DriverFixApp()

    app.mainloop()


if __name__ == "__main__":
    main()