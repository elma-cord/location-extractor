import re


GENERIC_LOCATION_TOKENS = {
    "uk",
    "united",
    "kingdom",
    "great",
    "britain",
    "england",
    "scotland",
    "wales",
    "northern",
    "ireland",
    "unitedstates",
    "states",
    "usa",
    "us",
    "city",
    "centre",
    "center",
    "county",
    "region",
    "north",
    "south",
    "east",
    "west",
    "remote",
    "hybrid",
    "onsite",
    "on",
    "site",
    "office",
}

DISALLOWED_LOCATION_TERMS = [
    "philippines",
    "metro manila",
    "makati",
    "bonifacio global city",
    "taguig",
    "united states",
    "usa",
    "us only",
    "canada",
    "germany",
    "france",
    "spain",
    "italy",
    "netherlands",
    "belgium",
    "sweden",
    "norway",
    "denmark",
    "finland",
    "switzerland",
    "austria",
    "poland",
    "portugal",
    "india",
    "singapore",
    "japan",
    "china",
    "australia",
    "new south wales",
    "nsw",
    "north ryde",
    "apac",
    "latam",
    "africa",
    "america",
    "americas",
    "north america",
    "south america",
    "central america",
    "latin america",
]


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def lower_text(text: str) -> str:
    return normalize_text(text).lower()


def _canonical_location_key(text: str) -> str:
    text = lower_text(text)
    text = text.replace("&", " and ")
    text = re.sub(r"\bgreat britain\b", " united kingdom ", text)
    text = re.sub(r"\bu\.?k\.?\b", " united kingdom ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _location_specific_tokens(text: str) -> list[str]:
    key = _canonical_location_key(text)
    if not key:
        return []

    tokens = []
    for token in key.split():
        if token in GENERIC_LOCATION_TOKENS:
            continue
        if len(token) <= 2:
            continue
        tokens.append(token)
    return tokens


def is_explicitly_foreign_location_text(value: str) -> bool:
    value_l = lower_text(value)
    return any(term in value_l for term in DISALLOWED_LOCATION_TERMS)


def normalize_location_match(value: str, allowed_locations: list[str]) -> str:
    value_key = _canonical_location_key(value)
    if not value_key:
        return ""

    exact_map = {_canonical_location_key(x): x for x in allowed_locations}
    if value_key in exact_map:
        return exact_map[value_key]

    if is_explicitly_foreign_location_text(value):
        return ""

    value_specific_tokens = set(_location_specific_tokens(value))
    if not value_specific_tokens:
        return ""

    best_value = ""
    best_score = -10**9

    for loc in allowed_locations:
        loc_key = _canonical_location_key(loc)
        loc_specific_tokens = set(_location_specific_tokens(loc))

        overlap = value_specific_tokens & loc_specific_tokens
        if not overlap:
            continue

        score = 0

        if value_key == loc_key:
            score += 5000

        if value_specific_tokens == loc_specific_tokens:
            score += 2000

        if value_specific_tokens.issubset(loc_specific_tokens):
            score += 700

        score += len(overlap) * 250

        extra_candidate_tokens = len(loc_specific_tokens - value_specific_tokens)
        score -= extra_candidate_tokens * 160

        if "," in loc:
            score += 40

        if len(loc_specific_tokens) == len(value_specific_tokens):
            score += 250

        if score > best_score:
            best_score = score
            best_value = loc

    if best_score >= 300:
        return best_value

    return ""


def get_primary_text_window(text: str, max_chars: int = 12000) -> str:
    text = text or ""

    # First cut obvious related-job / footer / survey sections.
    cut_markers = [
        r"(?i)\bour hiring process\b",
        r"(?i)\bother jobs\b",
        r"(?i)\bsimilar jobs\b",
        r"(?i)\byou may also like\b",
        r"(?i)\bmore jobs\b",
        r"(?i)\brelated jobs\b",
        r"(?i)\bsee more jobs\b",
        r"(?i)\bview all jobs\b",
        r"(?i)\brecommended jobs\b",
        r"(?i)\bjobs for you\b",
        r"(?i)\bhow would you rate your experience\b",
    ]

    for pattern in cut_markers:
        m = re.search(pattern, text)
        if m:
            text = text[:m.start()]
            break

    trailing_card_patterns = [
        r"(?is)\bfor more information,\s*visit\s*\.?\s*\n+\s*location\s*\n",
        r"(?is)\bjoin our winning team today\..*?\n+\s*location\s*\n",
        r"(?is)\bfor more information,\s*visit\b.*?\n+\s*location\s*\n",
    ]

    for pattern in trailing_card_patterns:
        m = re.search(pattern, text)
        if m:
            text = text[:m.start()]
            break

    related_card_match = re.search(
        r"(?is)\n\s*location\s*\n\s*[^\n]+\n\s*category\s*\n\s*[^\n]+\n\s*posted date\b",
        text,
    )
    if related_card_match and related_card_match.start() > 2500:
        text = text[:related_card_match.start()]

    if len(text) > max_chars:
        text = text[:max_chars]

    return text.strip()


def has_disallowed_location_signal(text: str) -> bool:
    text_l = lower_text(get_primary_text_window(text))
    if not text_l:
        return False

    location_value_patterns = [
        r"(?im)^\s*location(?: city)?\s*[:\-]\s*(.+)$",
        r"(?im)^\s*job location\s*[:\-]\s*(.+)$",
        r"(?im)^\s*work location\s*[:\-]\s*(.+)$",
        r"(?im)^\s*city\s*[:\-]\s*(.+)$",
        r"(?im)^\s*based in\s+(.+)$",
        r"(?im)^\s*where you[’']ll work\s*[:\-]?\s*(.+)$",
    ]

    for pattern in location_value_patterns:
        for match in re.finditer(pattern, text_l):
            value = (match.group(1) or "").strip()
            if is_explicitly_foreign_location_text(value):
                return True

    disallowed_group = (
        r"philippines|metro manila|makati|bonifacio global city|taguig|"
        r"united states|usa|canada|australia|new south wales|nsw|north ryde|"
        r"germany|france|spain|italy|netherlands|belgium|sweden|norway|"
        r"denmark|finland|switzerland|austria|poland|portugal|india|"
        r"singapore|japan|china|americas|america"
    )

    strong_actual_location_patterns = [
        rf"\b(?:location|job location|work location|city|based in|role is based in|position is based in)\b.{{0,100}}\b(?:{disallowed_group})\b",
        rf"\b(?:{disallowed_group})\b.{{0,80}}\b(?:office based|onsite|on-site|hybrid|work location|job location)\b",
        r"\bnorth ryde\b.{0,40}\b(?:nsw|australia)\b",
        r"\b(?:nsw|new south wales)\b.{0,40}\baustralia\b",
    ]

    for pattern in strong_actual_location_patterns:
        if re.search(pattern, text_l, flags=re.IGNORECASE | re.DOTALL):
            return True

    remote_block_patterns = [
        "remote apac",
        "apac only",
        "asia only",
        "remote asia",
        "latam only",
        "remote latam",
        "africa only",
        "remote africa",
        "usa only",
        "us only",
        "canada only",
        "australia only",
        "remote australia",
    ]
    return any(term in text_l for term in remote_block_patterns)


def extract_remote_preferences(text: str) -> list[str]:
    text_l = lower_text(get_primary_text_window(text))
    found = []

    strong_remote = [
        r"(?im)^\s*remote\s*$",
        r"\bfully remote\b",
        r"\bremote-only\b",
        r"\bremote only\b",
        r"\bwe work remotely\b",
        r"\bmostly async from anywhere\b",
        r"\bwork remotely and mostly async from anywhere\b",
    ]
    strong_hybrid = [
        r"\bhybrid\b",
        r"\bflexibility for occasional home working\b",
        r"\bwork from home day\b",
        r"\bhome working by agreement\b",
        r"\boccasional home working\b",
    ]
    strong_onsite = [
        r"\bon-site\b",
        r"\bonsite\b",
        r"\bon site\b",
        r"\bbased at our .* office\b",
        r"\bf/t site\b",
    ]

    if any(re.search(p, text_l, flags=re.IGNORECASE | re.DOTALL) for p in strong_remote):
        found.append("remote")

    if any(re.search(p, text_l, flags=re.IGNORECASE | re.DOTALL) for p in strong_hybrid):
        found.append("hybrid")

    if any(re.search(p, text_l, flags=re.IGNORECASE | re.DOTALL) for p in strong_onsite):
        found.append("onsite")

    if "remote" in found:
        if "hybrid" in found and not re.search(r"\bhybrid\b|\boccasional home working\b|\bwork from home day\b", text_l):
            found = [x for x in found if x != "hybrid"]
        if "onsite" in found and not re.search(r"\bon[- ]site\b|\bf/t site\b", text_l):
            found = [x for x in found if x != "onsite"]

    ordered = []
    for item in ["onsite", "hybrid", "remote"]:
        if item in found and item not in ordered:
            ordered.append(item)
    return ordered


def extract_remote_days(text: str) -> str:
    text_l = lower_text(get_primary_text_window(text))
    if not text_l:
        return "not specified"

    if re.search(r"\bfully remote\b|\b100% remote\b|\bremote-only\b|\bremote only\b|\bmostly async from anywhere\b", text_l):
        return "not specified"

    patterns = [
        (r"\b(?:works?|working)\s+on\s+site\s+four\s+days\s+a\s+week.*?\bone\s+flexible\s+work\s+from\s+home\s+day\b", "1"),
        (r"\bteam usually works on site four days a week.*?\bone\s+flexible\s+work\s+from\s+home\s+day\b", "1"),
        (r"\b4\s+days?\s+(?:a\s+week\s+)?(?:on[- ]site|in\s+the\s+office|on\s+site)\b.*?\b1\s+(?:day\s+)?(?:wfh|work\s+from\s+home|from\s+home)\b", "1"),
        (r"\b1\s+day\s+(?:a\s+week\s+)?(?:wfh|work\s+from\s+home|from\s+home)\b", "1"),
        (r"\b2\s+days?\s+(?:a\s+week\s+)?(?:wfh|work\s+from\s+home|from\s+home)\b", "2"),
        (r"\b3\s+days?\s+(?:a\s+week\s+)?(?:wfh|work\s+from\s+home|from\s+home)\b", "3"),
        (r"\b1\s*-\s*2\s+days?\s+in\s+the\s+office\b", "3"),
        (r"\b2\s*-\s*3\s+days?\s+in\s+the\s+office\b", "2"),
        (r"\b1\s+day\s+in\s+the\s+office\b", "4"),
        (r"\b2\s+days?\s+in\s+the\s+office\b", "3"),
        (r"\b3\s+days?\s+in\s+the\s+office\b", "2"),
        (r"\b4\s+days?\s+in\s+the\s+office\b", "1"),
    ]

    for pattern, value in patterns:
        if re.search(pattern, text_l, flags=re.IGNORECASE | re.DOTALL):
            return value

    return "not specified"
