# Blob Trigger Incident And Recovery

## Summary

On 2026-09-16 and 2026-09-17, uploads to the `bronze` container did not reliably produce transcript output in the `silver` container. The native Azure Blob trigger was correctly registered, but the Function App worker had startup and downstream activity failures.

The pipeline is now working. A controlled EBF image upload on 2026-09-17 was detected by the Blob trigger, started a Durable orchestration, and produced a valid silver JSON output.

## Scope

| Item | Value |
| --- | --- |
| Function App | `func-processing-6nre35xt3z4hg` |
| Resource Group | `P277_VPPRV_CC_URO_AIFactory` |
| Data Storage Account | `st6nre35xt3z4hgdata` |
| Host Storage Account | `st6nre35xt3z4hgfunc` |
| Log Analytics Workspace | `log-6nre35xt3z4hg` |
| Runtime | Azure Functions v4, Python 3.11 |

## Symptoms

- A unique file uploaded to `bronze` did not initially create the expected `*-output.json` file in `silver`.
- Newly added timer and queue triggers did not appear in the live function inventory during unhealthy host states.
- The Functions host returned errors or failed to respond for some HTTP and host-management checks.
- The Azure Functions host log contained worker startup errors and storage authentication failures.

## Root Causes

### 1. Incompatible local dependency package

The deployed app included a locally built `.python_packages` directory. The Function App runs on Linux, while the package had been built on Windows. This caused the Python worker to fail during startup with:

```text
ImportError: cannot import name 'x509' from 'cryptography.hazmat.bindings._rust'
```

The host then reported that no job functions were found. This made function discovery and trigger behavior unreliable.

### 2. Conflicting storage authentication settings

The Function App had both legacy key-based settings (`AzureWebJobsStorage` and `DataStorage`) and managed-identity settings. Storage account key authentication is disabled, so the host attempted an unavailable authentication path and logged:

```text
Unable to access AzureWebJobsStorage
KeyBasedAuthenticationNotPermitted
```

### 3. Missing downstream normalization helper

After the host and listener recovered, the transcript extraction activity failed before writing silver output:

```text
NameError: name '_apply_profile_normalization' is not defined
```

The activity imported `normalize_profile_result` but called the nonexistent `_apply_profile_normalization` name.

## Remediation

1. Removed the conflicting key-based `AzureWebJobsStorage` and `DataStorage` app settings. The app now uses only managed-identity connection settings.
2. Added `.python_packages` to `pipeline/.funcignore` so Windows-built dependencies are not deployed.
3. Deployed the Function App through Azure Functions remote build:

```powershell
az functionapp deployment source config-zip `
  --resource-group P277_VPPRV_CC_URO_AIFactory `
  --name func-processing-6nre35xt3z4hg `
  --src <deployment-zip> `
  --build-remote true
```

This builds the pinned Python dependencies on Linux through Oryx.

4. Corrected the multimodal activity in [pipeline/activities/callFoundryMultiModal.py](../pipeline/activities/callFoundryMultiModal.py):

```python
merged_result = normalize_profile_result(
    merged_result, blob_input.get("profile", "UNKNOWN")
)
```

5. Recycled the Function App after deployment.

## Validation

Controlled test file: `controlled-final-20260917-ebf.png`

| Event | UTC time |
| --- | --- |
| Blob uploaded to `bronze` | 2026-09-17 12:53:59 |
| Blob trigger detected upload | 2026-09-17 12:54:04 |
| Durable orchestration started | 2026-09-17 12:54:07 |
| Silver output written | 2026-09-17 12:54:56 |

Results:

- Detection latency: approximately 5 seconds.
- End-to-end processing time: approximately 57 seconds.
- Output: `silver/controlled-final-20260917-ebf-output.json`.
- Output was valid JSON with 11 extracted course records.

## Current Trigger Configuration

The production listener is the native Blob trigger in [pipeline/function_app.py](../pipeline/function_app.py):

```python
@app.function_name(name="start_orchestrator_on_blob")
@app.blob_trigger(
    arg_name="blob",
    path="bronze/{name}",
    connection="DataStorage",
)
@app.durable_client_input(client_name="client")
async def start_orchestrator_blob(blob, client):
    await _handle_blob_trigger(blob, client)
```

No Event Grid, timer, or queue fallback is configured.

## Monitoring

The following diagnostic settings are enabled:

| Setting | Resource | Logs |
| --- | --- | --- |
| `function-app-platform-to-law` | Function App | FunctionAppLogs, App Service audit, IP security, and authentication logs |
| `data-blob-service-to-law` | Data Blob service | StorageRead, StorageWrite, StorageDelete |

Both route to `log-6nre35xt3z4hg`.

The Azure Monitor scheduled-query alert `bronze-upload-missing-silver-output` is enabled:

- Checks every 5 minutes.
- Detects a `bronze` upload older than 30 minutes that has no matching `silver/*-output.json` result.
- Severity: 2.
- No notification action group is attached yet. Attach the client-approved action group to deliver email, Teams, ITSM, or webhook notifications.

## Operational Checks

When investigating future failures:

1. Check Function App host health at `/admin/host/status` from within the VNet.
2. Confirm `start_orchestrator_on_blob` is listed as a `blobTrigger`.
3. Review `FunctionAppLogs` for worker import errors, storage errors, and `Blob Trigger - Blob Received` entries.
4. Confirm managed-identity storage settings are present and legacy key-based storage settings are absent.
5. Confirm the Function App identity has Blob, Queue, and Table data roles for the data and host storage accounts.
6. Check the missing-output alert for uploads that do not reach `silver`.