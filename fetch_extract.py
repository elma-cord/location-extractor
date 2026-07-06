import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Optional

import requests
from bs4 import BeautifulSoup


DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9,en-US;q=0.8",
}

# --- Headless-Chrome rendering (Playwright) for JavaScript-loaded pages ---
# Free: runs inside the GitHub Action. Only used when render=True (i.e. when the
# static fetch produced no usable location). Images/fonts/trackers are blocked
# for speed. Each worker thread gets its own browser (Playwright's sync API is
# not shared across threads).
PW_BLOCK_RESOURCE_TYPES = {"image", "font", "media"}
PW_BLOCK_URL_SUBSTRINGS = [
    "google-analytics.com", "googletagmanager.com", "doubleclick.net",
    "segment.com", "hotjar.com", "fullstory.com",
]
RENDER_TIMEOUT_SECONDS = 45
RENDER_WAIT_MS = 2500

_thread_local = threading.local()


BLOCK_PATTERNS = [
    r"access denied",
    r"forbidden",
    r"captcha",
    r"verify you are human",
    r"checking your browser",
    r"unusual traffic",
    r"cloudflare",
    r"perimeterx",
    r"datadome",
    r"akamai",
    r"incapsula",
    r"imperva",
]


@dataclass
class FetchResult:
    url: str
    final_url: str
    status_code: Optional[int]
    ok: bool
    blocked: bool
    text: str
    error: str
    rendered: bool = False


def _clean_text(text: str) -> str:
    text = text or ""
    text = text.replace("\u00a0", " ")
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _join_address_parts(parts: list[str]) -> str:
    clean = []
    for part in parts:
        part = (part or "").strip()
        if part and part not in clean:
            clean.append(part)
    return ", ".join(clean)


def _address_to_line(addr) -> str:
    if isinstance(addr, dict):
        return _join_address_parts([
            str(addr.get("addressLocality") or ""),
            str(addr.get("addressRegion") or ""),
            str(addr.get("addressCountry") or ""),
        ])
    if isinstance(addr, str):
        return addr.strip()
    return ""


def _extract_job_location_lines(job_location, out: list[str]) -> None:
    # A JobPosting.jobLocation is a Place (or list of Places); the address lives
    # under "address". We ONLY read locations from here - never from a top-level
    # Organization / hiringOrganization address (that is the company HQ, not the
    # role location).
    if isinstance(job_location, list):
        for item in job_location:
            _extract_job_location_lines(item, out)
        return

    if isinstance(job_location, dict):
        addr = job_location.get("address", job_location)
        if isinstance(addr, list):
            for a in addr:
                line = _address_to_line(a)
                if line:
                    out.append(f"Location: {line}")
        else:
            line = _address_to_line(addr)
            if line:
                out.append(f"Location: {line}")
    elif isinstance(job_location, str):
        out.append(f"Location: {job_location.strip()}")


def _collect_structured_lines(obj, out: list[str]) -> None:
    # Find JobPosting objects anywhere in the ld+json graph and extract ONLY
    # their role-specific location fields.
    if isinstance(obj, dict):
        types = obj.get("@type")
        type_list = types if isinstance(types, list) else [types]
        is_job_posting = any(str(t).lower() == "jobposting" for t in type_list if t)

        if is_job_posting:
            if "jobLocation" in obj:
                _extract_job_location_lines(obj.get("jobLocation"), out)

            alr = obj.get("applicantLocationRequirements")
            if isinstance(alr, str):
                out.append(f"Location: {alr}")
            elif isinstance(alr, dict) and alr.get("name"):
                out.append(f"Location: {alr.get('name')}")
            elif isinstance(alr, list):
                for a in alr:
                    if isinstance(a, dict) and a.get("name"):
                        out.append(f"Location: {a.get('name')}")

            workplace = obj.get("jobLocationType")
            if isinstance(workplace, str) and workplace.strip():
                out.append(f"Workplace type: {workplace.strip()}")

        for value in obj.values():
            if isinstance(value, (dict, list)):
                _collect_structured_lines(value, out)

    elif isinstance(obj, list):
        for item in obj:
            _collect_structured_lines(item, out)


def _extract_structured_text_from_html(soup: BeautifulSoup) -> str:
    lines = []

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
            _collect_structured_lines(data, lines)
        except Exception:
            continue

    return _clean_text("\n".join(lines))


def _extract_title_text(soup: BeautifulSoup) -> str:
    title = soup.title.string.strip() if soup.title and soup.title.string else ""
    h1 = ""
    h1_node = soup.find("h1")
    if h1_node:
        h1 = _clean_text(h1_node.get_text(" ", strip=True))
    bits = [x for x in [title, h1] if x]
    return _clean_text("\n".join(bits))


def _html_to_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")

    title_text = _extract_title_text(soup)
    structured_text = _extract_structured_text_from_html(BeautifulSoup(html or "", "html.parser"))

    meta_texts = []
    for attr in ("description", "og:description", "twitter:description"):
        node = soup.find("meta", attrs={"name": attr}) or soup.find("meta", attrs={"property": attr})
        if node and node.get("content"):
            meta_texts.append(node.get("content", ""))

    for tag in soup(["script", "style", "noscript", "svg", "img", "picture", "source"]):
        if tag.get("type") == "application/ld+json":
            continue
        tag.decompose()

    body_text = soup.get_text(separator="\n")

    combined_parts = []
    if title_text:
        combined_parts.append(title_text)
    if meta_texts:
        combined_parts.append("\n".join(meta_texts))
    if structured_text:
        combined_parts.append(structured_text)
    if body_text:
        combined_parts.append(body_text)

    return _clean_text("\n".join(combined_parts))


def _is_blocked(status_code: Optional[int], text: str) -> bool:
    if status_code in {401, 403, 406, 429, 503}:
        return True
    lower = (text or "").lower()
    return any(re.search(p, lower) for p in BLOCK_PATTERNS)


# --- Playwright: one headless browser per worker thread ---
def _get_thread_browser():
    if not hasattr(_thread_local, "browser"):
        from playwright.sync_api import sync_playwright
        _thread_local.pw = sync_playwright().start()
        _thread_local.browser = _thread_local.pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
    return _thread_local.browser


def _render_html(url: str, timeout: int) -> tuple[Optional[int], str, str]:
    browser = _get_thread_browser()
    page = browser.new_page()
    try:
        def route_handler(route, request):
            try:
                if (request.resource_type or "").lower() in PW_BLOCK_RESOURCE_TYPES:
                    route.abort()
                    return
                if any(s in (request.url or "").lower() for s in PW_BLOCK_URL_SUBSTRINGS):
                    route.abort()
                    return
                route.continue_()
            except Exception:
                try:
                    route.continue_()
                except Exception:
                    pass

        try:
            page.route("**/*", route_handler)
        except Exception:
            pass

        status = None
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            if resp:
                status = resp.status
        except Exception:
            pass

        page.wait_for_timeout(RENDER_WAIT_MS)
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass

        html = page.content()
        final_url = page.url
        return status, final_url, html
    finally:
        try:
            page.close()
        except Exception:
            pass


def fetch_job_page_text(
    url: str,
    timeout: int = 25,
    sleep_seconds: float = 0.0,
    render: bool = False,
) -> FetchResult:
    if not url:
        return FetchResult(
            url=url, final_url=url, status_code=None, ok=False,
            blocked=False, text="", error="missing_url", rendered=False,
        )

    try:
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

        if render:
            status_code, final_url, html = _render_html(url, max(timeout, RENDER_TIMEOUT_SECONDS))
            text = _html_to_text(html)
            blocked = _is_blocked(status_code, text)
            ok = (status_code is None or status_code < 400) and not blocked and bool(text)
            return FetchResult(
                url=url, final_url=final_url, status_code=status_code,
                ok=ok, blocked=blocked, text=text, error="", rendered=True,
            )

        response = requests.get(
            url,
            headers=DEFAULT_HEADERS,
            timeout=timeout,
            allow_redirects=True,
        )
        html = response.text or ""
        text = _html_to_text(html)
        blocked = _is_blocked(response.status_code, text)
        return FetchResult(
            url=url,
            final_url=str(response.url),
            status_code=response.status_code,
            ok=response.ok and not blocked,
            blocked=blocked,
            text=text,
            error="",
            rendered=False,
        )
    except Exception as exc:
        prefix = "render_fetch_error" if render else "fetch_error"
        return FetchResult(
            url=url, final_url=url, status_code=None, ok=False,
            blocked=False, text="", error=f"{prefix}: {exc}", rendered=render,
        )
