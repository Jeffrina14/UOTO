import re


PROFILE_SUS = "SUS"
PROFILE_EBF = "EBF"
PROFILE_UNKNOWN = "UNKNOWN"

_PROFILE_PATTERN = re.compile(r"(?:^|[_.-])(SUS|EBF)(?:[_.-]|$)", re.IGNORECASE)


def profile_for_blob(blob_name):
    """Select a document profile from a filename token without using global state."""
    filename = str(blob_name or "").replace("\\", "/").rsplit("/", 1)[-1]
    match = _PROFILE_PATTERN.search(filename)
    return match.group(1).upper() if match else PROFILE_UNKNOWN
