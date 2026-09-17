import re


PROFILE_SUS = "SUS"
PROFILE_EBF = "EBF"
PROFILE_UNKNOWN = "UNKNOWN"

_PROFILE_PATTERN = re.compile(r"(?:^|[_.-])(SUS|EBF)(?:[_.-]|$)", re.IGNORECASE)


def profile_for_blob(blob_name):
    """Select a document profile from a filename token without using global state."""
    filename = str(blob_name or "").replace("\\", "/").rsplit("/", 1)[-1]
    match = _PROFILE_PATTERN.search(filename)
    return match.group(1).upper() if match else PROFILE_UNKNOWN


def normalize_profile_result(result, profile):
    """Apply deterministic profile rules after model extraction."""
    profile = str(profile or PROFILE_UNKNOWN).upper()
    normalized = dict(result or {})
    courses = []
    if profile == PROFILE_SUS:
        normalized["source_languages"] = normalized.get("source_languages") or ["English"]
        course_name_fixes = {"COUMONCORE ECONOMICS": "CONSUMER ECONOMICS"}
    elif profile == PROFILE_EBF:
        normalized["source_languages"] = normalized.get("source_languages") or ["French"]
        course_name_fixes = {"OPITION MATH": "OPTION MATH"}
    else:
        course_name_fixes = {}

    normalized["is_academic_record"] = bool(
        normalized.get("is_academic_record") or normalized.get("courses")
    )
    normalized["institution_context"] = normalized.get("institution_context") or []
    normalized["institution_details"] = normalized.get("institution_details") or []

    for original_course in normalized.get("courses") or []:
        course = dict(original_course)
        if course.get("course_name") in course_name_fixes:
            course["course_name"] = course_name_fixes[course["course_name"]]
        for key in (
            "institution", "year_or_session", "course_level", "course_name",
            "course_code", "grade", "notes", "period", "credits", "status",
            "parent_course_name",
        ):
            if course.get(key) is None:
                course[key] = ""
        courses.append(course)
    normalized["courses"] = courses
    return normalized
