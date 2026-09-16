import json
import logging
import datetime

import azure.functions as func
import azure.durable_functions as df
from azure.durable_functions import RetryOptions


from activities import (
    runDocIntel,
    callAiFoundry,
    writeToBlob,
    speechToText,
    callFoundryMultiModal,
    readTranscriptOutput,
    accreditationLookup,
    writeAccreditation,
    updateAccreditationSummary,
)
from configuration import Configuration

from pipelineUtils.accreditation import is_transcript_output_name
from pipelineUtils.blob_functions import BlobMetadata, list_blobs
from pipelineUtils.document_profiles import profile_for_blob
from pipelineUtils.transcript_parser import parse_transcript_response

config = Configuration()

# NEXT_STAGE = config.get_value("NEXT_STAGE")
FINAL_OUTPUT_CONTAINER = config.get_value("FINAL_OUTPUT_CONTAINER")

app = df.DFApp(http_auth_level=func.AuthLevel.FUNCTION)


# Shared handler for blob triggers.
async def _handle_blob_trigger(
    blob: func.InputStream,
    client: df.DurableOrchestrationClient,
):
    logging.info(f"Blob Trigger - Blob Received: {blob}")
    logging.info(f"path: {blob.name}")
    logging.info(f"Size: {blob.length} bytes")
    logging.info(f"URI: {blob.uri}")

    blob_metadata = BlobMetadata(
        name=blob.name,
        container="bronze",
        uri=blob.uri
    )
    blob_metadata.profile = profile_for_blob(blob.name)
    logging.info(f"Blob Metadata: {blob_metadata}")
    logging.info(f"Blob Metadata JSON: {blob_metadata.to_dict()}")
    instance_id = await client.start_new("process_blob", client_input=blob_metadata.to_dict())
    logging.info(f"Started orchestration {instance_id} for blob {blob.name}")


# Production: polling-based blob trigger
@app.function_name(name="start_orchestrator_on_blob")
@app.blob_trigger(
    arg_name="blob",
    path="bronze/{name}",
    connection="DataStorage",
)
@app.durable_client_input(client_name="client")
async def start_orchestrator_blob(
    blob: func.InputStream,
    client: df.DurableOrchestrationClient,
):
    await _handle_blob_trigger(blob, client)


def _blob_name(blob):
    return getattr(blob, "name", None) if blob is not None else None


def _blob_modified(blob):
    return getattr(blob, "last_modified", None) or getattr(
        getattr(blob, "properties", None), "last_modified", None
    )


def _expected_silver_name(blob_name):
    filename = blob_name.rsplit("/", 1)[-1]
    return f"{filename.rsplit('.', 1)[0]}-output.json"


@app.function_name(name="poll_bronze_for_processing")
@app.timer_trigger(
    schedule="0 */1 * * * *",
    arg_name="timer",
    run_on_startup=False,
    use_monitor=True,
)
@app.durable_client_input(client_name="client")
async def poll_bronze_for_processing(
    timer: func.TimerRequest,
    client: df.DurableOrchestrationClient,
):
    """Fallback for environments where the storage blob trigger does not poll."""
    if config.get_value("BRONZE_POLLING_ENABLED", "true").lower() != "true":
        return

    now = datetime.datetime.now(datetime.timezone.utc)
    lookback_minutes = int(config.get_value("BRONZE_POLL_LOOKBACK_MINUTES", "30"))
    cutoff = now - datetime.timedelta(minutes=max(1, lookback_minutes))
    silver_blobs = {
        _blob_name(blob): _blob_modified(blob)
        for blob in list_blobs("silver")
        if _blob_name(blob)
    }

    started = 0
    for blob in list_blobs("bronze"):
        blob_name = _blob_name(blob)
        modified = _blob_modified(blob)
        if not blob_name or not modified or modified < cutoff:
            continue
        if not blob_name.lower().endswith((".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".bmp")):
            continue

        output_name = _expected_silver_name(blob_name)
        output_modified = silver_blobs.get(output_name)
        if output_modified and output_modified >= modified:
            continue

        metadata = BlobMetadata(
            name=blob_name,
            container="bronze",
            uri=f"https://{config.get_value('DATA_STORAGE_ACCOUNT_NAME', '')}.blob.core.windows.net/bronze/{blob_name}",
            profile=profile_for_blob(blob_name),
        )
        await client.start_new("process_blob", client_input=metadata.to_dict())
        started += 1

    if started:
        logging.info(f"poll_bronze_for_processing: started={started}")


# Second stage: accreditation research, triggered by completed transcripts.
@app.function_name(name="start_accreditation_on_silver")
@app.blob_trigger(
    arg_name="blob",
    path="silver/{name}",
    connection="DataStorage",
)
@app.durable_client_input(client_name="client")
async def start_accreditation_blob(
    blob: func.InputStream,
    client: df.DurableOrchestrationClient,
):
    if config.get_value("ACCREDITATION_ENABLED", "false").lower() != "true":
        return
    if not is_transcript_output_name(blob.name):
        return

    blob_metadata = BlobMetadata(
        name=blob.name,
        container="silver",
        uri=blob.uri,
        profile=profile_for_blob(blob.name),
    )
    instance_id = await client.start_new(
        "process_accreditation", client_input=blob_metadata.to_dict()
    )
    logging.info(
        f"Started accreditation orchestration {instance_id} for blob {blob.name}"
    )


# An HTTP-triggered function with a Durable Functions client binding
@app.route(route="client")
@app.durable_client_input(client_name="client")
async def start_orchestrator_http(req: func.HttpRequest, client):
    """
    Starts a new orchestration instance and returns a response to the client.

    args:
        req (func.HttpRequest): The HTTP request object. Contains an array of JSONs with fields: name, and container
        client (DurableOrchestrationClient): The Durable Functions client.
    response:
        func.HttpResponse: The HTTP response object.
    """
    
    #Perform basic validation on the request body
    try:
        body = req.get_json()
        blob_name = body.get("name")
        blob_uri = body.get("uri")

    except ValueError:
        return func.HttpResponse("Invalid JSON.", status_code=400)

   
    blob_input = {
        "name": blob_name,
        "container": "bronze",
        "uri": blob_uri,
        "profile": profile_for_blob(blob_name),
    }

    #invoke the process_blob function with the list of blobs
    instance_id = await client.start_new('process_blob', client_input=blob_input)
    logging.info(f"Started orchestration with Batch ID = '{instance_id}'.")

    response = client.create_check_status_response(req, instance_id)
    return response


#Sub orchestrator
@app.function_name(name="process_blob")
@app.orchestration_trigger(context_name="context")
def process_blob(context):
    blob_input = context.get_input()
    sub_orchestration_id = context.instance_id 
    # Get file extensions
    blob_name = blob_input.get("name", "")
    source_filename = blob_name.rsplit("/", 1)[-1]
    output_filename = f"{source_filename.rsplit('.', 1)[0]}-output.json"
    logging.info(
        f"Processing source filename={source_filename} "
        f"instance_id={sub_orchestration_id}"
    )
    file_extension = blob_name.lower().split('.')[-1] if '.' in blob_name else ""
    # Audio file extensions
    audio_extensions = ['wav', 'mp3', 'opus', 'ogg', 'flac', 'wma', 'aac', 'webm']
    # Document file extensions
    document_extensions = ['pdf', 'docx', 'doc', 'xlsx', 'pptx', 'jpg', 'jpeg', 'png', 'tiff', 'bmp']
    

    # Define retry options for handling transient failures
    # Note: backoff_coefficient requires azure-functions-durable >= 1.3.0
    retry_options = RetryOptions(
        first_retry_interval_in_milliseconds=5000,    # 5 seconds initial wait
        max_number_of_attempts=5                       # More attempts for rate limit scenarios
    )

    # 1. Process Data Source based on file type
    multimodal_enabled = config.get_value("AOAI_MULTI_MODAL", "false").lower() == "true"
    if multimodal_enabled and file_extension in document_extensions:
        logging.info(
            f"Selected processing path=multimodal source filename={source_filename} "
            f"instance_id={sub_orchestration_id}"
        )
        aoai_input = {
            "name": blob_input.get("name"),
            "container": blob_input.get("container"),
            "uri": blob_input.get("uri"),
            "instance_id": sub_orchestration_id,
            "profile": blob_input.get("profile", profile_for_blob(blob_name)),
        }

        raw_multimodal_result = yield context.call_activity_with_retry(
            "callAoaiMultiModal", retry_options, aoai_input
        )
        validated_multimodal_result = parse_transcript_response(raw_multimodal_result)
        validated_multimodal_result["processing"] = {
            "profile": blob_input.get("profile", profile_for_blob(blob_name)),
            "routing_source": "filename",
        }
        final_result = json.dumps(
            validated_multimodal_result,
            ensure_ascii=False,
            indent=2,
        )


    elif config.get_value("AI_VISION_ENABLED", "false").lower() == "true":
        pass

    elif file_extension in audio_extensions:
        # Process audio with speech-to-text
        logging.info(
            f"Selected processing path=audio source filename={source_filename} "
            f"instance_id={sub_orchestration_id}"
        )
        text_result = yield context.call_activity_with_retry("speechToText", retry_options, blob_input)

    elif file_extension in document_extensions:
        # Process document with Document Intelligence
        logging.info(
            f"Selected processing path=document-intelligence source filename={source_filename} "
            f"instance_id={sub_orchestration_id}"
        )
        text_result = yield context.call_activity_with_retry("runDocIntel", retry_options, blob_input)
        
    else:
        # Unsupported file type
        logging.warning(f"Unsupported file type: {file_extension} for blob: {blob_name}")
        return {
            "blob": blob_input,
            "error": f"Unsupported file type: {file_extension}",
            "status": "skipped"
        }
    
    if not (multimodal_enabled and file_extension in document_extensions):
        # Feed non-multimodal output into AOAI to get insights.
        call_aoai_input = {
            "text_result": text_result,
            "instance_id": sub_orchestration_id
        }
        final_result = yield context.call_activity_with_retry("callAoai", retry_options, call_aoai_input)
    

    logging.info(
        f"Writing output filename={output_filename} source filename={source_filename} "
        f"instance_id={sub_orchestration_id}"
    )
    task_result = yield context.call_activity_with_retry(
        "writeToBlob", 
        retry_options,
        {
            "json_str": final_result,
            "blob_name": blob_input["name"],
            "final_output_container": FINAL_OUTPUT_CONTAINER
        }
    )
    return {
        "blob": blob_input,
        "text_result": final_result,
        "task_result": task_result
    }   


# Second stage sub orchestrator: silver transcript -> gold accreditation result.
@app.function_name(name="process_accreditation")
@app.orchestration_trigger(context_name="context")
def process_accreditation(context):
    blob_input = context.get_input()
    blob_name = blob_input.get("name", "")
    container = blob_input.get("container", "silver")

    retry_options = RetryOptions(
        first_retry_interval_in_milliseconds=5000,
        max_number_of_attempts=3
    )

    transcript_info = yield context.call_activity_with_retry(
        "readTranscriptOutput",
        retry_options,
        {"blob_name": blob_name, "container": container},
    )

    institutions = transcript_info.get("institutions") or []
    lookups = [
        context.call_activity_with_retry(
            "accreditationLookup",
            retry_options,
            {"institution": institution, "instance_id": context.instance_id},
        )
        for institution in institutions
    ]
    results = (yield context.task_all(lookups)) if lookups else []

    write_result = yield context.call_activity_with_retry(
        "writeAccreditation",
        retry_options,
        {
            "blob_name": blob_name,
            "institutions": results,
            "truncated": transcript_info.get("truncated", False),
        },
    )
    summary_result = yield context.call_activity_with_retry(
        "updateAccreditationSummary", retry_options, {}
    )

    return {
        "blob": blob_input,
        "institution_count": len(results),
        "write_result": write_result,
        "summary_result": summary_result,
    }

app.register_functions(runDocIntel.bp)
app.register_functions(callAiFoundry.bp)
app.register_functions(writeToBlob.bp)
app.register_functions(speechToText.bp)
app.register_functions(callFoundryMultiModal.bp)
app.register_functions(readTranscriptOutput.bp)
app.register_functions(accreditationLookup.bp)
app.register_functions(writeAccreditation.bp)
app.register_functions(updateAccreditationSummary.bp)