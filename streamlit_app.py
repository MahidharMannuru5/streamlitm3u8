import os
import re
import asyncio
import subprocess
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

# Put Playwright browser cache somewhere writable on Streamlit Cloud
BROWSER_CACHE = "/tmp/ms-playwright"
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", BROWSER_CACHE)

# ---------------- REGEX PATTERNS ----------------
M3U8_URL_RE = re.compile(r'https?://[^\s"\'<>]+\.m3u8(?:\?[^\s"\'<>]*)?', re.I)
MPD_URL_RE  = re.compile(r'https?://[^\s"\'<>]+\.mpd(?:\?[^\s"\'<>]*)?', re.I)
M4S_URL_RE  = re.compile(r'https?://[^\s"\'<>]+\.m4s(?:\?[^\s"\'<>]*)?', re.I)
SRC_ATTR_RE = re.compile(r'''src\s*=\s*["']([^"']+)["']''', re.I)

# ---------------- HELPERS ----------------
def absolutize(base: str, path: str) -> str:
    return urljoin(base, path)

def fetch_text(url: str, timeout: float = 20.0):
    with httpx.Client(headers=UA, follow_redirects=True, timeout=timeout) as c:
        r = c.get(url)
        r.raise_for_status()
        return r.text, str(r.url)

def find_media_urls_in_html(html: str, base_url: str):
    found = set()
    for regex in (M3U8_URL_RE, MPD_URL_RE, M4S_URL_RE):
        for u in regex.findall(html):
            found.add(u)
    for m in SRC_ATTR_RE.finditer(html):
        val = m.group(1)
        if any(ext in val.lower() for ext in (".m3u8", ".mpd", ".m4s")):
            found.add(absolutize(base_url, val))
    return list(dict.fromkeys(found))

def find_iframes(html: str, base_url: str):
    iframes = []
    for m in SRC_ATTR_RE.finditer(html):
        val = m.group(1)
        ctx = html[max(0, m.start() - 20):m.start() + 20].lower()
        if "<iframe" in ctx:
            iframes.append(absolutize(base_url, val))
    return list(dict.fromkeys(iframes))

def looks_like_master_m3u8(url: str) -> bool:
    try:
        with httpx.Client(headers=UA, follow_redirects=True, timeout=8) as c:
            r = c.get(url)
            return "#EXT-X-STREAM-INF" in r.text
    except Exception:
        return False

def looks_like_master_mpd(url: str) -> bool:
    try:
        with httpx.Client(headers=UA, follow_redirects=True, timeout=8) as c:
            r = c.get(url)
            t = r.text
            return "<Period" in t and "<AdaptationSet" in t
    except Exception:
        return False

def choose_best(candidates: list[str]) -> str | None:
    if not candidates:
        return None
    for u in candidates:
        if u.lower().endswith(".mpd") and looks_like_master_mpd(u):
            return u
    for u in candidates:
        if u.lower().endswith(".m3u8") and looks_like_master_m3u8(u):
            return u
    return candidates[0]

def find_media_static(page_url: str, iframe_depth: int = 0):
    try:
        html, final_url = fetch_text(page_url)
    except Exception as e:
        return None, [], f"Fetch failed: {e}"

    all_candidates = find_media_urls_in_html(html, final_url)
    iframes = find_iframes(html, final_url)[:10]
    seen = set()
    for _ in range(iframe_depth):
        next_iframes = []
        for f in iframes:
            if f in seen:
                continue
            seen.add(f)
            try:
                ihtml, ifinal = fetch_text(f)
            except Exception:
                continue
            all_candidates += find_media_urls_in_html(ihtml, ifinal)
            next_iframes += find_iframes(ihtml, ifinal)[:10]
        iframes = next_iframes

    deduped = list(dict.fromkeys(all_candidates))
    return choose_best(deduped), deduped, None

# ---------- Auto-install Playwright browsers (once per container) ----------
@st.cache_resource(show_spinner=False)
def ensure_playwright_browser():
    """
    Installs Chromium for Playwright into /tmp if it's missing.
    Cached so it runs only once per container/session on Streamlit Cloud.
    """
    chromium_dir = os.path.join(BROWSER_CACHE, "chromium")
    if os.path.exists(chromium_dir) and os.listdir(chromium_dir):
        return True
    try:
        # plain install (no --with-deps; apt-get is not available on Streamlit Cloud)
        subprocess.run(
            ["playwright", "install", "chromium"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return True
    except Exception as e:
        st.error(f"Playwright install failed: {e}")
        return False

# ---------------- PLAYWRIGHT (JS RENDERING) ----------------
async def find_media_playwright(url: str, wait_time: float = 5.0):
    if not ensure_playwright_browser():
        return None, [], "Playwright browser install failed."

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
    return choose_best(candidates), candidates, None

# ---------------- UI ----------------
st.title("🎯 Media Stream Finder")
st.caption("Find .m3u8, .mpd, and .m4s URLs (HLS & DASH). Static HTML by default; toggle JS mode if needed.")

url = st.text_input("Page URL", placeholder="https://example.com/watch/123")
col1, col2 = st.columns(2)
with col1:
    # Default to 0 to keep it simple; raise to 1–2 only if the site nests players in iframes.
    depth = st.selectbox("Iframe depth (0 = off)", options=[0, 1, 2], index=0,
                         help="How many layers of embedded players to scan. If unsure, leave at 0.")
with col2:
    use_js = st.checkbox("Enable JavaScript (Playwright)", value=False,
                         help="Render JS with headless Chromium. Slower; use only if static mode finds nothing.")

if st.button("Find Streams", type="primary") and url:
    with st.spinner("Scanning…"):
        if use_js:
            try:
                best, candidates, err = asyncio.run(find_media_playwright(url))
            except Exception as e:
                best, candidates, err = None, [], f"Playwright failed: {e}"
        else:
            best, candidates, err = find_media_static(url, int(depth))

    if err:
        st.error(err)
    elif not candidates:
        st.warning("No media URLs found.")
    else:
        st.success("Scan complete!")
        if best:
            st.subheader("Best (Master) URL")
            st.code(best, language=None)
            st.download_button("Copy as text", data=best, file_name="stream_url.txt", mime="text/plain")
        else:
            st.info("No verified master manifest found; showing first candidate.")
            st.code(candidates[0], language=None)
        with st.expander("All candidates found"):
            for u in candidates:
                st.write(u)
