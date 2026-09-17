import re


PROFILE_SUS = "SUS"
PROFILE_EBF = "EBF"
PROFILE_UNKNOWN = "UNKNOWN"

_INSTITUTION_NAME_FIXES = {
    "École Française Internationale de Djedda": "École Française Internationale de Djeddah",
}

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

    def normalize_institution_name(value):
        return _INSTITUTION_NAME_FIXES.get(value, value)

    normalized["institution_context"] = [
        normalize_institution_name(value)
        for value in normalized["institution_context"]
    ]
    for detail in normalized["institution_details"]:
        if isinstance(detail, dict):
            detail["name"] = normalize_institution_name(detail.get("name"))

    for original_course in normalized.get("courses") or []:
        course = dict(original_course)
        course["institution"] = normalize_institution_name(course.get("institution"))
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
