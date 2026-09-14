import logging
import time

import azure.durable_functions as df
from openai import OpenAI
from azure.identity import get_bearer_token_provider

from pipelineUtils.accreditation import (
    ACCREDITATION_DIRECTORIES,
    build_search_queries,
    dedupe_results,
    build_institution_result,
    normalize_search_results,
    unverified_result,
)
from pipelineUtils.azure_openai import run_prompt
from pipelineUtils.transcript_parser import parse_json_object
from configuration import Configuration

name = "accreditationLookup"
bp = df.Blueprint()
config = Configuration()

ASSESS_SYSTEM_PROMPT = (
    "You assess whether an educational institution appears in recognized "
    "accreditation or membership directories, using ONLY the supplied web search "
    "results. You never invent facts and return one valid JSON object."
)

ASSESS_SCHEMA = """{
  "accreditation_status": "found_in_directory | accredited_other_source | not_found | unverified",
  "accrediting_bodies": [],
  "matched_directories": [{"directory": null, "url": null, "evidence": null}],
  "is_exam_board": false,
  "confidence": "high | medium | low",
  "summary": null,
  "notes": null
}"""


def _max_results():
    return _positive_int("ACCREDITATION_MAX_RESULTS", "8")


def _timeout():
    return _positive_float("ACCREDITATION_TIMEOUT", "30")


def _search_pause():
    return max(0.0, _positive_float("ACCREDITATION_SEARCH_PAUSE", "0.3", allow_zero=True))


def _openai_client():
    token_provider = get_bearer_token_provider(
        config.credential, "https://ai.azure.com/.default"
    )
    endpoint = config.get_value("OPENAI_API_BASE").rstrip("/")
    return OpenAI(
        base_url=f"{endpoint}/openai/v1/",
        api_key=token_provider,
    )


def _response_value(value, key, default=None):
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _search_sources(response):
    hits = []
    output = _response_value(response, "output", []) or []
    for item in output:
        if _response_value(item, "type") != "web_search_call":
            continue
        action = _response_value(item, "action", {}) or {}
        sources = _response_value(action, "sources", []) or []
        for source in sources:
            url = _response_value(source, "url", "")
            if url:
                hits.append({
                    "title": _response_value(source, "title", ""),
                    "url": url,
                    "text": "",
                })

    grounded_text = _response_value(response, "output_text", "") or ""
    annotations = []
    for item in output:
        if _response_value(item, "type") != "message":
            continue
        for content in _response_value(item, "content", []) or []:
            annotations.extend(_response_value(content, "annotations", []) or [])

    for annotation in annotations:
        url = _response_value(annotation, "url", "")
        if not url:
            continue
        for hit in hits:
            if hit["url"] == url:
                hit["title"] = _response_value(annotation, "title", hit["title"])
                hit["text"] = grounded_text[:1200]
                break
        else:
            hits.append({
                "title": _response_value(annotation, "title", ""),
                "url": url,
                "text": grounded_text[:1200],
            })
    return hits


def _positive_int(key, default):
    value = config.get_value(key, default)
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{key} must be a positive integer") from error
    if parsed < 1:
        raise ValueError(f"{key} must be a positive integer")
    return parsed


def _positive_float(key, default, allow_zero=False):
    value = config.get_value(key, default)
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{key} must be a number") from error
    if parsed < 0 or (parsed == 0 and not allow_zero):
        raise ValueError(f"{key} must be a positive number")
    return parsed


def build_assess_prompt(institution, hits):
    directories = "\n".join(
        f"- {display_name}  (domain: {domain})"
        for display_name, domain in ACCREDITATION_DIRECTORIES.items()
    )
    if hits:
        blocks = [
            f"[{index}] {hit['title']}\n    url: {hit['url']}\n    snippet: {hit['text']}"
            for index, hit in enumerate(hits, start=1)
        ]
        hits_block = "\n".join(blocks)
    else:
        hits_block = "(no results returned)"

    return f"""Institution to check: "{institution.get('name')}"

Decide, USING ONLY THE SEARCH RESULTS BELOW, whether this institution appears in a
recognized accreditation/membership directory or has clear accreditation evidence.
Do NOT use prior knowledge for the final status; if the results do not support a
claim, say so.

Recognized directories (match by the domain in a result's url):
{directories}

SEARCH RESULTS:
{hits_block}

Return STRICT JSON only, exactly this shape:
{ASSESS_SCHEMA}

RULES
- "found_in_directory": at least one result's url domain matches a directory above
  AND the result indicates THIS institution is listed there. Record each such hit
  in matched_directories with the directory name, the exact url from the results,
  and a SHORT evidence quote taken from that result's snippet (max 14 words).
- "accredited_other_source": no directory hit, but a result clearly states the
  institution is accredited. Name the body in accrediting_bodies.
- "not_found": results were returned but none show accreditation or a listing.
- "unverified": results are too thin or ambiguous, or appear to describe a
  different entity than the one named.
- Absence from these specific directories does NOT prove the school is
  unaccredited; note that in "notes" when status is not_found or unverified.
- If the entity is an examination board or awarding body rather than a school or
  university, set is_exam_board true and explain in notes.
- Output ONLY the JSON object."""


def _search(query):
    try:
        allowed_domains = list(ACCREDITATION_DIRECTORIES.values())
        response = _openai_client().responses.create(
            model=config.get_value("OPENAI_MODEL"),
            tools=[{
                "type": "web_search",
                "filters": {"allowed_domains": allowed_domains},
            }],
            tool_choice="required",
            include=["web_search_call.action.sources"],
            input=(
                f"Search the public web for: {query}. "
                "Return concise, factual accreditation-directory evidence for "
                "the institution in the query and cite each source URL. Do not "
                "include personal information."
            ),
        )
        return normalize_search_results(_search_sources(response))
    except Exception as error:
        logging.warning(f"accreditationLookup: search request failed ({type(error).__name__})")
        return []


@bp.function_name(name)
@bp.activity_trigger(input_name="args")
def run(args: dict):
    institution = args.get("institution") or {}
    instance_id = args.get("instance_id", "")

    queries = build_search_queries(institution.get("name"))
    if not queries:
        return unverified_result(institution, queries, "The institution name was empty.")

    hits = []
    pause = _search_pause()
    for index, query in enumerate(queries):
        hits.extend(_search(query))
        if pause and index < len(queries) - 1:
            time.sleep(pause)

    hits = dedupe_results(hits, limit=_max_results())
    if not hits:
        return unverified_result(institution, queries, "No search results were returned.")

    try:
        raw_verdict = run_prompt(
            instance_id,
            ASSESS_SYSTEM_PROMPT,
            build_assess_prompt(institution, hits),
            log_interaction=False,
        )
        verdict = parse_json_object(raw_verdict)
    except Exception as error:
        logging.warning(
            f"accreditationLookup: assessment failed ({type(error).__name__})"
        )
        return unverified_result(
            institution,
            queries,
            "Model assessment failed; review the linked sources.",
            hits,
        )

    return build_institution_result(institution, verdict, hits, queries)
