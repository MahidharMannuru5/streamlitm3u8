import re
import asyncio
from urllib.parse import urljoin

import httpx
import streamlit as st
from playwright.async_api import async_playwright

# ---------------- CONFIG ----------------

st.set_page_config(page_title="🎯 Media Stream Finder", page_icon="🎯", layout="centered")

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
    )
}

# ---------------- REGEX PATTERNS ----------------

M3U8_URL_RE = re.compile(r'https?://[^\s"\'<>]+\.m3u8(?:\?[^\s"\'<>]*)?', re.I)
MPD_URL_RE  = re.compile(r'https?://[^\s"\'<>]+\.mpd(?:\?[^\s"\'<>]*)?', re.I)
M4S_URL_RE  = re.compile(r'https?://[^\s"\'<>]+\.m4s(?:\?[^\s"\'<>]*)?', re.I)
SRC_ATTR_RE = re.compile(r'''src\s*=\s*["']([^"']+)["']''', re.I)

# ---------------- HELPERS ----------------

def absolutize(base: str, path: str) -> str:
    """Join relative URLs to the base page."""
    return urljoin(base, path)

def fetch_text(url: str, timeout: float = 20.0):
    """Fetch static HTML text using httpx."""
    with httpx.Client(headers=UA, follow_redirects=True, timeout=timeout) as c:
        r = c.get(url)
        r.raise_for_status()
        return r.text, str(r.url)

def find_media_urls_in_html(html: str, base_url: str):
    """Extract .m3u8, .mpd, .m4s URLs (absolute or relative) from HTML."""
    found = set()
    # Absolute URLs
    for regex in (M3U8_URL_RE, MPD_URL_RE, M4S_URL_RE):
        for u in regex.findall(html):
            found.add(u)
    # Relative src= attributes
    for m in SRC_ATTR_RE.finditer(html):
        val = m.group(1)
        if any(ext in val.lower() for ext in (".m3u8", ".mpd", ".m4s")):
            found.add(absolutize(base_url, val))
    return list(dict.fromkeys(found))  # dedupe, preserve order

def find_iframes(html: str, base_url: str):
    """Extract iframe URLs."""
    iframes = []
    for m in SRC_ATTR_RE.finditer(html):
        val = m.group(1)
        ctx = html[max(0, m.start() - 20):m.start() + 20].lower()
        if "<iframe" in ctx:
            iframes.append(absolutize(base_url, val))
    return list(dict.fromkeys(iframes))

def looks_like_master_m3u8(url: str) -> bool:
    """Check if a .m3u8 playlist looks like a master manifest."""
    try:
        with httpx.Client(headers=UA, follow_redirects=True, timeout=8) as c:
            r = c.get(url)
            r.raise_for_status()
            return "#EXT-X-STREAM-INF" in r.text
    except Exception:
        return False

def looks_like_master_mpd(url: str) -> bool:
    """Check if an .mpd manifest looks like a master (top-level DASH)."""
    try:
        with httpx.Client(headers=UA, follow_redirects=True, timeout=8) as c:
            r = c.get(url)
            r.raise_for_status()
            text = r.text
            return "<Period" in text and "<AdaptationSet" in text
    except Exception:
        return False

def choose_best(candidates: list[str]) -> str | None:
    """Pick the best candidate among found URLs."""
    if not candidates:
        return None

    # Prefer verified master MPD
    for u in candidates:
        if u.lower().endswith(".mpd") and looks_like_master_mpd(u):
            return u

    # Prefer verified master M3U8
    masters = [u for u in candidates if "master" in u.lower() and u.lower().endswith(".m3u8")]
    for u in masters + candidates:
        if u.lower().endswith(".m3u8") and looks_like_master_m3u8(u):
            return u

    return candidates[0]

def find_media_static(page_url: str, iframe_depth: int = 1, max_iframes_per_level: int = 10):
    """Recursively scan a page and its iframes for streaming URLs (static HTML)."""
    try:
        html, final_url = fetch_text(page_url)
    except Exception as e:
        return None, [], f"Fetch failed: {e}"

    all_candidates = find_media_urls_in_html(html, final_url)
    frontier = find_iframes(html, final_url)[:max_iframes_per_level]
    seen = set()

    for _ in range(iframe_depth):
        next_frontier = []
        for iframe_url in frontier:
            if iframe_url in seen:
                continue
            seen.add(iframe_url)
            try:
                ihtml, ifinal = fetch_text(iframe_url)
            except Exception:
                continue
            all_candidates += find_media_urls_in_html(ihtml, ifinal)
            next_frontier += find_iframes(ihtml, ifinal)[:max_iframes_per_level]
        frontier = next_frontier

    deduped = list(dict.fromkeys(all_candidates))
    best = choose_best(deduped)
    return best, deduped, None

# ---------------- PLAYWRIGHT (JS RENDERING) ----------------

async def find_media_playwright(url: str, wait_time: float = 5.0):
    """Use Playwright to fetch rendered HTML and extract media URLs."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--headless=new", "--no-sandbox", "--disable-dev-shm-usage"]
        )
        page = await browser.new_page()
        await page.goto(url, wait_until="networkidle", timeout=30000)
        await asyncio.sleep(wait_time)
        html = await page.content()
        final_url = page.url
        await browser.close()

    candidates = find_media_urls_in_html(html, final_url)
    best = choose_best(candidates)
    return best, candidates, None

# ---------------- STREAMLIT UI ----------------

st.title("🎯 Media Stream Finder")
st.caption(
    "Paste a webpage URL — I'll scan for **.m3u8**, **.mpd**, and **.m4s** URLs, "
    "and identify the best (master) manifest. Works with static and JS-rendered pages."
)

url = st.text_input("Page URL", placeholder="https://example.com/watch/123")
col1, col2, col3 = st.columns([1, 1, 2])
with col1:
    depth = st.selectbox("Iframe depth", options=[0, 1, 2], index=1, help="Scan embedded players inside iframes.")
with col2:
    use_js = st.checkbox("Enable JavaScript (Playwright)", value=False, help="Use headless Chromium to render JS-heavy sites.")
with col3:
    run = st.button("Find Streams", type="primary")

st.divider()

if run and url:
    with st.spinner("Scanning…"):
        if use_js:
            try:
                best, candidates, err = asyncio.run(find_media_playwright(url))
            except Exception as e:
                err = f"Playwright failed: {e}"
                best, candidates = None, []
        else:
            best, candidates, err = find_media_static(url, iframe_depth=int(depth))

    if err:
        st.error(err)
    elif not candidates:
        st.warning("No media URLs found. The site may build URLs dynamically via JavaScript.")
    else:
        st.success("✅ Scan complete!")
        st.subheader("🎬 Best (Master) Stream URL")
        if best:
            st.code(best, language=None)
            st.download_button("Copy as text", data=best, file_name="stream_url.txt", mime="text/plain")
        else:
            st.info("No verified master manifest found; showing first candidate instead:")
            st.code(candidates[0], language=None)

        with st.expander("All candidates found"):
            for u in candidates:
                st.write(u)

st.markdown("""
**Notes**
- The static scanner works for most sites with direct media links.  
- The JavaScript mode uses **Playwright + headless Chromium** (`--headless=new`) for modern Chrome compatibility.  
- JS mode is slower but necessary for sites that load streams dynamically.  
- To set up locally:
  ```bash
  pip install -r requirements.txt
  playwright install chromium
  streamlit run streamlit_app.py
