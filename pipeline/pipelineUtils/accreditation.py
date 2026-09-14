"""Pure helpers for the silver-to-gold accreditation workflow.

This module performs no network or storage access so that every identity,
directory and status rule can be unit tested in isolation.
"""

import re
from urllib.parse import urlparse


ACCREDITATION_DIRECTORIES = {
    "WHED (IAU World Higher Education Database)": "whed.net",
    "CHEA (US Council for Higher Education Accreditation)": "chea.org",
    "MSA-CESS (Middle States Association)": "msa-cess.org",
    "Cognia (formerly AdvancED / SACS / NCA)": "cognia.org",
    "ACS WASC (Western Association of Schools and Colleges)": "acswasc.org",
    "NEASC (New England Association of Schools and Colleges)": "neasc.org",
    "CIS (Council of International Schools)": "cois.org",
}

DISCLAIMER = (
    "Automated research aid based on web search results. Verify each result "
    "against the linked sources before relying on it. Absence from a directory "
    "does not prove an institution is unaccredited."
)

ABSENCE_NOTE = (
    "Absence from these directories does not prove the institution is unaccredited."
)

STATUS_FOUND_IN_DIRECTORY = "found_in_directory"
STATUS_ACCREDITED_OTHER_SOURCE = "accredited_other_source"
STATUS_NOT_FOUND = "not_found"
STATUS_UNVERIFIED = "unverified"

CONFIDENCE_ORDER = ("low", "medium", "high")

FLAG_RECOGNIZED = "RECOGNIZED"
FLAG_CONTAINS_UNRECOGNIZED = "CONTAINS_UNRECOGNIZED"
FLAG_NEEDS_REVIEW = "NEEDS_REVIEW"

TRANSCRIPT_OUTPUT_SUFFIX = "-output.json"
ACCREDITATION_SUFFIX = "-accreditation.json"
SUMMARY_BLOB_NAME = "accreditation_summary.json"

SEARCH_QUERY_TEMPLATES = (
    '"{institution}" accreditation',
    '"{institution}" accredited school OR university directory',
)

INSTITUTION_CONTEXT_KEYS = ("name", "location", "country", "website")

_HIT_KEYS = ("results", "data", "hits", "documents", "items", "matches", "value")
_WRAPPER_KEYS = ("response", "result", "body", "webPages")

_NAME_STOPWORDS = frozenset({
    "the", "of", "and", "for", "at", "in", "de", "la", "le", "el", "du", "des",
})

_STATUS_RANK = {
    STATUS_UNVERIFIED: 0,
    STATUS_NOT_FOUND: 1,
    STATUS_ACCREDITED_OTHER_SOURCE: 2,
    STATUS_FOUND_IN_DIRECTORY: 3,
}


def blob_basename(blob_name):
    return str(blob_name or "").replace("\\", "/").rsplit("/", 1)[-1]


def normalize_blob_path(container, blob_name):
    """Strip a leading container prefix so downloads use a container-relative path."""
    name = str(blob_name or "").replace("\\", "/").lstrip("/")
    prefix = f"{str(container or '')}/"
    if prefix != "/" and name.lower().startswith(prefix.lower()):
        name = name[len(prefix):]
    return name


def is_transcript_output_name(blob_name):
    return blob_basename(blob_name).lower().endswith(TRANSCRIPT_OUTPUT_SUFFIX)


def source_id(blob_name):
    stem = blob_basename(blob_name)
    if stem.lower().endswith(".json"):
        stem = stem[: -len(".json")]
    if stem.lower().endswith("-output"):
        stem = stem[: -len("-output")]
    return stem


def accreditation_blob_name(blob_name):
    stem = source_id(blob_name)
    if not stem:
        raise ValueError("Transcript blob name does not contain a source name")
    return f"{stem}{ACCREDITATION_SUFFIX}"


def extract_institutions(transcript, limit=None):
    """Return (institution context list, truncated) from a validated transcript."""
    if not isinstance(transcript, dict):
        raise ValueError("Transcript must be a JSON object")

    courses = transcript.get("courses") or []
    if not isinstance(courses, list):
        raise ValueError("Transcript courses must be a JSON array")

    seen = set()
    institutions = []
    for course in courses:
        if not isinstance(course, dict):
            continue
        name = course.get("institution")
        if not isinstance(name, str):
            continue
        name = " ".join(name.split())
        key = name.lower()
        if not name or key in seen:
            continue
        seen.add(key)
        institutions.append({
            "name": name,
            "location": None,
            "country": None,
            "website": None,
        })

    truncated = False
    if limit is not None and len(institutions) > limit:
        institutions = institutions[:limit]
        truncated = True
    return institutions, truncated


def build_search_queries(institution_name):
    name = " ".join(str(institution_name or "").split())
    if not name:
        return []
    return [template.format(institution=name) for template in SEARCH_QUERY_TEMPLATES]


def build_search_payload(query, max_results):
    """Search payloads never carry anything beyond the query and result cap."""
    return {"query": str(query), "max_results": int(max_results)}


def normalize_search_results(data, max_text=1200):
    results = []
    for hit in _find_hit_list(data):
        if not isinstance(hit, dict):
            continue
        url = str(hit.get("url") or hit.get("link") or "").strip()
        if not _is_http_url(url):
            continue
        title = hit.get("title") or hit.get("name") or hit.get("heading") or ""
        text = (
            hit.get("text") or hit.get("snippet") or hit.get("summary")
            or hit.get("content") or hit.get("highlights") or ""
        )
        if isinstance(text, (list, tuple)):
            text = " ".join(str(item) for item in text)
        results.append({
            "title": " ".join(str(title).split()),
            "url": url,
            "text": " ".join(str(text).split())[:max_text],
        })
    return results


def dedupe_results(results, limit=None):
    seen = set()
    merged = []
    for hit in results or []:
        url = str(hit.get("url") or "").lower()
        if not url or url in seen:
            continue
        seen.add(url)
        merged.append(hit)
    if limit is not None:
        merged = merged[:limit]
    return merged


def directory_for_url(url):
    host = _host(url)
    if not host:
        return None
    for name, domain in ACCREDITATION_DIRECTORIES.items():
        if host == domain or host.endswith("." + domain):
            return name
    return None


def same_entity_confirmed(institution, hit):
    tokens = _significant_tokens((institution or {}).get("name"))
    if not tokens:
        return False

    website_host = _host((institution or {}).get("website"))
    if website_host and website_host == _host((hit or {}).get("url")):
        return True

    haystack = _searchable_text(hit)
    return all(token in haystack for token in tokens)


def confidence_ceiling(institution):
    """Name-only identity can never reach high confidence."""
    return "high" if (institution or {}).get("website") else "medium"


def validate_matched_directories(claimed_matches, hits, institution):
    """Keep only directory matches whose URL was actually returned by the search."""
    by_url = {
        str(hit.get("url") or "").lower(): hit
        for hit in hits or []
        if hit.get("url")
    }
    validated = []
    seen = set()
    for match in claimed_matches or []:
        if not isinstance(match, dict):
            continue
        url = str(match.get("url") or "").strip()
        key = url.lower()
        hit = by_url.get(key)
        if hit is None or key in seen:
            continue
        directory = directory_for_url(url)
        if directory is None or not same_entity_confirmed(institution, hit):
            continue
        seen.add(key)
        validated.append({
            "directory": directory,
            "url": url,
            "evidence": _evidence_from_hit(match.get("evidence"), hit),
        })
    return validated


def build_institution_result(institution, model_verdict, hits, queries):
    verdict = model_verdict if isinstance(model_verdict, dict) else {}
    hits = list(hits or [])

    matched = validate_matched_directories(
        verdict.get("matched_directories"), hits, institution
    )
    entity_confirmed = bool(matched) or any(
        same_entity_confirmed(institution, hit) for hit in hits
    )
    bodies = _clean_string_list(verdict.get("accrediting_bodies"))
    claimed = str(verdict.get("accreditation_status") or "").strip().lower()
    is_exam_board = bool(verdict.get("is_exam_board"))

    if matched:
        status = STATUS_FOUND_IN_DIRECTORY
    elif claimed == STATUS_ACCREDITED_OTHER_SOURCE and entity_confirmed and bodies:
        status = STATUS_ACCREDITED_OTHER_SOURCE
    elif not hits or not entity_confirmed:
        status = STATUS_UNVERIFIED
    elif claimed == STATUS_NOT_FOUND:
        status = STATUS_NOT_FOUND
    else:
        status = STATUS_UNVERIFIED

    confidence = _resolve_confidence(verdict.get("confidence"), institution, status)
    needs_review = (
        status not in (STATUS_FOUND_IN_DIRECTORY, STATUS_ACCREDITED_OTHER_SOURCE)
        or confidence == "low"
        or is_exam_board
        or not entity_confirmed
    )

    return {
        "institution": (institution or {}).get("name"),
        "accreditation_status": status,
        "accrediting_bodies": bodies if status in (
            STATUS_FOUND_IN_DIRECTORY, STATUS_ACCREDITED_OTHER_SOURCE
        ) else [],
        "matched_directories": matched,
        "is_exam_board": is_exam_board,
        "same_entity_confirmed": bool(entity_confirmed),
        "confidence": confidence,
        "needs_human_review": bool(needs_review),
        "summary": _clean_optional_text(verdict.get("summary")),
        "notes": _resolve_notes(verdict.get("notes"), status),
        "queries": list(queries or []),
        "checked_directories": list(ACCREDITATION_DIRECTORIES),
        "sources": hits,
    }


def unverified_result(institution, queries, summary, sources=None):
    return {
        "institution": (institution or {}).get("name"),
        "accreditation_status": STATUS_UNVERIFIED,
        "accrediting_bodies": [],
        "matched_directories": [],
        "is_exam_board": False,
        "same_entity_confirmed": False,
        "confidence": "low",
        "needs_human_review": True,
        "summary": _clean_optional_text(summary),
        "notes": ABSENCE_NOTE,
        "queries": list(queries or []),
        "checked_directories": list(ACCREDITATION_DIRECTORIES),
        "sources": list(sources or []),
    }


def derive_review_flags(institutions, truncated=False):
    institutions = list(institutions or [])
    flags = []
    if any(
        item.get("accreditation_status") == STATUS_NOT_FOUND
        for item in institutions
    ):
        flags.append(FLAG_CONTAINS_UNRECOGNIZED)

    if truncated or any(item.get("needs_human_review") for item in institutions):
        flags.append(FLAG_NEEDS_REVIEW)
    elif institutions:
        flags.append(FLAG_RECOGNIZED)
    return flags


def build_document_result(source_file, institutions, generated_at, truncated=False):
    institutions = list(institutions or [])
    return {
        "source_file": source_file,
        "generated_at": generated_at,
        "disclaimer": DISCLAIMER,
        "review_flags": derive_review_flags(institutions, truncated),
        "institution_count": len(institutions),
        "institutions": institutions,
    }


def recognized_by(institution_result):
    names = []
    for match in (institution_result or {}).get("matched_directories") or []:
        if not isinstance(match, dict):
            continue
        directory = str(match.get("directory") or "").strip()
        if directory and directory not in names:
            names.append(directory)
    for body in _clean_string_list((institution_result or {}).get("accrediting_bodies")):
        if body not in names:
            names.append(body)
    return names


def build_summary(documents, generated_at):
    schools = {}
    for document in documents or []:
        if not isinstance(document, dict):
            continue
        source = document.get("source_file")
        for institution in document.get("institutions") or []:
            if not isinstance(institution, dict):
                continue
            name = " ".join(str(institution.get("institution") or "").split())
            if not name:
                continue

            key = name.lower()
            status = institution.get("accreditation_status", STATUS_UNVERIFIED)
            candidate = {
                "institution": name,
                "files": [source] if source else [],
                "accreditation_status": status,
                "recognized_by": recognized_by(institution),
                "is_exam_board": bool(institution.get("is_exam_board")),
                "confidence": institution.get("confidence", "low"),
                "top_source_url": _first_source_url(institution),
            }

            existing = schools.get(key)
            if existing is None:
                schools[key] = candidate
                continue

            files = list(existing["files"])
            if source and source not in files:
                files.append(source)
            if _STATUS_RANK.get(status, 0) > _STATUS_RANK.get(
                existing["accreditation_status"], 0
            ):
                candidate["files"] = files
                schools[key] = candidate
            else:
                existing["files"] = files

    ordered = sorted(schools.values(), key=lambda item: item["institution"].lower())
    for record in ordered:
        record["files"] = sorted(record["files"])

    tally = {}
    for record in ordered:
        status = record["accreditation_status"]
        tally[status] = tally.get(status, 0) + 1

    return {
        "generated_at": generated_at,
        "directories_checked": dict(ACCREDITATION_DIRECTORIES),
        "disclaimer": DISCLAIMER,
        "institution_count": len(ordered),
        "status_tally": tally,
        "schools": ordered,
    }


def _find_hit_list(data):
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for key in _HIT_KEYS:
        if isinstance(data.get(key), list):
            return data[key]
    for wrapper in _WRAPPER_KEYS:
        inner = data.get(wrapper)
        if isinstance(inner, dict):
            for key in _HIT_KEYS:
                if isinstance(inner.get(key), list):
                    return inner[key]
    return []


def _is_http_url(url):
    parsed = urlparse(str(url or ""))
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _host(url):
    if not _is_http_url(url):
        return ""
    host = urlparse(str(url)).netloc.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def _significant_tokens(name):
    tokens = re.findall(r"[^\W_]+", str(name or "").lower(), flags=re.UNICODE)
    return [token for token in tokens if len(token) > 2 and token not in _NAME_STOPWORDS]


def _searchable_text(hit):
    joined = " ".join([
        str((hit or {}).get("title") or ""),
        str((hit or {}).get("text") or ""),
        str((hit or {}).get("url") or ""),
    ]).lower()
    return re.sub(r"[\W_]+", " ", joined, flags=re.UNICODE)


def _evidence_from_hit(quote, hit, max_words=14):
    text = str((hit or {}).get("text") or "")
    candidate = " ".join(str(quote or "").split())
    if candidate and candidate.lower() in text.lower():
        return " ".join(candidate.split()[:max_words])
    trimmed = " ".join(text.split()[:max_words])
    return trimmed or None


def _clean_string_list(values):
    cleaned = []
    for value in values or []:
        text = " ".join(str(value or "").split())
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned


def _clean_optional_text(value):
    text = " ".join(str(value or "").split())
    return text or None


def _resolve_confidence(value, institution, status):
    if status == STATUS_UNVERIFIED:
        return "low"
    candidate = str(value or "").strip().lower()
    if candidate not in CONFIDENCE_ORDER:
        candidate = "low"
    ceiling = confidence_ceiling(institution)
    if CONFIDENCE_ORDER.index(candidate) > CONFIDENCE_ORDER.index(ceiling):
        candidate = ceiling
    return candidate


def _resolve_notes(value, status):
    note = _clean_optional_text(value)
    if status not in (STATUS_NOT_FOUND, STATUS_UNVERIFIED):
        return note
    if not note:
        return ABSENCE_NOTE
    return note if ABSENCE_NOTE.lower() in note.lower() else f"{note} {ABSENCE_NOTE}"


def _first_source_url(institution_result):
    for source in (institution_result or {}).get("sources") or []:
        if isinstance(source, dict) and source.get("url"):
            return source["url"]
    return None
