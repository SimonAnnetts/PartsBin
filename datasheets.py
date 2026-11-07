"""Automatic datasheet-link finder for PartsBin.

Runs a web search (DuckDuckGo's HTML endpoint, no API key), then downloads candidate
PDFs and only accepts one that really mentions the part number. This avoids storing
a wrong link such as a cable-gland catalogue for "4538".
"""
import base64
import html as _html
import json
import os
import re
import tempfile
import time
import urllib.parse
import urllib.request

import pinout

UA = "Mozilla/5.0 (X11; Linux x86_64) PartsBin/1.0"


def _norm(s):
    return re.sub(r"[^A-Z0-9]", "", s.upper())


def part_variants(*names):
    """Normalised spellings a datasheet might use: as given, minus trailing package letters.
    Very short variants (e.g. '4538') are dropped: they match unrelated documents."""
    out = []
    for name in names:
        n = _norm(name or "")
        for _ in range(4):
            if len(n) >= 5 and n not in out:
                out.append(n)
            if len(n) > 5 and n[-1].isalpha():
                n = n[:-1]
            else:
                break
    return out


def _get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if r.status != 200:
            raise RuntimeError(f"HTTP {r.status} (throttled?)")
        return r.read().decode("utf-8", "replace")


def _duckduckgo(query):
    html = _get("https://html.duckduckgo.com/html/?q=" + urllib.parse.quote_plus(query))
    urls = (urllib.parse.unquote(m[1]) for m in re.finditer(r"uddg=([^&\"]+)", html))
    return [u for u in urls if u.startswith("http") and "duckduckgo.com" not in u]


def _bing(query):
    html = _get("https://www.bing.com/search?q=" + urllib.parse.quote_plus(query))
    urls = []
    # only organic results (<li class="b_algo">): the rest of the page is nav, ads and promos
    blocks = html.split('class="b_algo"')[1:]
    for block in blocks:
        m = re.search(r'href="(https://www\.bing\.com/ck/a\?[^"]+)"', block)
        if not m:
            continue
        q = urllib.parse.parse_qs(urllib.parse.urlparse(_html.unescape(m[1])).query).get("u", [""])[0]
        if q.startswith("a1"):               # Bing wraps the real URL: 'a1' + urlsafe base64
            b = q[2:] + "=" * (-len(q[2:]) % 4)
            try:
                urls.append(base64.urlsafe_b64decode(b).decode())
            except Exception:
                pass
    return [u for u in urls if u.startswith("http") and "bing.com" not in u]


def _serper(key):
    """Google results via serper.dev (needs an API key; 2,500 free queries at signup)."""
    def run(query):
        req = urllib.request.Request(
            "https://google.serper.dev/search",
            data=json.dumps({"q": query, "num": 10}).encode(),
            headers={"X-API-KEY": key, "Content-Type": "application/json", "User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        return [x["link"] for x in data.get("organic", [])
                if str(x.get("link", "")).startswith("http")]
    return run


FALLBACK_ENGINES = (("DuckDuckGo", _duckduckgo), ("Bing", _bing))


def engines(serper_key=""):
    """Keyed API first when configured; scraping engines are best-effort fallbacks."""
    return ((("Serper", _serper(serper_key)),) if serper_key else ()) + FALLBACK_ENGINES


def search_urls(query, serper_key=""):
    """(urls, errors): result URLs in rank order from the first engine that answers,
    plus any engine failures (throttling/blocks) met on the way."""
    errors = []
    for name, engine in engines(serper_key):
        try:
            urls = list(dict.fromkeys(engine(query)))
        except Exception as e:
            errors.append(f"{name}: {e}")
            continue
        if urls:
            return urls, errors
    return [], errors


def _rank(url):
    """0 = direct .pdf, 1 = looks like a datasheet/PDF link (may redirect to one),
    2 = ordinary web page (never a PDF, so not worth downloading)."""
    u = url.lower()
    return (0 if u.split("?")[0].endswith(".pdf") else
            1 if any(k in u for k in ("/lit/", "datasheet", "pdf")) else 2)


def verify_pdf(path, variants, url=""):
    """True if the PDF names the part. Scanned PDFs (no text) pass only if the URL does."""
    if pinout.fitz is None:
        return any(v in _norm(url) for v in variants)
    try:
        doc = pinout.fitz.open(path)
        text = _norm(" ".join(doc[i].get_text() for i in range(min(len(doc), 5))))
    except Exception:
        return False
    if len(text) < 80:                    # no extractable text: a scan
        return any(v in _norm(url) for v in variants)
    return any(v in text for v in variants)


def find_datasheet(name, mpn="", mfr="", progress=lambda m: None, max_candidates=8,
                   serper_key=""):
    """Return (url, note) for a verified datasheet PDF, or (None, reason)."""
    variants = part_variants(mpn, name)
    if not variants:
        return None, "Part name too short to search for reliably"
    base = mpn or name
    queries = [f"{base} {mfr} datasheet filetype:pdf".replace("  ", " "),
               f"{base} datasheet filetype:pdf", f"{base} datasheet pdf"]
    if mpn and name and _norm(name) != _norm(mpn):
        queries.append(f"{name} datasheet filetype:pdf")
    errors, candidates = [], []
    for qi, q in enumerate(dict.fromkeys(queries)):
        progress(f"Searching: {q}")
        urls, errs = search_urls(q, serper_key)
        errors.extend(e for e in errs if e not in errors)
        candidates.extend(u for u in urls if u not in candidates)
        if sum(1 for u in candidates if _rank(u) < 2) >= 3:
            break
        time.sleep(1.2)                   # be polite; also avoids throttling
    candidates = sorted((u for u in candidates if _rank(u) < 2), key=_rank)  # stable sort
    tmp = tempfile.mkdtemp(prefix="partsbin_ds_")
    for i, url in enumerate(candidates[:max_candidates], 1):
        progress(f"Checking {i}/{min(len(candidates), max_candidates)}: {url[:70]}")
        dest = os.path.join(tmp, f"c{i}.pdf")
        try:
            path = pinout.download_pdf(url, dest, max_mb=12)
        except Exception:
            continue                       # not a PDF / blocked / too big
        if verify_pdf(path, variants, url):
            return url, "verified: PDF mentions " + base
    if not candidates:
        why = "; ".join(errors)
        hint = " Searches are being throttled; try again in a few minutes." if any(
            c in why for c in ("202", "429")) else ""
        return None, "No PDF links in the search results." + (f" ({why})" if why else "") + hint
    return None, f"Checked {min(len(candidates), max_candidates)} results; none was a PDF naming {base}"
