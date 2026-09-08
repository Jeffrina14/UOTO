"""Optional, privacy-safe accreditation research post-processing helpers."""

import os
from typing import Callable, Iterable

STATUSES = {"found_in_directory", "accredited_other_source", "not_found", "unverified"}
DISCLAIMER = (
    "Research assistance only; not official verification and not an admissions decision."
)


def accreditation_enabled(environ=None):
    values = os.environ if environ is None else environ
    return str(values.get("ACCREDITATION_ENABLED", "false")).lower() == "true"


def _same_entity(institution, match):
    if not isinstance(match, dict):
        return False
    expected_name = str(institution.get("name") or "").strip().casefold()
    actual_name = str(match.get("name") or "").strip().casefold()
    if not expected_name or expected_name != actual_name:
        return False
    expected_location = str(
        institution.get("location") or institution.get("country") or ""
    ).strip().casefold()
    actual_location = str(
        match.get("location") or match.get("country") or ""
    ).strip().casefold()
    expected_website = str(institution.get("website") or "").strip().casefold()
    actual_website = str(match.get("website") or "").strip().casefold()
    if expected_location and actual_location and expected_location != actual_location:
        return False
    if expected_website and actual_website and expected_website != actual_website:
        return False
    return bool(
        (expected_location and actual_location)
        or (expected_website and actual_website)
    )


def evaluate_institution(institution, search: Callable[[dict], Iterable[dict]]):
    """Evaluate one institution using a privacy-safe search callback.

    The callback receives only name, location/country, website, and the literal
    accreditation query term. It must not receive transcript or student data.
    """
    query = {
        "name": institution.get("name"),
        "location": institution.get("location"),
        "country": institution.get("country"),
        "website": institution.get("website"),
        "query": "accreditation",
    }
    matches = list(search(query))
    same_entity_matches = [match for match in matches if _same_entity(institution, match)]
    if not same_entity_matches:
        status = "not_found" if not matches else "unverified"
    else:
        status = (
            "found_in_directory"
            if any(match.get("directory") for match in same_entity_matches)
            else "accredited_other_source"
        )
    return {
        "institution": institution.get("name"),
        "status": status if status in STATUSES else "unverified",
        "RECOGNIZED": status in {"found_in_directory", "accredited_other_source"},
        "CONTAINS_UNRECOGNIZED": status == "not_found",
        "NEEDS_REVIEW": status in {"unverified", "not_found"},
        "disclaimer": DISCLAIMER,
    }


def build_accreditation_summary(results):
    return {
        "ACCREDITATION_ENABLED": accreditation_enabled(),
        "results": list(results),
        "disclaimer": DISCLAIMER,
    }


def write_accreditation_excel(summary, output_path):
    """Write an optional workbook only when openpyxl is installed."""
    try:
        from openpyxl import Workbook
    except ImportError:
        return False
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["institution", "status", "RECOGNIZED", "CONTAINS_UNRECOGNIZED", "NEEDS_REVIEW"])
    for result in summary.get("results", []):
        sheet.append([
            result.get("institution"),
            result.get("status"),
            result.get("RECOGNIZED"),
            result.get("CONTAINS_UNRECOGNIZED"),
            result.get("NEEDS_REVIEW"),
        ])
    workbook.save(output_path)
    return True
