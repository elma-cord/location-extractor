import html
import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

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
_ATS_HEADERS = {**DEFAULT_HEADERS, "Accept": "application/json"}

# --- Headless-Chrome rendering (Playwright) for JavaScript-loaded pages ---
PW_BLOCK_RESOURCE_TYPES = {"image", "font", "media"}
PW_BLOCK_URL_SUBSTRINGS = [
    "google-analytics.com", "googletagmanager.com", "doubleclick.net",
    "segment.com", "hotjar.com", "fullstory.com",
]
RENDER_TIMEOUT_SECONDS = 45
RENDER_WAIT_MS = 2500

# --- ATS API fast-paths (free, exact) ---
ATS_TIMEOUT_SECONDS = 20
ATS_CONTENT_MAX_CHARS = 15000

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
    source: str = "static"


def _clean_text(text: str) -> str:
    text = text or ""
    text = text.replace("\u00a0", " ")
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _join_address_parts(parts) -> str:
    clean = []
    for part in parts:
        part = str(part or "").strip()
        if part and part.lower() not in ("none", "null") and part not in clean:
            clean.append(part)
    return ", ".join(clean)


def _address_to_line(addr) -> str:
    if isinstance(addr, dict):
        return _join_address_parts([
            addr.get("addressLocality"),
            addr.get("addressRegion"),
            addr.get("addressCountry"),
        ])
    if isinstance(addr, str):
        return addr.strip()
    return ""


def _extract_job_location_lines(job_location, out: list) -> None:
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


def _collect_structured_lines(obj, out: list) -> None:
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


def _html_to_text(html_doc: str) -> str:
    soup = BeautifulSoup(html_doc or "", "html.parser")

    title_text = _extract_title_text(soup)
    structured_text = _extract_structured_text_from_html(BeautifulSoup(html_doc or "", "html.parser"))

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


def _strip_html_fragment(fragment: str) -> str:
    if not fragment:
        return ""
    if "<" in fragment:
        fragment = BeautifulSoup(fragment, "html.parser").get_text("\n")
    return _clean_text(fragment)


def _is_blocked(status_code: Optional[int], text: str) -> bool:
    if status_code in {401, 403, 406, 429, 503}:
        return True
    lower = (text or "").lower()
    return any(re.search(p, lower) for p in BLOCK_PATTERNS)


# =====================================================================
# ATS API fast-paths: each returns a text blob with an explicit
# "Location:" line, or None. All are wrapped by _fetch_ats_text so any
# failure falls through to static -> render.
# =====================================================================
def _build_ats_text(title, location, workplace, content) -> Optional[str]:
    parts = []
    if location:
        parts.append(f"Location: {location}")
    if workplace:
        parts.append(f"Workplace type: {workplace}")
    if title:
        parts.append(str(title))
    body = _strip_html_fragment(content or "")
    if body:
        parts.append(body)
    text = _clean_text("\n\n".join(parts))
    if not text:
        return None
    return text[:ATS_CONTENT_MAX_CHARS]


def _ats_workday(url: str) -> Optional[str]:
    p = urlparse(url)
    host = p.netloc
    segs = [s for s in p.path.split("/") if s]
    if "job" not in segs:
        return None
    ji = segs.index("job")

    if "myworkdaysite.com" in host:
        # /{lang}/recruiting/{tenant}/{site}/job/...
        if "recruiting" not in segs:
            return None
        ri = segs.index("recruiting")
        if ri + 2 >= ji:
            return None
        tenant, site = segs[ri + 1], segs[ri + 2]
    else:
        # {tenant}.wdN.myworkdayjobs.com/{lang}/{site}/job/...
        tenant = host.split(".")[0]
        if ji == 0:
            return None
        site = segs[ji - 1]

    jobpath = "/".join(segs[ji:])
    cxs = f"https://{host}/wday/cxs/{tenant}/{site}/{jobpath}"
    headers = {**_ATS_HEADERS, "Referer": url}

    api = requests.get(cxs, headers=headers, timeout=ATS_TIMEOUT_SECONDS)
    if not api.ok:
        return None
    info = (api.json() or {}).get("jobPostingInfo") or {}
    loc = info.get("location") or ""
    addl = [a for a in (info.get("additionalLocations") or []) if isinstance(a, str) and a.strip()]
    if addl:
        loc = f"{loc} / {', '.join(addl)}" if loc else ", ".join(addl)
    return _build_ats_text(info.get("title"), loc, info.get("remoteType") or "",
                           html.unescape(info.get("jobDescription") or ""))


def _ats_greenhouse(url: str) -> Optional[str]:
    jid = token = None
    m_direct = re.search(r"greenhouse\.io/(?:embed/[^/?#]+\?for=)?([A-Za-z0-9_-]+)/jobs/(\d+)", url)
    if m_direct:
        token, jid = m_direct.group(1), m_direct.group(2)
    m_jid = re.search(r"[?&]gh_jid=(\d+)", url)
    if m_jid:
        jid = m_jid.group(1)
    if jid and not token:
        try:
            page = requests.get(url, headers=DEFAULT_HEADERS, timeout=ATS_TIMEOUT_SECONDS)
            tok = re.search(r"job_board/js\?for=([A-Za-z0-9_-]+)", page.text or "")
            if tok:
                token = tok.group(1)
        except Exception:
            return None
    if not (token and jid):
        return None
    for api_host in ("boards-api.greenhouse.io", "boards-api.eu.greenhouse.io"):
        try:
            api = requests.get(f"https://{api_host}/v1/boards/{token}/jobs/{jid}",
                               headers=_ATS_HEADERS, timeout=ATS_TIMEOUT_SECONDS)
            if api.ok:
                d = api.json()
                loc = (d.get("location") or {}).get("name") or ""
                if not loc:
                    offices = [o.get("name") for o in (d.get("offices") or []) if isinstance(o, dict) and o.get("name")]
                    loc = offices[0] if offices else ""
                return _build_ats_text(d.get("title"), loc, "", html.unescape(d.get("content") or ""))
        except Exception:
            continue
    return None


def _ats_lever(url: str) -> Optional[str]:
    m = re.search(r"lever\.co/([^/?#]+)/([0-9a-fA-F-]{6,})", url)
    if not m:
        return None
    company, jid = m.group(1), m.group(2)
    api = requests.get(f"https://api.lever.co/v0/postings/{company}/{jid}",
                       headers=_ATS_HEADERS, timeout=ATS_TIMEOUT_SECONDS)
    if not api.ok:
        return None
    d = api.json()
    if isinstance(d, list):
        d = d[0] if d else {}
    cats = d.get("categories") or {}
    loc = cats.get("location") or (cats.get("allLocations") or [""])[0] or d.get("country") or ""
    content = d.get("descriptionPlain") or d.get("description") or ""
    return _build_ats_text(d.get("text"), loc, d.get("workplaceType") or "", content)


def _ats_ashby(url: str) -> Optional[str]:
    m = re.search(r"ashbyhq\.com/([^/?#]+)/([0-9a-fA-F-]{6,})", url)
    if not m:
        return None
    org, jid = m.group(1), m.group(2)
    api = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{org}?includeCompensation=false",
                       headers=_ATS_HEADERS, timeout=max(ATS_TIMEOUT_SECONDS, 25))
    if not api.ok:
        return None
    jobs = (api.json() or {}).get("jobs") or []
    job = next((j for j in jobs if j.get("id") == jid or jid in (j.get("jobUrl") or "")), None)
    if not job:
        return None
    loc = job.get("location") or ""
    secondary = ", ".join([s for s in (job.get("secondaryLocations") or []) if isinstance(s, str)])
    if secondary:
        loc = f"{loc} / {secondary}" if loc else secondary
    workplace = job.get("workplaceType") or ("remote" if job.get("isRemote") else "")
    return _build_ats_text(job.get("title"), loc, workplace, job.get("descriptionPlain") or "")


def _ats_smartrecruiters(url: str) -> Optional[str]:
    m = re.search(r"smartrecruiters\.com/([^/?#]+)/(\d+)", url)
    if not m:
        return None
    company, pid = m.group(1), m.group(2)
    api = requests.get(f"https://api.smartrecruiters.com/v1/companies/{company}/postings/{pid}",
                       headers=_ATS_HEADERS, timeout=ATS_TIMEOUT_SECONDS)
    if not api.ok:
        return None
    d = api.json()
    loc = d.get("location") or {}
    loc_str = _join_address_parts([loc.get("city"), loc.get("region"), str(loc.get("country") or "").upper()])
    workplace = "remote" if loc.get("remote") else ""
    sections = ((d.get("jobAd") or {}).get("sections") or {})
    content_bits = []
    for key in ("jobDescription", "qualifications", "additionalInformation"):
        txt = (sections.get(key) or {}).get("text") or ""
        if txt:
            content_bits.append(txt)
    return _build_ats_text(d.get("name"), loc_str, workplace, "\n".join(content_bits))


def _ats_workable(url: str) -> Optional[str]:
    m = re.search(r"apply\.workable\.com/([^/?#]+)/j/([A-Za-z0-9]+)", url)
    if not m:
        return None
    acct, sc = m.group(1), m.group(2)
    api = requests.get(f"https://apply.workable.com/api/v2/accounts/{acct}/jobs/{sc}",
                       headers=_ATS_HEADERS, timeout=ATS_TIMEOUT_SECONDS)
    if not api.ok:
        return None
    d = api.json()
    loc = d.get("location") or {}
    loc_str = _join_address_parts([loc.get("city"), loc.get("region"), loc.get("country")])
    workplace = d.get("workplace") or ("remote" if d.get("remote") else "")
    return _build_ats_text(d.get("title"), loc_str, workplace, d.get("description") or "")


def _ats_oracle(url: str) -> Optional[str]:
    p = urlparse(url)
    host = p.netloc
    m = re.search(r"/job/(\d+)", p.path)
    if not m:
        return None
    jid = m.group(1)
    api = requests.get(
        f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails/{jid}?expand=all&onlyData=true",
        headers=_ATS_HEADERS, timeout=ATS_TIMEOUT_SECONDS,
    )
    if not api.ok:
        return None
    d = api.json() or {}
    loc = d.get("PrimaryLocation") or ""
    workplace = d.get("WorkplaceType") or d.get("WorkplaceTypeCode") or ""
    content = ""
    for key in ("ExternalDescriptionStr", "ExternalResponsibilitiesStr",
                "ExternalQualificationsStr", "ShortDescriptionStr"):
        val = d.get(key)
        if val:
            content += "\n" + str(val)
    return _build_ats_text(d.get("Title"), loc, workplace, html.unescape(content))


def _ats_bamboohr(url: str) -> Optional[str]:
    m = re.search(r"https?://([^.]+)\.bamboohr\.com/careers/(\d+)", url)
    if not m:
        return None
    comp, jid = m.group(1), m.group(2)
    api = requests.get(f"https://{comp}.bamboohr.com/careers/{jid}/detail",
                       headers=_ATS_HEADERS, timeout=ATS_TIMEOUT_SECONDS)
    if not api.ok:
        return None
    jo = ((api.json() or {}).get("result") or {}).get("jobOpening") or {}
    loc = jo.get("location") or {}
    loc_str = _join_address_parts([loc.get("city"), loc.get("state"), loc.get("addressCountry")])
    title = jo.get("jobOpeningName") or jo.get("title") or ""
    content = jo.get("description") or jo.get("jobDescription") or ""
    return _build_ats_text(title, loc_str, "", content)


def _ats_recruitee(url: str) -> Optional[str]:
    m = re.search(r"https?://([^.]+)\.recruitee\.com/o/([^/?#]+)", url)
    if not m:
        return None
    comp, slug = m.group(1), m.group(2)
    api = requests.get(f"https://{comp}.recruitee.com/api/offers/",
                       headers=_ATS_HEADERS, timeout=ATS_TIMEOUT_SECONDS)
    if not api.ok:
        return None
    offers = (api.json() or {}).get("offers") or []
    off = next((o for o in offers if o.get("slug") == slug), None) \
        or next((o for o in offers if slug in (o.get("slug") or "")), None)
    if not off:
        return None
    loc = off.get("location") or _join_address_parts([off.get("city"), off.get("country")])
    workplace = "remote" if off.get("remote") else ""
    return _build_ats_text(off.get("title"), loc, workplace, off.get("description") or "")


def _fetch_ats_text(url: str) -> Optional[str]:
    host = (urlparse(url).netloc or "").lower()
    try:
        if "myworkdayjobs.com" in host or "myworkdaysite.com" in host:
            return _ats_workday(url)
        if "gh_jid=" in url or "greenhouse.io" in host:
            return _ats_greenhouse(url)
        if "lever.co" in host:
            return _ats_lever(url)
        if "ashbyhq.com" in host:
            return _ats_ashby(url)
        if "smartrecruiters.com" in host:
            return _ats_smartrecruiters(url)
        if "workable.com" in host:
            return _ats_workable(url)
        if "oraclecloud.com" in host:
            return _ats_oracle(url)
        if "bamboohr.com" in host:
            return _ats_bamboohr(url)
        if "recruitee.com" in host:
            return _ats_recruitee(url)
    except Exception:
        return None
    return None


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


def _render_html(url: str, timeout: int):
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

        html_doc = page.content()
        final_url = page.url
        return status, final_url, html_doc
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
            blocked=False, text="", error="missing_url", rendered=False, source="none",
        )

    try:
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

        if render:
            status_code, final_url, html_doc = _render_html(url, max(timeout, RENDER_TIMEOUT_SECONDS))
            text = _html_to_text(html_doc)
            blocked = _is_blocked(status_code, text)
            ok = (status_code is None or status_code < 400) and not blocked and bool(text)
            return FetchResult(
                url=url, final_url=final_url, status_code=status_code,
                ok=ok, blocked=blocked, text=text, error="", rendered=True, source="render",
            )

        # 1) ATS API fast-path
        ats_text = _fetch_ats_text(url)
        if ats_text:
            return FetchResult(
                url=url, final_url=url, status_code=200, ok=True,
                blocked=False, text=ats_text, error="", rendered=False, source="ats",
            )

        # 2) Plain static fetch
        response = requests.get(url, headers=DEFAULT_HEADERS, timeout=timeout, allow_redirects=True)
        html_doc = response.text or ""
        text = _html_to_text(html_doc)
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
            source="static",
        )
    except Exception as exc:
        prefix = "render_fetch_error" if render else "fetch_error"
        return FetchResult(
            url=url, final_url=url, status_code=None, ok=False,
            blocked=False, text="", error=f"{prefix}: {exc}", rendered=render, source="error",
        )
