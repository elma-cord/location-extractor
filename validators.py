import html
import json
import re
from typing import Any


# Digits observed standing in for a lost apostrophe (CORD-6921). The apostrophe
# most often becomes a "7" ("We 7re" -> "We're"), but 2/9/27/39/92/99 have also
# been seen. We only touch a digit that sits where an apostrophe grammatically
# belongs - directly before a contraction ending or a possessive - so real
# numbers ("7 years", "70,000", "24/7", "7am") are never altered.
_APOSTROPHE_DIGIT_RE = re.compile(
    r"\b([A-Za-z]+)\s*(?:2|7|9|27|39|92|99)\s*(ll|re|ve|s|d|m|t)\b",
    re.IGNORECASE,
)

# Curly / non-standard punctuation -> plain ASCII.
_PUNCT_MAP = {
    "‘": "'", "’": "'",
    "“": '"', "”": '"',
    "–": "-", "—": "-",
    " ": " ",
}


def repair_text(text: str) -> str:
    """Fix character-encoding damage in job text.

    - Decodes HTML entities (&amp;, &#39;, &#x27;, &rsquo;, ...).
    - Restores apostrophes corrupted into stray digits ("We 7re" -> "We're",
      "don7t" -> "don't", "company7s" -> "company's"), but only where the digit
      stands in for an apostrophe. Real numbers are left untouched.
    - Normalises curly quotes/dashes and non-breaking spaces to plain ASCII.

    Idempotent and safe on both plain text and simple HTML.
    """
    if not text:
        return text

    s = str(text)
    s = html.unescape(s)
    s = _APOSTROPHE_DIGIT_RE.sub(r"\1'\2", s)
    for bad, good in _PUNCT_MAP.items():
        s = s.replace(bad, good)
    return s


def clean_description(text: str) -> str:
    # Repair encoding damage FIRST (decode entities, fix apostrophe-as-digit
    # artifacts) so every downstream consumer works on clean text.
    text = repair_text(text or "")
    text = text.replace(" ", " ")
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize_whitespace(value: str) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def safe_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_remote_preferences(value: Any) -> list[str]:
    allowed = {"onsite", "hybrid", "remote"}

    if isinstance(value, list):
        raw_items = value
    elif isinstance(value, str):
        raw_items = [x.strip() for x in value.split(",")]
    else:
        raw_items = []

    cleaned = []
    for item in raw_items:
        item_l = normalize_whitespace(str(item)).lower()
        if item_l in allowed and item_l not in cleaned:
            cleaned.append(item_l)

    # Keep every supported work pattern (e.g. ["onsite", "hybrid"]) in canonical
    # order. Combinations are allowed so a role surfaces for candidates filtering
    # on either pattern.
    ordered = [x for x in ["onsite", "hybrid", "remote"] if x in cleaned]

    return ordered


def normalize_remote_days(value: Any) -> str:
    value = normalize_whitespace("" if value is None else str(value)).lower()
    if not value:
        return "not specified"
    if value == "not specified":
        return "not specified"
    if re.fullmatch(r"[0-5]", value):
        return value
    return "not specified"


def extract_json_object(text: str):
    """Return the first JSON object found in the model output, or None."""
    if not text:
        return None
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        snippet = text[start:end + 1]
        try:
            obj = json.loads(snippet)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    return None
