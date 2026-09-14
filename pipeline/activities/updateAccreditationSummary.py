import datetime
import json
import logging

import azure.durable_functions as df

from pipelineUtils.accreditation import (
    ACCREDITATION_SUFFIX,
    SUMMARY_BLOB_NAME,
    build_summary,
)
from pipelineUtils.blob_functions import get_blob_content, list_blobs, write_to_blob
from configuration import Configuration

name = "updateAccreditationSummary"
bp = df.Blueprint()
config = Configuration()


def _enabled():
    return str(config.get_value("ACCREDITATION_SUMMARY_ENABLED", "true")).lower() == "true"


def _output_container():
    return config.get_value("ACCREDITATION_OUTPUT_CONTAINER", "gold")


def _blob_name(blob):
    if isinstance(blob, dict):
        return blob.get("name")
    return getattr(blob, "name", None)


@bp.function_name(name)
@bp.activity_trigger(input_name="args")
def run(args: dict):
    """Rebuild the summary from every gold result so concurrent runs cannot lose updates."""
    if not _enabled():
        return {"success": True, "skipped": True}

    container = _output_container()
    documents = []
    for blob in list_blobs(container):
        blob_name = _blob_name(blob)
        if not blob_name or not blob_name.endswith(ACCREDITATION_SUFFIX):
            continue
        try:
            content = get_blob_content(container_name=container, blob_path=blob_name)
            documents.append(json.loads(content.decode("utf-8")))
        except Exception as error:
            logging.warning(
                f"updateAccreditationSummary: skipped unreadable result ({type(error).__name__})"
            )

    summary = build_summary(
        documents, datetime.datetime.now(datetime.timezone.utc).isoformat()
    )
    payload = json.dumps(summary, ensure_ascii=False, indent=2).encode("utf-8")
    write_to_blob(container, SUMMARY_BLOB_NAME, payload)

    logging.info(
        f"updateAccreditationSummary: institutions={summary['institution_count']} "
        f"documents={len(documents)}"
    )
    return {
        "success": True,
        "skipped": False,
        "container": container,
        "output_blob": SUMMARY_BLOB_NAME,
        "institution_count": summary["institution_count"],
    }
