import json
import platform
import re
import subprocess
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DATABASE_FILE = BASE_DIR / "driver_database.json"


def is_windows():
    return platform.system().lower() == "windows"


def run_powershell(script):
    if not is_windows():
        return ""

    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

        if result.returncode != 0:
            return ""

        return result.stdout.strip()

    except Exception:
        return ""


def load_driver_database():
    if not DATABASE_FILE.exists():
        return {}

    try:
        with open(
            DATABASE_FILE,
            "r",
            encoding="utf-8",
        ) as file:
            return json.load(file)

    except Exception:
        return {}


def normalize_version(version):
    """
    Convert common Windows driver versions into
    comparable integer tuples.

    Example:
        31.0.15.5222
        -> (31, 0, 15, 5222)
    """

    if not version:
        return ()

    numbers = re.findall(
        r"\d+",
        str(version),
    )

    return tuple(
        int(number)
        for number in numbers
    )


def compare_versions(installed, available):
    installed_version = normalize_version(
        installed
    )

    available_version = normalize_version(
        available
    )

    if not installed_version or not available_version:
        return 0

    length = max(
        len(installed_version),
        len(available_version),
    )

    installed_version += (0,) * (
        length - len(installed_version)
    )

    available_version += (0,) * (
        length - len(available_version)
    )

    if available_version > installed_version:
        return 1

    if available_version < installed_version:
        return -1

    return 0


def categorize_driver(
    name,
    device_id="",
    manufacturer="",
):
    text = (
        f"{name} "
        f"{device_id} "
        f"{manufacturer}"
    ).lower()

    categories = {
        "Display": [
            "display",
            "graphics",
            "nvidia",
            "geforce",
            "radeon",
            "video controller",
        ],

        "Audio": [
            "audio",
            "sound",
            "realtek",
            "speakers",
            "microphone",
        ],

        "Network": [
            "network",
            "ethernet",
            "wireless",
            "wi-fi",
            "wifi",
            "802.11",
            "lan",
        ],

        "Bluetooth": [
            "bluetooth",
        ],

        "USB": [
            "usb",
            "universal serial bus",
            "xhci",
            "ehci",
        ],

        "Storage": [
            "storage",
            "nvme",
            "sata",
            "ahci",
            "raid",
            "scsi",
        ],

        "Input": [
            "keyboard",
            "mouse",
            "touchpad",
            "hid",
            "human interface",
        ],

        "Camera": [
            "camera",
            "webcam",
            "imaging",
        ],

        "Printer": [
            "printer",
        ],
    }

    for category, keywords in categories.items():
        if any(
            keyword in text
            for keyword in keywords
        ):
            return category

    return "Other"


def get_problem_devices():
    script = r"""
$ErrorActionPreference = 'SilentlyContinue'

Get-CimInstance Win32_PnPEntity |
Where-Object {
    $_.ConfigManagerErrorCode -and
    $_.ConfigManagerErrorCode -ne 0
} |
Select-Object `
    Name,
    DeviceID,
    ConfigManagerErrorCode |
ConvertTo-Json -Compress
"""

    output = run_powershell(script)

    if not output:
        return {}

    try:
        data = json.loads(output)

        if isinstance(data, dict):
            data = [data]

        result = {}

        for device in data:
            device_id = str(
                device.get("DeviceID", "")
            ).lower()

            result[device_id] = {
                "name": device.get(
                    "Name",
                    "Unknown device",
                ),
                "code": device.get(
                    "ConfigManagerErrorCode"
                ),
            }

        return result

    except Exception:
        return {}


def scan_drivers():
    """
    Scan the actual Windows driver inventory.

    Returns a list of driver dictionaries.
    """

    if not is_windows():
        return []

    database = load_driver_database()
    problems = get_problem_devices()

    script = r"""
$ErrorActionPreference = 'SilentlyContinue'

Get-CimInstance Win32_PnPSignedDriver |
Where-Object {
    $_.DeviceName -and
    $_.DriverVersion
} |
Select-Object `
    DeviceName,
    Manufacturer,
    DriverVersion,
    DriverDate,
    InfName,
    DeviceID,
    IsSigned,
    Status,
    ClassGuid |
ConvertTo-Json -Depth 3 -Compress
"""

    output = run_powershell(script)

    if not output:
        return []

    try:
        data = json.loads(output)

        if isinstance(data, dict):
            data = [data]

    except Exception:
        return []

    drivers = []

    for item in data:

        name = str(
            item.get(
                "DeviceName",
                "Unknown device",
            )
        )

        manufacturer = str(
            item.get(
                "Manufacturer",
                "Unknown",
            )
        )

        version = str(
            item.get(
                "DriverVersion",
                "Unknown",
            )
        )

        device_id = str(
            item.get(
                "DeviceID",
                "",
            )
        )

        device_id_lower = device_id.lower()

        category = categorize_driver(
            name,
            device_id,
            manufacturer,
        )

        # ----------------------------------------------------
        # Problem detection
        # ----------------------------------------------------

        problem = (
            device_id_lower in problems
        )

        problem_code = None

        if problem:
            problem_code = problems[
                device_id_lower
            ].get("code")

        # ----------------------------------------------------
        # Database lookup
        # ----------------------------------------------------

        database_entry = None

        # First try exact hardware ID.
        if device_id in database:
            database_entry = database[
                device_id
            ]

        # Case-insensitive fallback.
        if database_entry is None:

            for key, value in database.items():

                if key.lower() == device_id_lower:
                    database_entry = value
                    break

        available_version = None
        download_url = None
        source = None

        if database_entry:

            available_version = (
                database_entry.get(
                    "latest_version"
                )
            )

            download_url = (
                database_entry.get(
                    "download_url"
                )
            )

            source = (
                database_entry.get(
                    "source"
                )
            )

        # ----------------------------------------------------
        # Actual outdated detection
        # ----------------------------------------------------

        update_available = False

        if (
            available_version
            and version != "Unknown"
        ):

            update_available = (
                compare_versions(
                    version,
                    available_version,
                ) < 0
            )

        if problem:
            display_status = "Problem"

        elif update_available:
            display_status = "Update available"

        elif available_version:
            display_status = "Up to date"

        elif not item.get("IsSigned"):
            display_status = "Unsigned"

        else:
            display_status = "Unknown"

        drivers.append(
            {
                "name": name,
                "manufacturer": manufacturer,
                "version": version,
                "date": str(
                    item.get(
                        "DriverDate",
                        "",
                    )
                ),
                "inf": str(
                    item.get(
                        "InfName",
                        "",
                    )
                ),
                "device_id": device_id,
                "signed": bool(
                    item.get(
                        "IsSigned",
                        False,
                    )
                ),
                "status": str(
                    item.get(
                        "Status",
                        "Unknown",
                    )
                ),
                "class_guid": str(
                    item.get(
                        "ClassGuid",
                        "",
                    )
                ),
                "category": category,

                "problem": problem,
                "problem_code": problem_code,

                "available_version":
                    available_version,

                "update_available":
                    update_available,

                "download_url":
                    download_url,

                "source":
                    source,

                "display_status":
                    display_status,
            }
        )

    return drivers