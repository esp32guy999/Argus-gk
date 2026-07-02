"""NEC (NFPA 70, 2023) lookup engine for the Forge "NEC" tab.

Pipeline for a natural-language question:
  1. Router LLM maps the question -> candidate NEC section numbers + verbatim search phrases.
  2. Ground each section against the PDF via a prebuilt section->page index (kills hallucination:
     if the section number isn't actually printed in the book, it's dropped).
  3. Render the hit page(s) to PNG with the phrases highlighted (PyMuPDF).
  4. OCR the hit page with tesseract for CLEAN text — the PDF's own text layer is ~78% garbled
     (broken font/ToUnicode), fine for exact search_for but not for feeding an LLM.
  5. Translate the clean text to plain English (LLM).
Multiple hits -> the UI pages through them with side arrows.
"""
import os, re, json, threading, subprocess, functools, asyncio
import httpx, fitz

_HERE = os.path.dirname(os.path.abspath(__file__))
PDF_PATH   = os.path.join(_HERE, "..", "data", "nec", "nec2023.pdf")
INDEX_PATH = os.path.join(_HERE, "..", "data", "nec", "section_index.json")
MODEL_URL  = os.environ.get("ARGUS_MODEL_URL", "http://localhost:9090/v1")
NEC_MODEL  = os.environ.get("ARGUS_NEC_MODEL", "gemma4-26b")  # non-reasoning: reliable JSON + NEC knowledge

_doc = None
_doc_lock = threading.Lock()
_index = None

# ── PDF / rendering ─────────────────────────────────────────────────────────
def doc():
    global _doc
    if _doc is None:
        _doc = fitz.open(PDF_PATH)
    return _doc

def page_count():
    return doc().page_count

def render_page(idx, highlights=None, dpi=160):
    """PNG bytes of page `idx` with each phrase in `highlights` highlighted (yellow)."""
    d = doc()
    idx = max(0, min(int(idx), d.page_count - 1))
    with _doc_lock:                       # we mutate the shared doc (add/remove annots)
        page = d[idx]
        added = []
        for phrase in (highlights or []):
            phrase = (phrase or "").strip()
            if len(phrase) < 2:
                continue
            try:
                for r in page.search_for(phrase)[:16]:
                    a = page.add_highlight_annot(r)
                    a.update()
                    added.append(a)
            except Exception:
                pass
        pix = page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72))
        data = pix.tobytes("png")
        for a in added:
            try: page.delete_annot(a)
            except Exception: pass
    return data

@functools.lru_cache(maxsize=128)
def ocr_page(idx):
    """Clean text for one page via tesseract (bypasses the garbled embedded text)."""
    d = doc()
    idx = max(0, min(int(idx), d.page_count - 1))
    with _doc_lock:
        png = d[idx].get_pixmap(matrix=fitz.Matrix(2.6, 2.6)).tobytes("png")
    try:
        p = subprocess.run(["tesseract", "-", "-", "--psm", "3"],
                           input=png, capture_output=True, timeout=90)
        return p.stdout.decode("utf-8", "ignore")
    except Exception:
        return ""

# ── section -> page index (built once, cached to disk) ──────────────────────
def _build_index():
    d = doc()
    secpat = re.compile(r'\b(\d{3}\.\d{1,3})\b')                       # 210.52
    tabpat = re.compile(r'Table\s+(\d{3}\.\d+(?:\([A-Za-z0-9]+\))?)', re.I)
    ctabpat = re.compile(r'Table\s+(C\.\d+(?:\([A-Za-z0-9]+\))?)', re.I)  # Annex C: Table C.1(A)
    ch9pat = re.compile(r'Table\s+([1-9]|1[0-2])\b')                    # Chapter 9: Table 1..12
    artpat = re.compile(r'Article\s+(\d{3})', re.I)
    idx = {}
    def add(k, i):
        idx.setdefault(k, [])
        if i not in idx[k]:
            idx[k].append(i)
    for i in range(d.page_count):
        t = d[i].get_text()
        for s in set(secpat.findall(t)):        add(s, i)
        for s in set(tabpat.findall(t)):        add("Table " + s, i)
        for s in set(ctabpat.findall(t)):       add("Table " + s.upper(), i)
        for s in set(ch9pat.findall(t)):        add("Table " + s, i)
        for s in set(artpat.findall(t)):        add("Article " + s, i)
    try:
        json.dump(idx, open(INDEX_PATH, "w"))
    except Exception:
        pass
    return idx

def section_index():
    global _index
    if _index is None:
        if os.path.exists(INDEX_PATH):
            try: _index = json.load(open(INDEX_PATH))
            except Exception: _index = _build_index()
        else:
            _index = _build_index()
    return _index

def pages_for_section(sec):
    sec = (sec or "").strip()
    if not sec:
        return []
    idx = section_index()
    # candidate keys, most-specific first: full string, then base section number
    # (router often returns subsections like "210.52(B)(1)" — index holds base "210.52")
    cands = [sec]
    # Annex C table, e.g. "Table C.1(A)" -> "Table C.1"
    cm = re.search(r'Table\s+(C\.\d+)', sec, re.I)
    if cm:
        cands.append("Table " + cm.group(1).upper())
    # Chapter 9 table, e.g. "Chapter 9, Table 4" -> "Table 4"
    hm = re.search(r'Table\s+([1-9]|1[0-2])\b', sec)
    if hm:
        cands.append("Table " + hm.group(1))
    # numbered section/table, e.g. "210.52(B)(1)" -> "210.52"
    m = re.match(r'((?:Table\s+|Article\s+)?\d{1,3}\.\d{1,3})', sec)
    if m and m.group(1) not in cands:
        cands.append(m.group(1))
    m2 = re.search(r'\d{1,3}\.\d{1,3}', sec)          # bare number even if Table/Article-prefixed
    if m2 and m2.group(0) not in cands:
        cands.append(m2.group(0))
    for key in cands:
        if key in idx:
            return idx[key]
    # last resort: live scan for the most specific token we have
    needle = (cm.group(0) if cm else (m2.group(0) if m2 else sec))
    d = doc(); out = []
    for i in range(d.page_count):
        if d[i].search_for(needle):
            out.append(i)
            if len(out) >= 4:
                break
    return out

def pages_for_phrase(phrase, cap=3):
    d = doc(); out = []
    for i in range(d.page_count):
        if d[i].search_for(phrase):
            out.append(i)
            if len(out) >= cap:
                break
    return out

# ── LLM ─────────────────────────────────────────────────────────────────────
async def _complete(messages, max_tokens=500, temperature=0.2):
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(f"{MODEL_URL}/chat/completions",
                         json={"model": NEC_MODEL, "messages": messages,
                               "max_tokens": max_tokens, "temperature": temperature})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

def _extract_json(s):
    m = re.search(r'\{.*\}', s or "", re.S)
    if not m:
        return {}
    for cand in (m.group(0), m.group(0).replace("'", '"')):
        try:
            return json.loads(cand)
        except Exception:
            continue
    return {}

ROUTER_SYS = (
    "You are an NEC (NFPA 70, 2023 edition) navigation assistant. Given a question, identify the "
    "most relevant NEC section number(s) and short VERBATIM phrases likely printed in the code book. "
    "Respond with ONLY a JSON object, no prose:\n"
    '{"sections": ["210.52", "Table 210.52"], '
    '"phrases": ["small-appliance branch circuits", "receptacle outlets"], '
    '"answer": "one concise direct answer"}\n'
    "Use canonical NEC numbering (e.g. 210.52, 250.66, Table 310.16, Article 250). Prefer 1-3 sections.\n"
    "IMPORTANT for conduit/raceway FILL questions (how many conductors fit): the direct lookup tables "
    "are in ANNEX C — cite them as 'Table C.1' (EMT), 'Table C.4' (RMC), 'Table C.10' (PVC Sch 40), etc., "
    "AND Chapter 9 'Table 1' (% fill), 'Table 4' (conduit area), 'Table 5' (conductor area).\n"
    "Phrases must be DISTINCTIVE verbatim text likely printed in the code (table titles, defined terms) — "
    "not generic words like 'conduit' or 'wire'. Good phrases help locate & highlight the exact spot."
)

async def _translate(question, hint, clean_text, section):
    snippet = (clean_text or "")[:3500]
    sys = ("You explain U.S. electrical code (NEC / NFPA 70) in plain, practical English for a "
           "knowledgeable DIYer or electrician. Be concise and accurate. Never invent requirements; "
           "if the provided text doesn't fully answer, say what it does cover.")
    usr = (f"Question: {question}\n\n"
           f"Relevant NEC page text (OCR, around section {section or '?'}):\n"
           f'"""\n{snippet}\n"""\n\n'
           "Explain the answer in plain English (3-6 sentences). Reference the section number(s).")
    try:
        return await _complete([{"role": "system", "content": sys},
                                {"role": "user", "content": usr}],
                               max_tokens=1400, temperature=0.3)  # reasoning model needs headroom
    except Exception as e:
        return f"(Couldn't generate a plain-English summary: {e})"

def gather_and_rank(sections, phrases):
    """Collect candidate pages from sections + phrases, then rank by how many distinctive
    phrases actually appear on each page — so the real table/section page beats noisy
    cross-references (e.g. a stray 'Table 1' in the front matter)."""
    cand = {}
    for sec in sections:
        for p in pages_for_section(sec)[:5]:
            cand.setdefault(p, sec)
    for ph in phrases:
        for p in pages_for_phrase(ph, 5):
            cand.setdefault(p, "")
    scored = []
    d = doc()
    with _doc_lock:
        for p, sec in cand.items():
            score = sum(1 for ph in phrases if ph and d[p].search_for(ph))
            # a page whose section anchor is actually printed on it gets a small bonus
            if sec and d[p].search_for(sec.split(",")[-1].strip()):
                score += 0.5
            # Annex C tables directly list "max N conductors" — prioritize for fill questions
            if "C." in sec or "Annex C" in sec:
                score += 2
            scored.append((p, sec, score))
    scored.sort(key=lambda t: (-t[2], t[0]))
    hits = []
    for p, sec, score in scored[:8]:
        hl = ([sec] if sec else []) + phrases
        hits.append({"page": p, "section": sec, "highlights": hl})
    return hits

async def translate_page(question, page, section=""):
    """Plain-English for a specific page (used when the UI arrows to another hit)."""
    clean = await asyncio.to_thread(ocr_page, int(page))
    return await _translate(question or "Explain this NEC page.", "", clean, section)

async def ask(question):
    question = (question or "").strip()
    if not question:
        return {"error": "empty question"}
    # 1) router
    try:
        raw = await _complete([{"role": "system", "content": ROUTER_SYS},
                               {"role": "user", "content": question}],
                              max_tokens=1600, temperature=0)  # room for hidden reasoning + JSON
    except Exception as e:
        return {"error": f"router model error: {e}", "hits": []}
    spec = _extract_json(raw)
    sections = [s for s in (spec.get("sections") or []) if isinstance(s, str)][:5]
    phrases  = [p for p in (spec.get("phrases") or []) if isinstance(p, str)][:6]
    answer   = spec.get("answer") or ""

    # 2) ground + rank pages by distinctive-phrase density (blocking; offload)
    hits = await asyncio.to_thread(gather_and_rank, sections, phrases)

    # 4) translate the top hit from clean OCR text
    translation = ""
    if hits:
        clean = await asyncio.to_thread(ocr_page, hits[0]["page"])
        translation = await _translate(question, answer, clean, hits[0].get("section"))
    elif answer:
        translation = answer

    return {"answer": answer, "sections": sections, "phrases": phrases,
            "hits": hits, "translation": translation, "pages": page_count()}
