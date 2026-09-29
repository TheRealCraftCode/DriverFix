from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from collections import deque
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


# ============================================================
# DriverFix Internet Catalog Collector
# ============================================================
#
# PURPOSE
# -------
# Builds a metadata-only catalog of publicly listed drivers.
#
# IMPORTANT:
# This collector does NOT download or install driver packages.
# It stores metadata and official URLs only.
#
# SOURCES
# -------
# - Official manufacturer websites
# - Microsoft Update Catalog
#
# OUTPUT
# ------
# resources/internet_catalog.json
#
# ============================================================


BASE_DIR = Path(__file__).resolve().parent.parent
RESOURCES_DIR = BASE_DIR / "resources"

LOCAL_DATABASE = RESOURCES_DIR / "signature.json"
OUTPUT_DATABASE = RESOURCES_DIR / "internet_catalog.json"

USER_AGENT = (
    "DriverFix-DriverCatalog/1.0 "
    "(metadata collector; official-source indexing only)"
)

REQUEST_TIMEOUT = 20
REQUEST_DELAY = 0.75

DEFAULT_MAX_PAGES_PER_DOMAIN = 100
DEFAULT_MAX_CATALOG_RESULTS_PER_ID = 25

# ------------------------------------------------------------
# Official sources
# ------------------------------------------------------------

OFFICIAL_SOURCES = {
    "AMD": [
        "https://www.amd.com/en/support",
        "https://www.amd.com/en/support/download/drivers.html",
    ],

    "NVIDIA": [
        "https://www.nvidia.com/en-us/drivers/",
        "https://www.nvidia.com/en-us/drivers/drivers-by-industry/",
    ],

    "Intel": [
        "https://www.intel.com/content/www/us/en/download-center/home.html",
        "https://www.intel.com/content/www/us/en/download-center/home.html",
    ],

    "Realtek": [
        "https://www.realtek.com/Download/Index",
    ],

    "ASUS": [
        "https://www.asus.com/support/",
    ],

    "MSI": [
        "https://www.msi.com/support",
    ],

    "Gigabyte": [
        "https://www.gigabyte.com/Support",
    ],

    "Dell": [
        "https://www.dell.com/support/home/",
        "https://www.dell.com/support/kbdoc/",
    ],

    "HP": [
        "https://support.hp.com/drivers",
    ],

    "Lenovo": [
        "https://support.lenovo.com/",
    ],

    "Acer": [
        "https://www.acer.com/us-en/support/drivers-and-manuals",
    ],

    "Qualcomm": [
        "https://www.qualcomm.com/support",
    ],

    "Broadcom": [
        "https://www.broadcom.com/support/download-search",
    ],

    "MediaTek": [
        "https://www.mediatek.com/products",
    ],

    "Synaptics": [
        "https://www.synaptics.com/products",
    ],

    "Microsoft": [
        "https://www.catalog.update.microsoft.com/",
    ],
}


# Domains that the crawler is allowed to visit.
#
# We intentionally do not follow arbitrary external links.
ALLOWED_DOMAINS = {
    "amd.com",
    "www.amd.com",

    "nvidia.com",
    "www.nvidia.com",

    "intel.com",
    "www.intel.com",

    "realtek.com",
    "www.realtek.com",

    "asus.com",
    "www.asus.com",

    "msi.com",
    "www.msi.com",

    "gigabyte.com",
    "www.gigabyte.com",

    "dell.com",
    "www.dell.com",

    "hp.com",
    "support.hp.com",

    "lenovo.com",
    "support.lenovo.com",

    "acer.com",
    "www.acer.com",

    "qualcomm.com",
    "www.qualcomm.com",

    "broadcom.com",
    "www.broadcom.com",

    "mediatek.com",
    "www.mediatek.com",

    "synaptics.com",
    "www.synaptics.com",

    "microsoft.com",
    "www.microsoft.com",

    "catalog.update.microsoft.com",
}


# ------------------------------------------------------------
# Regexes
# ------------------------------------------------------------

VERSION_PATTERNS = [
    re.compile(
        r"\b(?:version|ver\.?|driver\s+version|package\s+version)"
        r"\s*[:#]?\s*([0-9]+(?:\.[0-9]+){1,5})\b",
        re.IGNORECASE,
    ),

    re.compile(
        r"\bv([0-9]+(?:\.[0-9]+){1,5})\b",
        re.IGNORECASE,
    ),

    re.compile(
        r"\b([0-9]{2,5}\.[0-9]{1,5}(?:\.[0-9]{1,5})?(?:\.[0-9]{1,5})?)\b"
    ),
]


DATE_PATTERNS = [
    re.compile(
        r"\b(20[0-9]{2}[-/](?:0[1-9]|1[0-2])[-/](?:0[1-9]|[12][0-9]|3[01]))\b"
    ),

    re.compile(
        r"\b((?:0?[1-9]|1[0-2])[-/](?:0?[1-9]|[12][0-9]|3[01])[-/]20[0-9]{2})\b"
    ),

    re.compile(
        r"\b((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*"
        r"\s+\d{1,2},\s+20\d{2})\b",
        re.IGNORECASE,
    ),
]


DRIVER_WORDS = (
    "driver",
    "drivers",
    "graphics",
    "chipset",
    "audio",
    "bluetooth",
    "wireless",
    "wifi",
    "wi-fi",
    "ethernet",
    "network",
    "display",
    "storage",
    "firmware",
    "controller",
    "card reader",
    "touchpad",
    "thunderbolt",
)


DOWNLOAD_EXTENSIONS = (
    ".exe",
    ".msi",
    ".cab",
    ".zip",
    ".inf",
)


# ------------------------------------------------------------
# Utility functions
# ------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_url(url: str) -> str:
    try:
        parsed = urlparse(url)

        # Remove fragments.
        parsed = parsed._replace(fragment="")

        # Remove common tracking parameters.
        query = []

        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            lowered = key.lower()

            if lowered.startswith("utm_"):
                continue

            if lowered in {
                "gclid",
                "fbclid",
                "mc_cid",
                "mc_eid",
            }:
                continue

            query.append((key, value))

        parsed = parsed._replace(
            query=urlencode(query, doseq=True)
        )

        return urlunparse(parsed)

    except Exception:
        return url


def domain_allowed(url: str) -> bool:
    try:
        hostname = (urlparse(url).hostname or "").lower()

        return (
            hostname in ALLOWED_DOMAINS
            or any(
                hostname.endswith("." + domain)
                for domain in ALLOWED_DOMAINS
                if not domain.startswith("www.")
            )
        )

    except Exception:
        return False


def clean_text(value: str) -> str:
    value = unescape(value)
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def strip_html(html: str) -> str:
    html = re.sub(
        r"<script\b[^>]*>.*?</script>",
        " ",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )

    html = re.sub(
        r"<style\b[^>]*>.*?</style>",
        " ",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )

    return clean_text(html)


def extract_title(html: str) -> str | None:
    match = re.search(
        r"<title\b[^>]*>(.*?)</title>",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if not match:
        return None

    title = clean_text(match.group(1))

    return title[:500] if title else None


def extract_versions(text: str) -> list[str]:
    found = []

    for pattern in VERSION_PATTERNS:
        for match in pattern.finditer(text):
            version = match.group(1).strip()

            if version not in found:
                found.append(version)

    return found[:20]


def extract_dates(text: str) -> list[str]:
    found = []

    for pattern in DATE_PATTERNS:
        for match in pattern.finditer(text):
            date = match.group(1).strip()

            if date not in found:
                found.append(date)

    return found[:20]


def looks_driver_related(text: str) -> bool:
    lowered = text.lower()

    return any(word in lowered for word in DRIVER_WORDS)


def make_id(*parts: str) -> str:
    raw = "|".join(
        str(part or "").strip().lower()
        for part in parts
    )

    return hashlib.sha256(
        raw.encode("utf-8", errors="ignore")
    ).hexdigest()[:32]


# ------------------------------------------------------------
# HTTP
# ------------------------------------------------------------

def fetch(url: str) -> tuple[str | None, str | None]:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": (
                "text/html,application/xhtml+xml,"
                "application/xml;q=0.9,*/*;q=0.8"
            ),
        },
    )

    try:
        with urlopen(
            request,
            timeout=REQUEST_TIMEOUT,
        ) as response:

            content_type = response.headers.get(
                "Content-Type",
                "",
            ).lower()

            if (
                "text/html" not in content_type
                and "xml" not in content_type
                and "text/plain" not in content_type
            ):
                return None, content_type

            data = response.read()

            if len(data) > 10 * 1024 * 1024:
                return None, content_type

            charset = response.headers.get_content_charset() or "utf-8"

            try:
                text = data.decode(charset, errors="replace")
            except Exception:
                text = data.decode("utf-8", errors="replace")

            return text, content_type

    except HTTPError as exc:
        print(f"      HTTP {exc.code}: {url}")
        return None, None

    except URLError as exc:
        print(f"      Network error: {url} ({exc.reason})")
        return None, None

    except Exception as exc:
        print(f"      Request failed: {url} ({exc})")
        return None, None


# ------------------------------------------------------------
# HTML link extraction
# ------------------------------------------------------------

def extract_links(html: str, base_url: str) -> list[str]:
    links = []

    pattern = re.compile(
        r"""(?:href|src)\s*=\s*["']([^"']+)["']""",
        re.IGNORECASE,
    )

    for match in pattern.finditer(html):
        raw = unescape(match.group(1).strip())

        if not raw:
            continue

        if raw.startswith(
            (
                "javascript:",
                "mailto:",
                "tel:",
                "#",
                "data:",
            )
        ):
            continue

        absolute = normalize_url(
            urljoin(base_url, raw)
        )

        parsed = urlparse(absolute)

        if parsed.scheme not in ("http", "https"):
            continue

        if not domain_allowed(absolute):
            continue

        if absolute not in links:
            links.append(absolute)

    return links


# ------------------------------------------------------------
# Local signature database
# ------------------------------------------------------------

def load_local_hardware_ids() -> list[str]:
    if not LOCAL_DATABASE.exists():
        print()
        print("[!] Local signature database not found:")
        print(f"    {LOCAL_DATABASE}")
        return []

    try:
        data = json.loads(
            LOCAL_DATABASE.read_text(
                encoding="utf-8"
            )
        )

    except Exception as exc:
        print(f"[!] Could not read signature.json: {exc}")
        return []

    result = set()

    for item in data.get("signatures", []):
        if not isinstance(item, dict):
            continue

        hardware_id = item.get("hardware_id")

        if hardware_id:
            result.add(
                str(hardware_id).strip()
            )

        for compatible in item.get("compatible_ids", []):
            if compatible:
                result.add(
                    str(compatible).strip()
                )

    return sorted(result)


# ------------------------------------------------------------
# Catalog database
# ------------------------------------------------------------

def load_catalog() -> dict:
    if not OUTPUT_DATABASE.exists():
        return {
            "schema_version": 1,
            "database_type": "internet_driver_catalog",
            "generated_at": None,
            "last_updated": None,
            "collector_version": "1.0",
            "entries": [],
        }

    try:
        data = json.loads(
            OUTPUT_DATABASE.read_text(
                encoding="utf-8"
            )
        )

        if not isinstance(data, dict):
            raise ValueError("Root is not an object")

        if not isinstance(data.get("entries"), list):
            data["entries"] = []

        return data

    except Exception as exc:
        print(f"[!] Existing catalog could not be loaded: {exc}")
        print("[!] Starting a fresh catalog.")

        return {
            "schema_version": 1,
            "database_type": "internet_driver_catalog",
            "generated_at": None,
            "last_updated": None,
            "collector_version": "1.0",
            "entries": [],
        }


def save_catalog(data: dict) -> None:
    RESOURCES_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    data["last_updated"] = now_iso()

    temp = OUTPUT_DATABASE.with_suffix(".tmp")

    temp.write_text(
        json.dumps(
            data,
            indent=4,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    temp.replace(OUTPUT_DATABASE)


def existing_entry_map(data: dict) -> dict:
    result = {}

    for entry in data.get("entries", []):
        if not isinstance(entry, dict):
            continue

        entry_id = entry.get("id")

        if entry_id:
            result[entry_id] = entry

    return result


# ------------------------------------------------------------
# Official website crawler
# ------------------------------------------------------------

def create_web_entry(
    vendor: str,
    url: str,
    html: str,
) -> dict:

    text = strip_html(html)

    title = extract_title(html)
    versions = extract_versions(text)
    dates = extract_dates(text)

    driver_related = looks_driver_related(
        f"{url} {title or ''} {text[:10000]}"
    )

    return {
        "id": make_id(
            vendor,
            url,
        ),

        "type": "official_web_source",

        "vendor": vendor,

        "title": title,

        "url": url,

        "versions": versions,

        "dates": dates,

        "driver_related": driver_related,

        "source": vendor,

        "source_type": "official_manufacturer",

        "collected_at": now_iso(),
    }


def crawl_vendor(
    vendor: str,
    start_urls: Iterable[str],
    max_pages: int,
    data: dict,
) -> int:

    print()
    print("=" * 64)
    print(f"COLLECTING: {vendor}")
    print("=" * 64)

    queue = deque()

    visited = set()

    for url in start_urls:
        if domain_allowed(url):
            queue.append(normalize_url(url))

    entries = existing_entry_map(data)

    collected = 0

    while queue and len(visited) < max_pages:
        url = queue.popleft()

        if url in visited:
            continue

        visited.add(url)

        print(
            f"  [{len(visited):>3}/{max_pages}] "
            f"{url}"
        )

        html, _ = fetch(url)

        if not html:
            continue

        entry = create_web_entry(
            vendor,
            url,
            html,
        )

        if entry["driver_related"]:
            entry_id = entry["id"]

            if entry_id not in entries:
                data["entries"].append(entry)
                entries[entry_id] = entry
                collected += 1

        for link in extract_links(
            html,
            url,
        ):

            if link in visited:
                continue

            # Avoid obvious binary downloads.
            lower = link.lower()

            if lower.endswith(
                DOWNLOAD_EXTENSIONS
            ):
                continue

            # Avoid huge query explosions.
            if len(link) > 1000:
                continue

            queue.append(link)

        time.sleep(REQUEST_DELAY)

    print(
        f"  Collected driver-related pages: {collected}"
    )

    return collected


# ------------------------------------------------------------
# Microsoft Update Catalog
# ------------------------------------------------------------

def catalog_search_url(query: str) -> str:
    return (
        "https://www.catalog.update.microsoft.com/"
        "Search.aspx?"
        + urlencode({"q": query})
    )


def parse_catalog_results(
    html: str,
    hardware_id: str,
    max_results: int,
) -> list[dict]:

    text = strip_html(html)

    results = []

    # The catalog page has historically exposed table-like
    # information in its HTML. We intentionally use flexible
    # extraction rather than relying on one exact HTML layout.

    title_candidates = re.findall(
        r"""(?:title|name|Description)[^>]*>
        \s*([^<]{5,300})
        <""",
        html,
        flags=re.IGNORECASE | re.VERBOSE,
    )

    versions = extract_versions(text)
    dates = extract_dates(text)

    combined_titles = []

    for title in title_candidates:
        title = clean_text(title)

        if title and title not in combined_titles:
            combined_titles.append(title)

    # If the HTML structure changes, still preserve the search
    # itself as a useful catalog record.
    if not combined_titles:
        combined_titles = [
            f"Microsoft Update Catalog result for {hardware_id}"
        ]

    for index, title in enumerate(
        combined_titles[:max_results]
    ):

        version = (
            versions[index]
            if index < len(versions)
            else None
        )

        release_date = (
            dates[index]
            if index < len(dates)
            else None
        )

        search_url = catalog_search_url(
            hardware_id
        )

        results.append(
            {
                "id": make_id(
                    "Microsoft Update Catalog",
                    hardware_id,
                    title,
                    version or "",
                    release_date or "",
                ),

                "type": "microsoft_update_catalog",

                "vendor": "Microsoft",

                "title": title,

                "hardware_id": hardware_id,

                "version": version,

                "release_date": release_date,

                "url": search_url,

                "source": "Microsoft Update Catalog",

                "source_type": "microsoft_catalog",

                "collected_at": now_iso(),
            }
        )

    return results


def query_microsoft_catalog(
    hardware_ids: list[str],
    max_results_per_id: int,
    data: dict,
) -> int:

    print()
    print("=" * 64)
    print("MICROSOFT UPDATE CATALOG")
    print("=" * 64)

    if not hardware_ids:
        print("  No hardware IDs available.")
        return 0

    entries = existing_entry_map(data)

    added = 0

    for index, hardware_id in enumerate(
        hardware_ids,
        start=1,
    ):

        print()
        print(
            f"  [{index}/{len(hardware_ids)}] "
            f"{hardware_id}"
        )

        url = catalog_search_url(
            hardware_id
        )

        html, _ = fetch(url)

        if not html:
            continue

        results = parse_catalog_results(
            html,
            hardware_id,
            max_results_per_id,
        )

        for entry in results:
            entry_id = entry["id"]

            if entry_id in entries:
                continue

            data["entries"].append(entry)
            entries[entry_id] = entry
            added += 1

        time.sleep(REQUEST_DELAY)

    return added


# ------------------------------------------------------------
# Statistics
# ------------------------------------------------------------

def print_statistics(data: dict) -> None:
    entries = data.get("entries", [])

    vendors = {}

    official_pages = 0
    microsoft_results = 0
    driver_related = 0

    for entry in entries:
        vendor = entry.get(
            "vendor",
            "Unknown",
        )

        vendors[vendor] = (
            vendors.get(vendor, 0) + 1
        )

        if entry.get("type") == "official_web_source":
            official_pages += 1

        if entry.get("type") == "microsoft_update_catalog":
            microsoft_results += 1

        if entry.get("driver_related"):
            driver_related += 1

    print()
    print("=" * 64)
    print("CATALOG STATISTICS")
    print("=" * 64)

    print(
        f"Total catalog entries:       {len(entries)}"
    )

    print(
        f"Official source pages:       {official_pages}"
    )

    print(
        f"Microsoft catalog entries:   {microsoft_results}"
    )

    print(
        f"Driver-related entries:      {driver_related}"
    )

    print()
    print("By vendor:")

    for vendor, count in sorted(
        vendors.items(),
        key=lambda item: (-item[1], item[0].lower()),
    ):
        print(
            f"  {vendor:<20} {count}"
        )


# ------------------------------------------------------------
# Main
# ------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "DriverFix Internet Driver Catalog Collector"
        )
    )

    parser.add_argument(
        "--pages",
        type=int,
        default=DEFAULT_MAX_PAGES_PER_DOMAIN,
        help=(
            "Maximum pages to crawl per vendor "
            f"(default: {DEFAULT_MAX_PAGES_PER_DOMAIN})"
        ),
    )

    parser.add_argument(
        "--catalog-results",
        type=int,
        default=DEFAULT_MAX_CATALOG_RESULTS_PER_ID,
        help=(
            "Maximum Microsoft Catalog records per hardware ID "
            f"(default: {DEFAULT_MAX_CATALOG_RESULTS_PER_ID})"
        ),
    )

    parser.add_argument(
        "--no-microsoft",
        action="store_true",
        help="Skip Microsoft Update Catalog queries.",
    )

    parser.add_argument(
        "--vendor",
        action="append",
        help=(
            "Only collect a specific vendor. "
            "Can be specified multiple times."
        ),
    )

    parser.add_argument(
        "--stats",
        action="store_true",
        help="Only display existing catalog statistics.",
    )

    args = parser.parse_args()

    print()
    print("=" * 64)
    print(" DRIVERFIX INTERNET DRIVER CATALOG COLLECTOR")
    print("=" * 64)
    print()
    print("Metadata-only collection.")
    print("Driver installers are NOT downloaded.")
    print()

    data = load_catalog()

    if not data.get("generated_at"):
        data["generated_at"] = now_iso()

    if args.stats:
        print_statistics(data)
        return 0

    selected_vendors = set(
        vendor.lower()
        for vendor in args.vendor
    ) if args.vendor else None

    total_added = 0

    # --------------------------------------------------------
    # Manufacturer websites
    # --------------------------------------------------------

    for vendor, urls in OFFICIAL_SOURCES.items():

        if vendor == "Microsoft":
            continue

        if selected_vendors:
            if vendor.lower() not in selected_vendors:
                continue

        added = crawl_vendor(
            vendor,
            urls,
            max(1, args.pages),
            data,
        )

        total_added += added

        # Save after every vendor.
        save_catalog(data)

    # --------------------------------------------------------
    # Microsoft Update Catalog
    # --------------------------------------------------------

    if not args.no_microsoft:

        hardware_ids = load_local_hardware_ids()

        print()
        print(
            f"Hardware IDs available for Microsoft lookup: "
            f"{len(hardware_ids)}"
        )

        added = query_microsoft_catalog(
            hardware_ids,
            max(1, args.catalog_results),
            data,
        )

        total_added += added

        save_catalog(data)

    # --------------------------------------------------------
    # Final save
    # --------------------------------------------------------

    save_catalog(data)

    print()
    print("=" * 64)
    print("COLLECTION COMPLETE")
    print("=" * 64)

    print(
        f"New entries this run: {total_added}"
    )

    print(
        f"Total entries:        "
        f"{len(data.get('entries', []))}"
    )

    print()
    print("Database:")
    print(f"  {OUTPUT_DATABASE}")

    print_statistics(data)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())

    except KeyboardInterrupt:
        print()
        print("[!] Collection cancelled by user.")
        raise SystemExit(130)

    except Exception as exc:
        print()
        print("=" * 64)
        print("FATAL ERROR")
        print("=" * 64)
        print(str(exc))
        raise SystemExit(1)