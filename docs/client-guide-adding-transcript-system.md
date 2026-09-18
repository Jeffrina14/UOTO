# Client Guide: Adding A New Transcript System

This guide explains how to add support for a new transcript or academic-document system, following the same profile-based approach used for SUS and EBF.

## Overview

Each document system is represented by a profile. A profile controls:

- How the filename is routed to the correct extraction rules.
- Which system and user prompts are used.
- Profile-specific OCR corrections and metadata defaults.
- How institution, course, grade, credit, and notes fields are interpreted.
- How the extracted institution is passed to the gold accreditation workflow.

The profile must identify the document format, but it must not assign one fixed institution name to every document. The institution must be read from each transcript.

## Information Needed Before Development

Provide at least:

- A short profile code, such as `SUS`, `EBF`, or `NEW`.
- The filename token that identifies the system.
- Two or more representative redacted transcripts.
- The document language or languages.
- The country or education system, if known.
- The table headings and grading conventions.
- Whether the documents contain course codes, credits, levels, periods, or repeated attempts.
- The expected institution identity fields.
- Any known OCR problems or recurring labels.
- Whether location, country, or website details may appear in the header.

Do not use student names, dates of birth, addresses, or other personal data as routing keys.

## Step 1: Add The Profile Code

Update `pipeline/pipelineUtils/document_profiles.py`.

Add a profile constant and extend the filename pattern. For example:

```python
PROFILE_NEW = "NEW"
_PROFILE_PATTERN = re.compile(
    r"(?:^|[_.-])(SUS|EBF|NEW)(?:[_.-]|$)",
    re.IGNORECASE,
)
```

Update any profile-specific normalization rules only when they are deterministic and verified from source documents.

Important rules:

- Never hardcode one institution name for an entire profile.
- Preserve the institution extracted from the current document.
- Add an OCR spelling correction only when the correct spelling is verified.
- Keep corrections narrow and document-specific.
- Do not modify Blob trigger code when adding a transcript profile.

## Step 2: Create The Prompt File

Create `data/new.yaml` using the existing `data/sus.yaml` or `data/ebf.yaml` structure.

The prompt should describe:

- The document type and education system.
- How to identify the issuing institution.
- How to read the course table.
- Which column supplies the student grade.
- How to preserve class averages and comparison values.
- How to handle course codes and missing values.
- How to preserve original spelling and language.
- How to identify printed location, country, and website details.
- How to handle logos and small header text.
- Which rows are real courses and which are summaries or legends.

The institution section should follow this pattern:

```text
Determine the issuing institution from the document header, issuer block,
logo caption, or official identity line. Preserve the complete name printed on
this document. Do not use the filename, profile name, prior transcript, city,
district, translator, or mailing address as the institution. If a location,
country, or website is visibly printed, return it in institution_details. If it
is not printed, return an empty value and do not infer it.
```

Include `institution_details` in the requested JSON shape:

```yaml
user_prompt: |
  Return strict JSON with institution_context and institution_details.
  institution_details must contain name, location, country, and website.
```

## Step 3: Upload The Prompt

The prompt file must be uploaded to the `prompts` Blob container.

For a deployed environment:

```powershell
az storage blob upload `
  --account-name <data-storage-account> `
  --container-name prompts `
  --name new.yaml `
  --file data\new.yaml `
  --auth-mode login `
  --overwrite true
```

For a repeatable deployment, add the upload to:

- `scripts/postprovision.ps1`
- `scripts/postprovision.sh`

## Step 4: Add Prompt Loading

Update `pipeline/pipelineUtils/prompts.py` only if the existing profile loader does not already support the new code.

The loader should map the profile to its prompt file:

```python
PROFILE_PROMPT_FILES = {
    "SUS": "sus.yaml",
    "EBF": "ebf.yaml",
    "NEW": "new.yaml",
}
```

If the file is missing, fail clearly. Do not silently use another system's prompt because that can produce plausible but incorrect results.

## Step 5: Add Profile-Specific Normalization

Use `normalize_profile_result` for deterministic cleanup only:

- Correct verified OCR variants.
- Set a reliable source language when the document system guarantees it.
- Normalize a known course label typo.
- Preserve institution and location extracted from the current document.
- Preserve empty values as empty strings, lists, or maps according to the schema.

Do not do this:

```python
canonical_institution = "One Fixed School"
```

A profile represents a document system, not one school. Different documents in the same profile may come from different institutions.

## Step 6: Institution Details And Gold Accreditation

Silver should contain:

```json
{
  "institution_context": ["Institution Name"],
  "institution_details": [
    {
      "name": "Institution Name",
      "location": "Printed location or empty string",
      "country": "Printed country or empty string",
      "website": "Printed website or empty string"
    }
  ]
}
```

Gold uses this information to distinguish institutions with the same name. A website or location is used in search queries when it is present, but it is never invented.

Gold classifications are:

- `found_in_directory`: the exact institution is listed on one of the configured accreditation-directory domains.
- `accredited_other_source`: an official school, government, or accreditor source confirms accreditation, but no direct configured-directory record was found.
- `not_found`: the search completed but did not find accreditation evidence.
- `unverified`: the search was incomplete, ambiguous, rate-limited, or failed.

A school must not be approved only because a common keyword appears in a search result. The institution name, location, country, website, and source evidence must identify the same entity.

## Step 7: Add Tests

Add tests for:

- Filename routing to the new profile.
- Case-insensitive profile matching.
- Similar filename tokens not matching accidentally.
- Institution details being preserved.
- Location-aware search queries.
- Profile-specific OCR corrections.
- Course row ownership and grade-column behavior.
- Missing fields using the correct empty value.

Example routing test:

```python
self.assertEqual(
    profile_for_blob("bronze/123_NEW_transcript.pdf"),
    PROFILE_NEW,
)
```

Example institution-preservation test:

```python
result = normalize_profile_result(
    {
        "institution_details": [{
            "name": "Example High School",
            "location": "Example City",
            "country": "Example Country",
            "website": "",
        }],
        "courses": [{"institution": "Example High School"}],
    },
    PROFILE_NEW,
)

assert result["institution_details"][0]["name"] == "Example High School"
assert result["courses"][0]["institution"] == "Example High School"
```

## Step 8: Validate Locally

Run focused tests and compile the touched modules:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_document_profiles
.\.venv\Scripts\python.exe -m unittest tests.test_accreditation
.\.venv\Scripts\python.exe -m py_compile pipeline\pipelineUtils\document_profiles.py
.\.venv\Scripts\python.exe -m py_compile pipeline\activities\callFoundryMultiModal.py
```

Inspect the generated JSON manually. Confirm that:

- The institution is from the document, not the profile default.
- Printed location is preserved exactly enough for identity matching.
- Courses are complete and not duplicated.
- Grades come from the student column.
- Course codes are not invented.
- The source language is correct.

## Step 9: Deploy Correctly

The Function App runs on Linux. Do not deploy Windows-built native Python packages.

Before packaging:

- Keep `.python_packages` excluded through `pipeline/.funcignore`.
- Use Azure remote build/Oryx.
- Ensure `requirements.txt` is included.

Example deployment:

```powershell
$items = Get-ChildItem pipeline -Force |
  Where-Object { $_.Name -notlike ".python_packages*" }
Compress-Archive -Path $items.FullName -DestinationPath new-profile.zip -Force

az functionapp deployment source config-zip `
  --resource-group <resource-group> `
  --name <function-app> `
  --src new-profile.zip `
  --build-remote true `
  --timeout 1800
```

After deployment, confirm the host is healthy and the expected functions remain indexed:

```powershell
curl.exe --silent --show-error `
  "https://<function-app>.azurewebsites.net/admin/host/status?code=<master-key>"

az functionapp function list `
  --resource-group <resource-group> `
  --name <function-app>
```

## Step 10: Run A Controlled Test

1. Upload a unique redacted transcript to `bronze` with the profile token in its filename.
2. Record the upload time.
3. Confirm the Blob trigger starts an orchestration.
4. Wait for the matching `silver/*-output.json` file.
5. Inspect institution details and course rows.
6. Confirm the matching gold accreditation file is generated.
7. Record search sources, status, and review flags.

Example upload:

```powershell
az storage blob upload `
  --account-name <data-storage-account> `
  --container-name bronze `
  --name 123_NEW_transcript.pdf `
  --file .\test-data\123_NEW_transcript.pdf `
  --auth-mode login `
  --overwrite true
```

Do not reuse a test filename when measuring trigger behavior. Use a unique name so a previous trigger receipt cannot affect the test.

## Common Mistakes

- Hardcoding one institution name for a profile.
- Treating the filename as the institution identity.
- Searching only by institution name when a printed location is available.
- Treating a general accreditor page as proof that the school is listed.
- Treating absence from one directory as proof of no accreditation.
- Deploying Windows-built `.python_packages` to the Linux Function App.
- Forgetting to upload the new prompt file.
- Omitting tests for OCR variants and missing location data.
- Reusing a previous bronze filename during trigger testing.
- Changing Blob trigger code when only a new transcript profile is needed.

## Production Checklist

- [ ] New profile code added to the router.
- [ ] New prompt file added under `data/`.
- [ ] Prompt file uploaded to the `prompts` container.
- [ ] Profile loader maps the code to the prompt file.
- [ ] Institution names are extracted from each document.
- [ ] Printed location, country, and website are preserved when available.
- [ ] No fixed institution name is assigned to the profile.
- [ ] Profile-specific normalization is covered by tests.
- [ ] Linux remote build is used for deployment.
- [ ] Silver output is manually reviewed against the source.
- [ ] Gold search evidence and source URLs are reviewed.
- [ ] A unique end-to-end test has completed successfully.
- [ ] Blob trigger code was not changed unnecessarily.
