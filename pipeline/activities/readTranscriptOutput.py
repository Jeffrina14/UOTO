import json
import logging

import azure.durable_functions as df

from pipelineUtils.accreditation import extract_institutions, normalize_blob_path
from pipelineUtils.blob_functions import get_blob_content
from configuration import Configuration

name = "readTranscriptOutput"
bp = df.Blueprint()
config = Configuration()


def _max_institutions():
    value = config.get_value("ACCREDITATION_MAX_INSTITUTIONS", "25")
    try:
        limit = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("ACCREDITATION_MAX_INSTITUTIONS must be a positive integer") from error
    if limit < 1:
        raise ValueError("ACCREDITATION_MAX_INSTITUTIONS must be a positive integer")
    return limit


@bp.function_name(name)
@bp.activity_trigger(input_name="args")
def run(args: dict):
    """Return only institution context so no student data enters orchestration state."""
    container = args.get("container") or "silver"
    blob_name = args["blob_name"]

    blob_content = get_blob_content(
        container_name=container,
        blob_path=normalize_blob_path(container, blob_name),
    )
    transcript = json.loads(blob_content.decode("utf-8"))
    institutions, truncated = extract_institutions(transcript, limit=_max_institutions())

    logging.info(
        f"readTranscriptOutput: institutions={len(institutions)} truncated={truncated}"
    )
    return {"institutions": institutions, "truncated": truncated}
