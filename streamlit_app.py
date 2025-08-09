import re
from urllib.parse import urljoin
import httpx
import streamlit as st

st.set_page_config(page_title="M3U8 Finder", page_icon="🎯", layout="centered")

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "KHTML, like Gecko) Chrome/124 Safari/537.36"}

M3U8_URL_RE = re.compile(r'https?://[^\s"\'<>]+\.m3u8(?:\?[^\s"\'<>]*)?', re.I)
SRC_ATTR_RE = re.compile(r'''src\s*=\s*["']([^"']+)["']''', re.I)

def absolutize(base: str, path: str) -> str:
    return urljoin(base, path)

def fetch_text(url: str, timeout: float = 20.0):
    with httpx.Client(headers=UA, follow_redirects=True, timeout=timeout) as c:
        r = c.get(url)
        r.raise_for_status()
        return r.text, str(r.url)

def find_m3u8_in_html(html: str, base_url: str):
    found = set()
    # Absolute .m3u8
    for u in M3U8_URL_RE.findall(html):
        found.add(u)
    # Any src="..." that contains .m3u8 (may be relative)
    for m in SRC_ATTR_RE.finditer(html):
        val = m.group(1)
        if ".m3u8" in val.lower():
            found.add(absolutize(base_url, val))
    return list(dict.fromkeys(found))  # dedupe, keep order

def find_iframes(html: str, base_url: str):
    iframes = []
    for m in SRC_ATTR_RE.finditer(html):
        val = m.group(1)
        # quick-and-dirty context check for <iframe ... src="...">
        ctx = html[max(0, m.start()-20):m.start()+20].lower()
        if "<iframe" in ctx:
            iframes.append(absolutize(base_url, val))
    return list(dict.fromkeys(iframes))

def looks_like_master(url: str) -> bool:
    try:
        with httpx.Client(headers=UA, follow_redirects=True, timeout=8) as c:
            r = c.get(url)
            r.raise_for_status()
            # Master playlists contain variant lines:
            return "#EXT-X-STREAM-INF" in r.text
    except Exception:
        return False

def choose_best(candidates: list[str]) -> str | None:
    if not candidates:
        return None
    masters = [u for u in candidates if "master" in u.lower()]
    for u in masters + candidates:
        if looks_like_master(u):
            return u
    return candidates[0]

def find_m3u8_deep(page_url: str, iframe_depth: int = 1, max_iframes_per_level: int = 10):
    try:
        html, final_url = fetch_text(page_url)
    except Exception as e:
        return None, [], f"Fetch failed: {e}"

    all_candidates = []
    all_candidates += find_m3u8_in_html(html, final_url)

    # scan iframes breadth-first up to iframe_depth
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
            all_candidates += find_m3u8_in_html(ihtml, ifinal)
            next_frontier += find_iframes(ihtml, ifinal)[:max_iframes_per_level]
        frontier = next_frontier

    deduped = list(dict.fromkeys(all_candidates))
    best = choose_best(deduped)
    return best, deduped, None

# ---------------- UI ----------------

st.title("🎯 Master M3U8 Finder")
st.caption("Paste a page URL. I’ll return the best (master) .m3u8 and any other candidates I can find.")

url = st.text_input("Page URL", placeholder="https://example.com/watch/123")
col1, col2, col3 = st.columns([1,1,2])
with col1:
    depth = st.selectbox("Iframe depth", options=[0,1,2], index=1, help="Scan embedded players inside iframes.")
with col2:
    run = st.button("Find M3U8", type="primary")

st.divider()

if run and url:
    with st.spinner("Scanning…"):
        best, candidates, err = find_m3u8_deep(url, iframe_depth=int(depth))
    if err:
        st.error(err)
    elif not candidates:
        st.warning("No .m3u8 links found. The site may build URLs via JS at runtime or block bots.")
    else:
        st.success("Done")
        st.subheader("Best (master) URL")
        if best:
            st.code(best, language=None)
            st.download_button("Copy as text", data=best, file_name="m3u8.txt", mime="text/plain")
        else:
            st.info("Couldn't verify a master playlist; showing first candidate instead:")
            st.code(candidates[0], language=None)

        with st.expander("All candidates"):
            for u in candidates:
                st.write(u)

st.markdown(
    """
**Notes**
- This simple version scans static HTML (and iframes). If the player constructs the URL only via JavaScript/XHR,
  Streamlit Cloud won’t see it without a headless browser.  
- Need JS-heavy support? Deploy on Render/Railway with **Playwright** and a headless Chromium.
"""
)

