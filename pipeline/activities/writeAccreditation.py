import datetime
import json
import logging

import azure.durable_functions as df

from pipelineUtils.accreditation import (
    accreditation_blob_name,
    blob_basename,
    build_document_result,
)
from pipelineUtils.blob_functions import write_to_blob
from configuration import Configuration

name = "writeAccreditation"
bp = df.Blueprint()
config = Configuration()


def _output_container():
    return config.get_value("ACCREDITATION_OUTPUT_CONTAINER", "gold")


@bp.function_name(name)
@bp.activity_trigger(input_name="args")
def run(args: dict):
    """Write the accreditation result beside, never inside, the silver transcript."""
    blob_name = args["blob_name"]
    institutions = args.get("institutions") or []
    truncated = bool(args.get("truncated"))

    container = _output_container()
    output_blob = accreditation_blob_name(blob_name)
    document = build_document_result(
        blob_basename(blob_name),
        institutions,
        datetime.datetime.now(datetime.timezone.utc).isoformat(),
        truncated,
    )

    payload = json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")
    write_to_blob(container, output_blob, payload)

    logging.info(
        f"writeAccreditation: wrote {output_blob} to {container} "
        f"institutions={document['institution_count']} flags={document['review_flags']}"
    )
    return {
        "success": True,
        "container": container,
        "output_blob": output_blob,
        "review_flags": document["review_flags"],
        "institution_count": document["institution_count"],
    }
