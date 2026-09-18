import json
import re
from typing import Any


COURSE_KEYS = (
    "institution",
    "year_or_session",
    "course_level",
    "course_name",
    "course_code",
    "grade",
    "notes",
)

EXTRA_COURSE_KEYS = (
    "period",
    "credits",
    "status",
    "parent_course_name",
    "reported_grades",
    "reported_credits",
    "reported_attributes",
)

EXTRA_TOP_LEVEL_KEYS = (
    "page_type",
    "fields",
    "totals",
    "other",
    "summaries",
    "academic_summary_rows",
    "document_fields",
    "other_information",
    "institution_context",
    "institution_details",
)

TOP_LEVEL_DEFAULTS = {
    "student_name": None,
    "student_first_name": None,
    "student_last_name": None,
    "document_issue_date": None,
    "source_languages": [],
    "document_type": None,
    "is_academic_record": False,
    "institution_context": [],
    "institution_details": [],
    "courses": [],
}

_NULL_LIKE_VALUES = {"", "n/a", "na", "none", "null"}


class IncompleteJSONError(ValueError):
    """Raised when a response ends before its outer JSON object is complete."""


def _remove_thinking_blocks(value: str) -> str:
    return re.sub(r"<think>.*?</think>", "", value, flags=re.IGNORECASE | re.DOTALL)


def _remove_json_fences(value: str) -> str:
    value = re.sub(r"```\s*json\s*", "", value, flags=re.IGNORECASE)
    return value.replace("```", "")


def _extract_outer_object(value: str) -> str:
    start = value.find("{")
    if start == -1:
        raise ValueError("Transcript response does not contain a JSON object")

    depth = 0
    in_string = False
    escaped = False

    for index in range(start, len(value)):
        character = value[index]

        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue

        if character == '"':
            in_string = True
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return value[start:index + 1]

    raise IncompleteJSONError(
        "Transcript response contains malformed JSON: incomplete object"
    )


def _remove_trailing_commas(value: str) -> str:
    result = []
    index = 0
    in_string = False
    escaped = False

    while index < len(value):
        character = value[index]

        if in_string:
            result.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            index += 1
            continue

        if character == '"':
            in_string = True
            result.append(character)
            index += 1
            continue

        if character == ",":
            lookahead = index + 1
            while lookahead < len(value) and value[lookahead].isspace():
                lookahead += 1
            if lookahead < len(value) and value[lookahead] in "}]":
                index += 1
                continue

        result.append(character)
        index += 1

    return "".join(result)


def _normalize_value(value: Any) -> Any:
    if isinstance(value, str) and value.strip().lower() in _NULL_LIKE_VALUES:
        return None
    return value


def _normalize_course(course: Any, index: int) -> dict:
    if not isinstance(course, dict):
        raise ValueError(f"Course row at index {index} must be a JSON object")

    normalized = {
        key: _normalize_value(course.get(key))
        for key in COURSE_KEYS
    }
    for key in EXTRA_COURSE_KEYS:
        if key in course:
            normalized[key] = course[key]
    return normalized


def parse_json_object(raw_response: str) -> dict:
    """Parse a model response into a JSON object, tolerating fences and thinking blocks."""
    if not isinstance(raw_response, str) or not raw_response.strip():
        raise ValueError("Model response cannot be empty")

    cleaned_response = _remove_thinking_blocks(raw_response)
    cleaned_response = _remove_json_fences(cleaned_response).strip()
    if not cleaned_response:
        raise ValueError("Model response cannot be empty after removing wrappers")

    json_text = _remove_trailing_commas(_extract_outer_object(cleaned_response))
    try:
        parsed = json.loads(json_text)
    except json.JSONDecodeError as error:
        raise ValueError(f"Model response contains malformed JSON: {error.msg}") from error

    if not isinstance(parsed, dict):
        raise ValueError("Model response top-level value must be a JSON object")

    return parsed


def parse_transcript_response(raw_response: str) -> dict:
    if not isinstance(raw_response, str) or not raw_response.strip():
        raise ValueError("Transcript response cannot be empty")

    cleaned_response = _remove_thinking_blocks(raw_response)
    cleaned_response = _remove_json_fences(cleaned_response).strip()
    if not cleaned_response:
        raise ValueError("Transcript response cannot be empty after removing wrappers")

    if cleaned_response.startswith("["):
        json_text = _remove_trailing_commas(cleaned_response)
    else:
        json_text = _remove_trailing_commas(_extract_outer_object(cleaned_response))
    try:
        parsed = json.loads(json_text)
    except json.JSONDecodeError as error:
        raise ValueError(f"Transcript response contains malformed JSON: {error.msg}") from error

    if not isinstance(parsed, dict):
        raise ValueError("Transcript response top-level value must be a JSON object")

    result = {
        key: parsed[key]
        for key in TOP_LEVEL_DEFAULTS
        if key in parsed
    }
    for key in EXTRA_TOP_LEVEL_KEYS:
        if key in parsed:
            result[key] = parsed[key]
    for key, default in TOP_LEVEL_DEFAULTS.items():
        result.setdefault(key, default.copy() if isinstance(default, list) else default)

    for key in (
        "student_name",
        "student_first_name",
        "student_last_name",
        "document_issue_date",
        "document_type",
    ):
        result[key] = _normalize_value(result[key])

    source_languages = result["source_languages"]
    if source_languages is None:
        result["source_languages"] = []
    elif not isinstance(source_languages, list):
        raise ValueError("source_languages must be a JSON array")
    else:
        result["source_languages"] = [
            _normalize_value(language) for language in source_languages
        ]

    institution_context = result["institution_context"]
    if institution_context is None:
        result["institution_context"] = []
    elif not isinstance(institution_context, list):
        raise ValueError("institution_context must be a JSON array")
    else:
        result["institution_context"] = [
            _normalize_value(institution) for institution in institution_context
            if isinstance(institution, str) and institution.strip()
        ]

    institution_details = result["institution_details"]
    if institution_details is None:
        result["institution_details"] = []
    elif not isinstance(institution_details, list):
        raise ValueError("institution_details must be a JSON array")
    else:
        result["institution_details"] = [
            {
                "name": _normalize_value(detail.get("name")) or "",
                "location": _normalize_value(detail.get("location")) or "",
                "country": _normalize_value(detail.get("country")) or "",
                "website": _normalize_value(detail.get("website")) or "",
            }
            for detail in institution_details
            if isinstance(detail, dict) and _normalize_value(detail.get("name"))
        ]

    courses = result["courses"]
    if not isinstance(courses, list):
        raise ValueError("courses must be a JSON array")
    result["courses"] = [
        _normalize_course(course, index)
        for index, course in enumerate(courses)
    ]

    return result
