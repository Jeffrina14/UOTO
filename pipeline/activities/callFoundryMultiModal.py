import azure.durable_functions as df
import base64
import json
import os
import re
import fitz
from pipelineUtils.prompts import load_prompts
from pipelineUtils.blob_functions import get_blob_content
from pipelineUtils.azure_openai import RequestTooLargeError, run_prompt
from pipelineUtils.transcript_parser import (
    IncompleteJSONError,
    parse_transcript_response,
)
from configuration import Configuration

name = "callAoaiMultiModal"
bp = df.Blueprint()
config = Configuration()

DOCUMENT_TYPES = {
        "transcript",
        "report",
        "letter",
        "certificate",
        "current-course-list",
        "predicted-results",
        "unknown",
}
PAGE_ROLES = {
        "academic-result-page",
        "current-course-page",
        "letter",
        "certificate",
        "grading-legend",
        "translation",
        "duplicate-translation",
        "blank-or-irrelevant",
        "unknown",
}

CLASSIFICATION_SYSTEM_PROMPT = """
Classify the supplied scanned document page images for academic extraction. Images are
the authoritative source. Do not extract course rows and do not infer missing codes,
levels, grades, credits, compulsory status, institutions, or jurisdiction-specific
meaning. Do not apply Ontario-specific rules. Do not skip original-language or
translation pages. Mark a duplicate-translation role only when there is strong visual
evidence that the page is an official duplicate of another page.

Return strict JSON only with exactly these fields:
{
    "document_type": "transcript|report|letter|certificate|current-course-list|predicted-results|unknown",
    "jurisdiction": null,
    "source_languages": [],
    "institution_context": [],
    "issue_date": null,
    "official_translation_detected": false,
    "contains_results": false,
    "has_course_table": false,
    "is_translation": false,
    "page_roles": []
}

page_roles must contain one role per supplied image, using only:
academic-result-page, current-course-page, letter, certificate, grading-legend,
translation, duplicate-translation, blank-or-irrelevant, unknown.
""".strip()


def _pages_per_request():
    value = config.get_value("MULTIMODAL_PAGES_PER_REQUEST", "2")
    try:
        pages_per_request = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("MULTIMODAL_PAGES_PER_REQUEST must be a positive integer") from error
    if pages_per_request < 1:
        raise ValueError("MULTIMODAL_PAGES_PER_REQUEST must be a positive integer")
    return pages_per_request


def _verification_enabled():
    return str(
        config.get_value("TRANSCRIPT_VERIFICATION_ENABLED", "false")
    ).lower() == "true"


def _table_cropping_enabled():
    return str(
        config.get_value("MULTIMODAL_TABLE_CROPPING_ENABLED", "false")
    ).lower() == "true"


def _empty_document_classification():
    return {
        "document_type": "unknown",
        "jurisdiction": None,
        "source_languages": [],
        "institution_context": [],
        "issue_date": None,
        "official_translation_detected": False,
        "contains_results": False,
        "has_course_table": False,
        "is_translation": False,
        "page_roles": [],
    }


def _parse_document_classification(raw_result, page_count):
    try:
        parsed = json.loads(raw_result)
    except json.JSONDecodeError as error:
        raise ValueError("Document classification must be strict JSON") from error
    if not isinstance(parsed, dict):
        raise ValueError("Document classification must be a JSON object")

    classification = _empty_document_classification()
    classification.update({
        key: parsed[key]
        for key in classification
        if key in parsed
    })
    if classification["document_type"] not in DOCUMENT_TYPES:
        classification["document_type"] = "unknown"
    if not isinstance(classification["source_languages"], list):
        classification["source_languages"] = []
    if not isinstance(classification["institution_context"], list):
        classification["institution_context"] = []
    classification["institution_context"] = [
        item for item in classification["institution_context"]
        if isinstance(item, str) and item.strip()
    ]
    classification["page_roles"] = [
        role if role in PAGE_ROLES else "unknown"
        for role in classification["page_roles"]
    ] if isinstance(classification["page_roles"], list) else []
    if len(classification["page_roles"]) != page_count:
        classification["page_roles"] = ["unknown"] * page_count
    classification["contains_results"] = bool(classification["contains_results"])
    classification["has_course_table"] = bool(classification["has_course_table"])
    classification["is_translation"] = bool(classification["is_translation"])
    classification["official_translation_detected"] = bool(
        classification["official_translation_detected"]
    )
    return classification


def _classify_document_pages(base64_images, instance_id, model_runner=run_prompt):
    if not base64_images:
        return _empty_document_classification()
    try:
        raw_result = _run_unlogged_prompt(
            model_runner,
            instance_id,
            CLASSIFICATION_SYSTEM_PROMPT,
            "Classify these document images. Return strict JSON only.",
            base64_images=base64_images,
        )
        return _parse_document_classification(raw_result, len(base64_images))
    except RequestTooLargeError:
        if len(base64_images) == 1:
            raise
        midpoint = len(base64_images) // 2
        left = _classify_document_pages(base64_images[:midpoint], instance_id, model_runner)
        right = _classify_document_pages(base64_images[midpoint:], instance_id, model_runner)
        merged = _empty_document_classification()
        merged["page_roles"] = left["page_roles"] + right["page_roles"]
        merged["document_type"] = left["document_type"] if left["document_type"] != "unknown" else right["document_type"]
        merged["jurisdiction"] = left["jurisdiction"] or right["jurisdiction"]
        merged["source_languages"] = list(dict.fromkeys(
            left["source_languages"] + right["source_languages"]
        ))
        merged["institution_context"] = list(dict.fromkeys(
            left["institution_context"] + right["institution_context"]
        ))
        merged["issue_date"] = left["issue_date"] or right["issue_date"]
        merged["official_translation_detected"] = (
            left["official_translation_detected"]
            or right["official_translation_detected"]
        )
        merged["contains_results"] = left["contains_results"] or right["contains_results"]
        merged["has_course_table"] = left["has_course_table"] or right["has_course_table"]
        merged["is_translation"] = left["is_translation"] or right["is_translation"]
        return merged


def _format_extraction_context(classification):
    if not classification:
        return ""
    safe_context = {
        "document_type": classification.get("document_type", "unknown"),
        "jurisdiction": classification.get("jurisdiction"),
        "source_languages": classification.get("source_languages", []),
        "institution_context": classification.get("institution_context", []),
        "issue_date": classification.get("issue_date"),
        "official_translation_detected": bool(
            classification.get("official_translation_detected", False)
        ),
        "contains_results": bool(classification.get("contains_results", False)),
        "has_course_table": bool(classification.get("has_course_table", False)),
        "is_translation": bool(classification.get("is_translation", False)),
        "page_roles": classification.get("page_roles", []),
    }
    return (
        "\n\nDocument classification context (secondary guidance only; page images remain "
        "authoritative):\n"
        + json.dumps(safe_context, ensure_ascii=False)
        + "\nExtract only actual result rows from visible academic tables. Do not treat "
        "letters, legends, totals, explanatory prose, or course mentions outside "
        "result tables as course rows. Keep document_type format-neutral: transcript, "
        "report, letter, certificate, current-course-list, predicted-results, or "
        "unknown. Course codes, levels, grades, credits, compulsory markers, and "
        "sessions may legitimately be absent or use local formats; use null when not "
        "visible and do not infer them. Preserve multiple institutions at row level. "
        "Do not skip original or translation pages unless explicitly marked as "
        "confirmed duplicate translations. Preserve exact source formatting for "
        "course codes, grades, sessions, and notes, including spaces, punctuation, "
        "A-/A+/W/P, numeric or comma-decimal grades, and local session formats. "
        "Do not globally convert characters, dates, levels, grades, compulsory "
        "markers, codes, or translations. If official_translation_detected is true, "
        "use the confirmed official English page values while preserving all source "
        "languages; otherwise do not translate or replace original-language values."
    )


def _is_confident_ontario(classification):
    if not classification or classification.get("document_type") != "transcript":
        return False
    jurisdiction = str(classification.get("jurisdiction") or "").lower()
    return "ontario" in jurisdiction


def _normalize_ontario_notes(notes):
    if notes is None:
        return None
    parts = [part.strip() for part in str(notes).split(";")]
    kept = []
    compulsory = False
    for part in parts:
        marker = part.lower()
        if marker in {"x", "yes", "required"}:
            compulsory = True
        elif marker in {"", "no", "null", "none", "n/a", "na"}:
            continue
        else:
            kept.append(part)
    if compulsory:
        kept.append("Compulsory")
    return "; ".join(kept) or None


def _ontario_code_candidate(course):
    code = course.get("course_code")
    if not isinstance(code, str):
        return None
    raw_code = "".join(code.split()).upper()
    if len(raw_code) != 5:
        return None
    candidates = set()
    for index, character in enumerate(raw_code):
        replacements = {"I": "1", "O": "0"}.get(character)
        if replacements:
            candidate = raw_code[:index] + replacements + raw_code[index + 1:]
            if re.fullmatch(r"[A-Z]{3}[0-9][A-Z]", candidate):
                candidates.add(candidate)
    if len(candidates) != 1 or not course.get("course_name"):
        return None
    return candidates.pop()


def _normalize_transcript_result(result, classification):
    classification = classification or {}
    normalized_result = dict(result)
    if normalized_result.get("document_type") in (None, "unknown"):
        normalized_result["document_type"] = classification.get(
            "document_type", normalized_result.get("document_type")
        )
    if not normalized_result.get("source_languages"):
        normalized_result["source_languages"] = list(
            classification.get("source_languages", [])
        )
    if not normalized_result.get("document_issue_date"):
        normalized_result["document_issue_date"] = classification.get("issue_date")

    institutions = classification.get("institution_context", [])
    if len(institutions) == 1:
        normalized_result["courses"] = [
            dict(course, institution=course.get("institution") or institutions[0])
            for course in normalized_result.get("courses", [])
        ]

    if not _is_confident_ontario(classification):
        return normalized_result

    normalized_courses = []
    for original_course in normalized_result.get("courses", []):
        course = dict(original_course)
        candidate = _ontario_code_candidate(course)
        if candidate is not None:
            course["course_code"] = candidate
            if course.get("course_level") is None:
                course["course_level"] = candidate[3]
        course["notes"] = _normalize_ontario_notes(course.get("notes"))
        normalized_courses.append(course)
    normalized_result["courses"] = normalized_courses
    return normalized_result


def _polygon_to_render_rect(polygon, scale=2.0, points_per_unit=72.0):
    coordinates = list(polygon)
    if len(coordinates) < 8 or len(coordinates) % 2:
        raise ValueError("Layout polygon must contain four coordinate pairs")
    points = [
        (coordinates[index] * points_per_unit, coordinates[index + 1] * points_per_unit)
        for index in range(0, len(coordinates), 2)
    ]
    return fitz.Rect(
        min(point[0] for point in points) * scale,
        min(point[1] for point in points) * scale,
        max(point[0] for point in points) * scale,
        max(point[1] for point in points) * scale,
    )


def _layout_rows(layout_result):
    rows = []
    for table in getattr(layout_result, "tables", None) or []:
        grouped_cells = {}
        for cell in getattr(table, "cells", None) or []:
            row_index = getattr(cell, "row_index", None)
            for region in getattr(cell, "bounding_regions", None) or []:
                page_number = getattr(region, "page_number", None)
                polygon = getattr(region, "polygon", None)
                if row_index is not None and page_number and polygon:
                    grouped_cells.setdefault((page_number, row_index), []).append(polygon)

        for (page_number, row_index), polygons in grouped_cells.items():
            rows.append((page_number, row_index, polygons))

    return sorted(rows, key=lambda item: (item[0], item[1]))


def _layout_row_crops(blob_content):
    try:
        from azure.ai.documentintelligence import DocumentIntelligenceClient
        from azure.ai.documentintelligence.models import AnalyzeDocumentRequest

        client = DocumentIntelligenceClient(
            endpoint=config.get_value("AI_SERVICES_ENDPOINT"),
            credential=config.credential,
        )
        request = AnalyzeDocumentRequest(bytes_source=blob_content)
        layout_result = client.begin_analyze_document(
            "prebuilt-layout", request
        ).result()

        rows = _layout_rows(layout_result)
        if not rows:
            return []

        crops = []
        with fitz.open(stream=blob_content, filetype="pdf") as document:
            matrix = fitz.Matrix(2, 2)
            for page_number, row_index, polygons in rows:
                page = document[page_number - 1]
                rectangles = [
                    _polygon_to_render_rect(polygon)
                    for polygon in polygons
                ]
                crop = fitz.Rect(
                    min(rect.x0 for rect in rectangles),
                    min(rect.y0 for rect in rectangles),
                    max(rect.x1 for rect in rectangles),
                    max(rect.y1 for rect in rectangles),
                )
                pix = page.get_pixmap(matrix=matrix, clip=crop, alpha=False)
                crops.append(base64.b64encode(pix.tobytes("png")).decode("ascii"))
        return crops
    except Exception:
        return []


def _normalize_identity_value(value):
    if value is None:
        return None
    if isinstance(value, str):
        normalized = " ".join(value.split())
        if normalized.lower() in {"", "n/a", "na", "none", "null"}:
            return None
        return normalized
    return value


def _course_identity(course):
    return tuple(
        _normalize_identity_value(course.get(key))
        for key in (
            "institution",
            "year_or_session",
            "course_level",
            "course_name",
            "course_code",
            "grade",
            "notes",
        )
    )


def _merge_transcript_results(results):
    merged = {
        "student_name": None,
        "student_first_name": None,
        "student_last_name": None,
        "document_issue_date": None,
        "source_languages": [],
        "document_type": None,
        "is_academic_record": False,
        "courses": [],
    }

    seen_courses = set()
    for result in results:
        for key in (
            "student_name",
            "student_first_name",
            "student_last_name",
            "document_issue_date",
            "document_type",
        ):
            if merged[key] is None and result.get(key) is not None:
                merged[key] = result[key]

        for language in result.get("source_languages", []):
            if language not in merged["source_languages"]:
                merged["source_languages"].append(language)

        merged["is_academic_record"] = (
            merged["is_academic_record"] or result.get("is_academic_record", False)
        )
        for course in result.get("courses", []):
            identity = _course_identity(course)
            if identity not in seen_courses:
                seen_courses.add(identity)
                merged["courses"].append(course)

    return merged


def _run_unlogged_prompt(model_runner, *args, **kwargs):
    kwargs["log_interaction"] = False
    return model_runner(*args, **kwargs)


def _parse_verification_response(raw_result, expected_indexes):
    parsed_result = parse_transcript_response(raw_result)
    try:
        raw_result_object = json.loads(raw_result)
    except json.JSONDecodeError as error:
        raise ValueError("Verification response must be strict JSON") from error

    raw_courses = raw_result_object.get("courses")
    if not isinstance(raw_courses, list):
        raise ValueError("Verification response courses must be a JSON array")

    expected_indexes = set(expected_indexes)
    seen_indexes = set()
    indexed_courses = {}
    for course in raw_courses:
        if not isinstance(course, dict) or "row_index" not in course:
            raise ValueError("Verification response contains a course without row_index")
        row_index = course["row_index"]
        if not isinstance(row_index, int) or isinstance(row_index, bool):
            raise ValueError("Verification row_index must be an integer")
        if row_index in seen_indexes:
            raise ValueError(f"Verification response contains duplicate row_index {row_index}")
        if row_index not in expected_indexes:
            raise ValueError(f"Verification response contains unknown row_index {row_index}")
        seen_indexes.add(row_index)
        indexed_courses[row_index] = course

    if seen_indexes != expected_indexes:
        missing_indexes = sorted(expected_indexes - seen_indexes)
        raise ValueError(f"Verification response is missing row indexes {missing_indexes}")

    normalized_by_index = {
        row_index: parsed_result["courses"][position]
        for position, row_index in enumerate(course["row_index"] for course in raw_courses)
    }
    return normalized_by_index


def _verify_chunk(
    base64_images,
    first_pass_courses,
    instance_id,
    system_prompt,
    model_runner=run_prompt,
):
    verification_prompt = (
        "Verify only the course fields in the supplied first-pass rows against the "
        "document page images. Re-read every row directly from the images, horizontally "
        "from left to right, using the printed table columns. Treat course codes as "
        "visual text and distinguish 1 from I and 0 from O. Never infer or repair a "
        "course code from a known pattern; use null for any unclear code or field. "
        "Preserve the exact printed session and course level. Keep only the grade in "
        "grade, and keep credits, compulsory markers, and printed notes in notes. "
        "Return strict JSON only, with the same transcript schema. Preserve each "
        "row_index unchanged and include every supplied row exactly once."
        "\n\nFirst-pass rows:\n"
        + json.dumps({"courses": first_pass_courses}, ensure_ascii=False)
    )
    raw_result = _run_unlogged_prompt(
        model_runner,
        instance_id,
        system_prompt,
        verification_prompt,
        base64_images=base64_images,
    )
    return _parse_verification_response(
        raw_result,
        [course["row_index"] for course in first_pass_courses],
    )


def _extract_chunk_results(
    base64_images,
    page_numbers,
    instance_id,
    system_prompt,
    user_prompt,
    document_context,
    classification,
    model_runner,
    parser,
):
    chunk_prompt = (
        f"{user_prompt}\n\n"
        f"This request contains document pages {', '.join(map(str, page_numbers))}. "
        "Use these page numbers when preserving result distinctions."
        f"{document_context}"
    )

    try:
        raw_result = _run_unlogged_prompt(
            model_runner,
            instance_id,
            system_prompt,
            chunk_prompt,
            base64_images=base64_images,
        )
    except RequestTooLargeError:
        if len(base64_images) == 1:
            raise
        midpoint = len(base64_images) // 2
        return _extract_chunk_results(
            base64_images[:midpoint],
            page_numbers[:midpoint],
            instance_id,
            system_prompt,
            user_prompt,
            document_context,
            classification,
            model_runner,
            parser,
        ) + _extract_chunk_results(
            base64_images[midpoint:],
            page_numbers[midpoint:],
            instance_id,
            system_prompt,
            user_prompt,
            document_context,
            classification,
            model_runner,
            parser,
        )

    try:
        parsed_result = parser(raw_result)
    except IncompleteJSONError:
        retry_prompt = (
            "Return exactly one complete strict JSON object matching the "
            "transcript schema. Do not include Markdown, explanations, or any "
            "text outside the JSON object. Do not omit or partially return data."
        )
        try:
            retry_result = _run_unlogged_prompt(
                model_runner,
                instance_id,
                system_prompt,
                retry_prompt,
                base64_images=base64_images,
            )
        except RequestTooLargeError:
            if len(base64_images) == 1:
                raise
            midpoint = len(base64_images) // 2
            return _extract_chunk_results(
                base64_images[:midpoint],
                page_numbers[:midpoint],
                instance_id,
                system_prompt,
                user_prompt,
                document_context,
                classification,
                model_runner,
                parser,
            ) + _extract_chunk_results(
                base64_images[midpoint:],
                page_numbers[midpoint:],
                instance_id,
                system_prompt,
                user_prompt,
                document_context,
                classification,
                model_runner,
                parser,
            )
        parsed_result = parser(retry_result)

    return [(base64_images, page_numbers, parsed_result)]


def _extract_chunked_transcript(
    base64_images,
    instance_id,
    system_prompt,
    user_prompt,
    pages_per_request,
    document_context="",
    classification=None,
    model_runner=run_prompt,
    parser=parse_transcript_response,
    verify=False,
):
    chunk_results = []
    next_row_index = 0
    for start in range(0, len(base64_images), pages_per_request):
        end = min(start + pages_per_request, len(base64_images))
        page_numbers = list(range(start + 1, end + 1))
        extracted_results = _extract_chunk_results(
            base64_images[start:end],
            page_numbers,
            instance_id,
            system_prompt,
            user_prompt,
            document_context,
            classification,
            model_runner,
            parser,
        )
        for chunk_images, _, first_pass_result in extracted_results:
            first_pass_result = _normalize_transcript_result(
                first_pass_result,
                classification,
            )
            indexed_courses = []
            for course in first_pass_result["courses"]:
                indexed_course = dict(course)
                indexed_course["row_index"] = next_row_index
                next_row_index += 1
                indexed_courses.append(indexed_course)

            if verify and indexed_courses:
                verified_courses = _verify_chunk(
                    chunk_images,
                    indexed_courses,
                    instance_id,
                    system_prompt,
                    model_runner=model_runner,
                )
                for indexed_course in indexed_courses:
                    verified_course = verified_courses[indexed_course["row_index"]]
                    for key in (
                        "year_or_session",
                        "course_level",
                        "course_name",
                        "course_code",
                        "grade",
                        "notes",
                    ):
                        indexed_course[key] = verified_course[key]

            first_pass_result["courses"] = [
                {key: course[key] for key in (
                    "institution",
                    "year_or_session",
                    "course_level",
                    "course_name",
                    "course_code",
                    "grade",
                    "notes",
                )}
                for course in indexed_courses
            ]
            chunk_results.append(first_pass_result)

    return _merge_transcript_results(chunk_results)


def convert_to_base64_images(blob_input: dict, blob_content: bytes):
    """Convert a PDF or raster image blob to base64-encoded PNG images."""
    blob_name = blob_input.get("name")
    extension = os.path.splitext(blob_name or "")[1].lower()

    if extension == ".pdf":
        base64_images = []
        matrix = fitz.Matrix(2, 2)
        with fitz.open(stream=blob_content, filetype="pdf") as document:
            for page in document:
                pix = page.get_pixmap(matrix=matrix, alpha=False)
                image_bytes = pix.tobytes("png")
                base64_images.append(base64.b64encode(image_bytes).decode("ascii"))
        return base64_images

    if extension in {".png", ".jpg", ".jpeg", ".tiff", ".bmp"}:
        return [base64.b64encode(blob_content).decode("ascii")]

    raise ValueError(
        f"Unsupported multimodal blob extension '{extension or '<none>'}'. "
        "Supported extensions are PDF, PNG, JPG, JPEG, TIFF, and BMP."
    )

@bp.function_name(name)
@bp.activity_trigger(input_name="blob_input")
def run(blob_input: dict):
    # Parse args
    blob_name = blob_input.get("name")
    container = blob_input.get('container')
    instance_id = blob_input.get('instance_id', '')

    blob_content = get_blob_content(container_name=container, blob_path=blob_name)
    base64_images = convert_to_base64_images(blob_input, blob_content)

    prompt_json = load_prompts()
    classification = _classify_document_pages(base64_images, instance_id)
    document_context = _format_extraction_context(classification)
    user_prompt = prompt_json["user_prompt"]
    if (
        _table_cropping_enabled()
        and os.path.splitext(blob_name or "")[1].lower() == ".pdf"
    ):
        cropped_images = _layout_row_crops(blob_content)
        if cropped_images:
            base64_images = cropped_images
            user_prompt = (
                f"{user_prompt}\n\n"
                "Each supplied image is an ordered crop of a transcript table row. "
                "Read columns left to right as date/session, level, course name, "
                "course code, grade, credit, compulsory marker, and notes."
            )
    merged_result = _extract_chunked_transcript(
        base64_images,
        instance_id,
        prompt_json["system_prompt"],
        user_prompt,
        _pages_per_request(),
        document_context=document_context,
        classification=classification,
        verify=_verification_enabled(),
    )

    return json.dumps(merged_result, ensure_ascii=False)