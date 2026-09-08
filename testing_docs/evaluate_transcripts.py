"""Run the existing multimodal extractor against local PDF fixtures."""

import argparse
import base64
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

import fitz


COURSE_FIELDS = (
    "institution",
    "year_or_session",
    "course_level",
    "course_name",
    "course_code",
    "grade",
    "notes",
)
TOP_LEVEL_FIELDS = (
    "document_type",
    "is_academic_record",
    "source_languages",
)


def _normalize(value):
    if value is None:
        return None
    if isinstance(value, str):
        return " ".join(value.split()).lower() or None
    return value


def _course_key(course, index):
    code = _normalize(course.get("course_code"))
    name = _normalize(course.get("course_name"))
    return ("code", code) if code else ("index", index, name)


def compare_reference(expected, actual):
    expected_courses = expected.get("courses", [])
    actual_courses = actual.get("courses", [])
    expected_by_key = {
        _course_key(course, index): course
        for index, course in enumerate(expected_courses)
    }
    actual_by_key = {
        _course_key(course, index): course
        for index, course in enumerate(actual_courses)
    }

    missing = sorted(str(key) for key in expected_by_key.keys() - actual_by_key.keys())
    extra = sorted(str(key) for key in actual_by_key.keys() - expected_by_key.keys())
    mismatches = Counter()
    for key in expected_by_key.keys() & actual_by_key.keys():
        expected_course = expected_by_key[key]
        actual_course = actual_by_key[key]
        for field in COURSE_FIELDS:
            if _normalize(expected_course.get(field)) != _normalize(actual_course.get(field)):
                mismatches[field] += 1

    for field in TOP_LEVEL_FIELDS:
        if field == "source_languages":
            expected_value = [_normalize(item) for item in expected.get(field, [])]
            actual_value = [_normalize(item) for item in actual.get(field, [])]
        else:
            expected_value = _normalize(expected.get(field))
            actual_value = _normalize(actual.get(field))
        if expected_value != actual_value:
            mismatches[field] += 1

    return {
        "missing_rows": missing,
        "extra_rows": extra,
        "mismatch_counts": dict(mismatches),
        "expected_course_count": len(expected_courses),
        "actual_course_count": len(actual_courses),
    }


def _load_extractor():
    repository_root = Path(__file__).resolve().parents[1]
    pipeline_path = repository_root / "pipeline"
    production = None

    def load_production():
        nonlocal production
        if production is None:
            if str(pipeline_path) not in sys.path:
                sys.path.insert(0, str(pipeline_path))
            from activities import callFoundryMultiModal
            from pipelineUtils.azure_openai import run_prompt
            from pipelineUtils.prompts import load_prompts
            production = {
                "classify": callFoundryMultiModal._classify_document_pages,
                "extract": callFoundryMultiModal._extract_chunked_transcript,
                "format_context": callFoundryMultiModal._format_extraction_context,
                "pages_per_request": callFoundryMultiModal._pages_per_request,
                "run_prompt": run_prompt,
                "load_prompts": load_prompts,
            }
        return production

    def convert_to_base64_images(blob_input, blob_content):
        extension = os.path.splitext(blob_input.get("name", ""))[1].lower()
        if extension == ".pdf":
            images = []
            with fitz.open(stream=blob_content, filetype="pdf") as document:
                matrix = fitz.Matrix(2, 2)
                for page in document:
                    pix = page.get_pixmap(matrix=matrix, alpha=False)
                    images.append(base64.b64encode(pix.tobytes("png")).decode("ascii"))
            return images
        if extension in {".png", ".jpg", ".jpeg", ".tiff", ".bmp"}:
            return [base64.b64encode(blob_content).decode("ascii")]
        raise ValueError(f"Unsupported local evaluator extension: {extension or '<none>'}")

    def lazy_call(key):
        def call(*args, **kwargs):
            return load_production()[key](*args, **kwargs)
        return call

    return {
        "classify": lazy_call("classify"),
        "extract": lazy_call("extract"),
        "format_context": lazy_call("format_context"),
        "pages_per_request": lazy_call("pages_per_request"),
        "convert": convert_to_base64_images,
        "run_prompt": lazy_call("run_prompt"),
        "load_prompts": lazy_call("load_prompts"),
    }


def evaluate_pdf(pdf_path, result_path, extractor):
    started = time.perf_counter()
    try:
        blob_input = {"name": pdf_path.name, "container": "bronze"}
        images = extractor["convert"](blob_input, pdf_path.read_bytes())
        instance_id = f"local-evaluation-{pdf_path.stem}"
        classification = extractor["classify"](images, instance_id)
        prompts = extractor["load_prompts"]()
        result = extractor["extract"](
            images,
            instance_id,
            prompts["system_prompt"],
            prompts["user_prompt"],
            extractor["pages_per_request"](),
            document_context=extractor["format_context"](classification),
            model_runner=extractor["run_prompt"],
            verify=False,
        )
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        sidecar_path = pdf_path.with_suffix(".json")
        comparison = None
        if sidecar_path.exists():
            comparison = compare_reference(
                json.loads(sidecar_path.read_text(encoding="utf-8-sig")),
                result,
            )
        review_reasons = []
        if classification.get("document_type") == "unknown":
            review_reasons.append("unknown-document-type")
        if not classification.get("contains_results"):
            review_reasons.append("no-confirmed-results")
        return {
            "source_file": pdf_path.name,
            "status": "success",
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "document_type": classification.get("document_type", "unknown"),
            "jurisdiction": classification.get("jurisdiction"),
            "needs_review": bool(review_reasons),
            "review_reasons": review_reasons,
            "reference_comparison": comparison,
        }
    except Exception as error:
        return {
            "source_file": pdf_path.name,
            "status": "failed",
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "error_category": type(error).__name__,
        }


def evaluate_directory(input_dir, result_dir):
    result_dir.mkdir(parents=True, exist_ok=True)
    extractor = _load_extractor()
    records = []
    for pdf_path in sorted(input_dir.rglob("*.pdf")):
        output_path = result_dir / f"{pdf_path.stem}.json"
        records.append(evaluate_pdf(pdf_path, output_path, extractor))

    successful = [record for record in records if record["status"] == "success"]
    summary = {
        "total_files": len(records),
        "successful_files": len(successful),
        "failed_files": len(records) - len(successful),
        "files_needing_review": sum(record.get("needs_review", False) for record in successful),
        "document_types": dict(Counter(record.get("document_type", "unknown") for record in successful)),
        "per_field_mismatch_counts": dict(Counter(
            field
            for record in successful
            for field, count in (record.get("reference_comparison") or {}).get("mismatch_counts", {}).items()
            for _ in range(count)
        )),
        "documents": records,
    }
    (result_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return summary


def main():
    parser = argparse.ArgumentParser(description="Evaluate transcript PDFs locally")
    parser.add_argument("--input", type=Path, default=Path(__file__).parent)
    parser.add_argument("--output", type=Path, default=Path(__file__).parent / "evaluation_results")
    args = parser.parse_args()
    summary = evaluate_directory(args.input, args.output)
    print(json.dumps({key: summary[key] for key in (
        "total_files", "successful_files", "failed_files",
        "files_needing_review", "document_types", "per_field_mismatch_counts",
    )}, indent=2))


if __name__ == "__main__":
    main()
