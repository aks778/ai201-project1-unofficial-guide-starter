import html
import json
import re
from pathlib import Path
from typing import List, Dict, Optional

import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent
DOCUMENTS_DIR = BASE_DIR / "documents"
RAW_DIR = DOCUMENTS_DIR / "raw"
CLEAN_DIR = DOCUMENTS_DIR / "cleaned"
OUTPUT_FILE = BASE_DIR / "chunks.json"

# Sources from planning.md: Reddit threads only
DOCUMENT_SOURCES = [
    "https://www.reddit.com/r/gmu/comments/1cnjd7v/im_graduating_here_are_some_professors_to_stay/",
    "https://www.reddit.com/r/gmu/comments/lq2b0x/for_cs_majors_which_cs_class_have_you_enjoyed_the/",
    "https://www.reddit.com/r/gmu/comments/hrb9ji/cs_majors_cs_450_databases_vs_cs_468_secure/",
    "https://www.reddit.com/r/gmu/comments/125fkhn/cs_advice/",
    "https://www.reddit.com/r/gmu/comments/1gzxnfm/cs_310_russell/",
    "https://www.reddit.com/r/gmu/comments/jiqwa6/easiest_cs_senior_classes/",
    "https://www.reddit.com/r/gmu/comments/9ve2lx/how_are_these_cs_electives_classes/",
    "https://www.reddit.com/r/gmu/comments/1twm5k5/cs_can_i_manage_these_courses_togather/",
    "https://www.reddit.com/r/gmu/comments/150e28r/best_electives_for_a_cs_major_to_focus_on_data/",
    "https://www.reddit.com/r/gmu/comments/10qyv95/cs_students_what_are_some_of_the_classes_that/",
]

CHUNK_SIZE = 220  # target chunk size in tokens, smaller for tighter focused chunks
OVERLAP = 33  # ~15% overlap to preserve continuity between adjacent chunks

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0 Safari/537.36"
}

BOILERPLATE_PATTERNS = [
    r"^Read more",
    r"^Follow",
    r"^Share",
    r"^Log in",
    r"^Sign in",
    r"^Sign up",
    r"^Privacy",
    r"^Terms",
    r"^Cookie",
    r"^Help",
    r"^About",
    r"^Adverti",
    r"^Trending",
    r"^Related",
    r"^Subscribe",
    r"^More from",
    r"^Reddit",
    r"^Rate My Professors",
    r"^See all",
    r"^Sort by",
    r"^Comment.*count",
    r"^Give Award",
    r"^View on",
    r"^Sponsored",
    r"^Add a comment",
    r"^Search",
    r"^Cookies",
]


def ensure_dirs() -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    CLEAN_DIR.mkdir(parents=True, exist_ok=True)


def safe_filename(source: str) -> str:
    name = re.sub(r"https?://", "", source)
    name = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
    return name[:120].strip("_")


def normalize_entities(text: str) -> str:
    # Decode HTML entities (incl. numeric ones like &#225; -> á) rather than
    # stripping them, so accented names and symbols survive intact.
    text = html.unescape(text)
    text = text.replace("\xa0", " ")
    return text


def clean_text(text: str) -> str:
    text = normalize_entities(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def remove_boilerplate(text: str) -> str:
    lines = [line.strip() for line in text.splitlines()]
    clean_lines = []
    for line in lines:
        if not line:
            continue
        if len(line) < 20:
            if not re.search(r"\b(CS|Professor|Prof|Dr\.|GMU|George Mason|Overall|Rating|score|review|workload|difficulty|course|data science|cybersecurity)\b", line, flags=re.IGNORECASE):
                continue
        if any(re.search(pattern, line, flags=re.IGNORECASE) for pattern in BOILERPLATE_PATTERNS):
            continue
        clean_lines.append(line)

    # Remove repeated lines that are likely site boilerplate or repeated headers
    seen = set()
    filtered = []
    for line in clean_lines:
        if line in seen:
            continue
        seen.add(line)
        filtered.append(line)
    return "\n".join(filtered)


def fetch_reddit_raw_text(url: str) -> Optional[str]:
    old_url = re.sub(r"https?://(www\.)?reddit\.com",
                     "https://old.reddit.com", url)
    try:
        response = requests.get(old_url, headers=HEADERS, timeout=25)
        response.raise_for_status()
    except Exception as exc:
        print(f"Warning: failed to fetch Reddit HTML for {url}: {exc}")
        return None

    soup = BeautifulSoup(response.text, "html.parser")
    pieces = []

    # Keep the post (title + body) together as one content unit; each comment
    # is its own unit. These become the segment boundaries the chunker respects.
    post_parts = []
    title = soup.select_one("a.title")
    if title:
        post_parts.append(title.get_text(separator=" ", strip=True))

    selftext = soup.select_one("div.expando div.usertext-body div.md")
    if selftext:
        post_parts.append(selftext.get_text(separator=" ", strip=True))

    if post_parts:
        pieces.append(" ".join(post_parts))

    comment_nodes = soup.select(
        "div.comment div.entry div.usertext-body div.md")
    for comment in comment_nodes:
        comment_text = comment.get_text(separator=" ", strip=True)
        if comment_text and len(comment_text) > 20:
            pieces.append(comment_text)

    if not pieces:
        return extract_raw_text(url, response.text)

    raw = "\n\n".join(pieces)
    raw = normalize_entities(raw)
    raw = re.sub(r"\n{3,}", "\n\n", raw)
    return raw.strip()


def extract_raw_text(url: str, html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for element in soup(["script", "style", "noscript", "iframe", "header", "footer", "nav", "aside", "form", "svg", "button", "input", "img", "meta", "link"]):
        element.decompose()

    for tag in soup.find_all(True):
        attrs = getattr(tag, "attrs", {}) or {}
        class_id = " ".join([str(attrs.get("class")), str(attrs.get("id"))])
        if class_id and re.search(r"cookie|banner|nav|menu|footer|header|sidebar|advert|promo|share|subreddit|reddit|footer|social|comment-count|commentcount|vote|post-meta", class_id, re.IGNORECASE):
            if tag.name not in {"p", "span", "div", "article", "section"}:
                tag.decompose()

    paragraphs = []
    for element in soup.find_all(["h1", "h2", "h3", "p", "span", "li", "div"]):
        text = element.get_text(separator=" ", strip=True)
        if not text:
            continue
        if element.name == "div" and text.count(" ") < 5:
            continue
        paragraphs.append(text)

    raw = "\n\n".join(paragraphs)
    raw = normalize_entities(raw)
    raw = re.sub(r"\n{3,}", "\n\n", raw)
    return raw.strip()


def fetch_raw_text(url: str) -> Optional[str]:
    if "reddit.com" in url:
        raw = fetch_reddit_raw_text(url)
        if raw:
            return raw

    try:
        response = requests.get(url, headers=HEADERS, timeout=25)
        response.raise_for_status()
    except Exception as exc:
        print(f"Warning: failed to fetch {url}: {exc}")
        return None
    return extract_raw_text(url, response.text)


def save_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def load_local_documents() -> List[Dict[str, str]]:
    docs = []
    for path in sorted(DOCUMENTS_DIR.glob("*.*")):
        if path.name in {".gitkeep", "raw", "cleaned"}:
            continue
        if path.suffix.lower() in {".txt", ".md"}:
            text = path.read_text(encoding="utf-8", errors="ignore")
        elif path.suffix.lower() in {".html", ".htm"}:
            html = path.read_text(encoding="utf-8", errors="ignore")
            text = extract_raw_text(str(path), html)
        else:
            continue
        docs.append({"source": str(path), "raw_text": text})
    return docs


def clean_document(raw: str) -> str:
    # Remove boilerplate line-by-line while the structure is intact, then
    # collapse whitespace *within* each segment while keeping segment
    # boundaries (one content unit per line) so the chunker can respect them.
    cleaned = normalize_entities(raw)
    cleaned = remove_boilerplate(cleaned)
    segments = [clean_text(line) for line in cleaned.split("\n")]
    segments = [s for s in segments if s]
    return "\n".join(segments)


def tokenize(text: str) -> List[str]:
    return re.findall(r"\S+", text)


ABBREVIATIONS = {
    "dr", "prof", "mr", "mrs", "ms", "sr", "jr", "st", "vs", "etc", "eg",
    "ie", "no", "fig", "inc", "ltd", "co", "phd", "approx", "gen", "sen",
    "rep", "gov", "col", "capt", "ph", "u", "p",
}


def split_sentences(text: str) -> List[str]:
    # Split on sentence-ending punctuation, but don't break after an
    # abbreviation ("Dr.", "Prof.", "e.g.") or a single capital initial
    # ("J. Smith") — common in professor names and course discussion.
    parts = re.split(r"(?<=[.!?])\s+", text)
    sentences: List[str] = []
    buffer = ""
    for part in parts:
        part = part.strip()
        if not part:
            continue
        buffer = f"{buffer} {part}" if buffer else part
        match = re.search(r"(\w+)[.!?]+[\"')\]]*$", buffer)
        if match:
            last = match.group(1)
            is_initial = len(last) == 1 and last.isupper()
            if last.lower() in ABBREVIATIONS or is_initial:
                continue
        sentences.append(buffer)
        buffer = ""
    if buffer:
        sentences.append(buffer)
    return sentences


def hard_split_tokens(text: str, chunk_size: int, overlap: int) -> List[str]:
    """Last-resort fixed-width split for a single span with no sentence breaks."""
    tokens = tokenize(text)
    chunks = []
    start = 0
    step = max(chunk_size - overlap, 1)
    while start < len(tokens):
        end = min(start + chunk_size, len(tokens))
        chunks.append(" ".join(tokens[start:end]).strip())
        if end == len(tokens):
            break
        start += step
    return chunks


def split_units(text: str, chunk_size: int) -> List[str]:
    """Break a document into packable pieces that respect content boundaries.

    Each segment (post body or a comment, one per line from clean_document) is
    kept whole if it fits in chunk_size; an oversized segment is broken into
    its sentences so it can be packed without ever cutting mid-sentence.
    """
    units: List[str] = []
    for segment in text.split("\n"):
        segment = segment.strip()
        if not segment:
            continue
        if len(tokenize(segment)) <= chunk_size:
            units.append(segment)
        else:
            units.extend(split_sentences(segment))
    return units


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = OVERLAP) -> List[str]:
    """Pack content into ~chunk_size chunks while respecting boundaries.

    Whole small comments are combined to approach the target size, so we don't
    emit lots of tiny chunks; a comment too large to fit is split on sentence
    boundaries instead. Pieces are never cut mid-sentence, and ~overlap tokens
    of the trailing piece(s) carry into the next chunk for continuity.
    """
    chunks: List[str] = []
    current: List[str] = []
    current_tokens = 0

    for unit in split_units(text, chunk_size):
        unit_tokens = len(tokenize(unit))
        # A single piece bigger than the budget (an over-long sentence with no
        # internal break) can't be packed — flush, then hard-split it.
        if unit_tokens > chunk_size:
            if current:
                chunks.append(" ".join(current))
                current, current_tokens = [], 0
            chunks.extend(hard_split_tokens(unit, chunk_size, overlap))
            continue
        if current and current_tokens + unit_tokens > chunk_size:
            chunks.append(" ".join(current))
            # Cap the carry so carry + the incoming unit still fits chunk_size;
            # otherwise overlap could push a near-full chunk over the target.
            carry_budget = min(overlap, max(chunk_size - unit_tokens, 0))
            carry: List[str] = []
            carry_tokens = 0
            for prev in reversed(current):
                prev_tokens = len(tokenize(prev))
                if carry_tokens + prev_tokens > carry_budget:
                    break
                carry.insert(0, prev)
                carry_tokens += prev_tokens
            current, current_tokens = carry, carry_tokens
        current.append(unit)
        current_tokens += unit_tokens

    if current:
        chunks.append(" ".join(current))
    return chunks


def build_chunks_from_documents(documents: List[Dict[str, str]]) -> List[Dict[str, str]]:
    all_chunks = []
    for doc_index, doc in enumerate(documents, start=1):
        text = doc["clean_text"]
        chunks = chunk_text(text)
        for chunk_index, chunk in enumerate(chunks, start=1):
            all_chunks.append(
                {
                    "source": doc["source"],
                    "chunk_id": f"doc{doc_index}_chunk{chunk_index}",
                    "text": chunk,
                    "chunk_size_tokens": len(tokenize(chunk)),
                }
            )
    return all_chunks


def print_example_document(documents: List[Dict[str, str]]) -> None:
    if not documents:
        return
    doc = documents[0]
    print("\n=== Example cleaned document ===")
    print(f"Source: {doc['source']}")
    excerpt = doc["clean_text"][:3000]
    print(excerpt)
    print("\n=== End example document ===\n")


def main() -> None:
    ensure_dirs()

    documents = []

    local_docs = load_local_documents()
    if local_docs:
        print(
            f"Loaded {len(local_docs)} local raw documents from {DOCUMENTS_DIR}")
        for doc in local_docs:
            raw_path = RAW_DIR / (safe_filename(doc["source"]) + ".txt")
            save_text(raw_path, doc["raw_text"])
            cleaned = clean_document(doc["raw_text"])
            clean_path = CLEAN_DIR / (safe_filename(doc["source"]) + ".txt")
            save_text(clean_path, cleaned)
            documents.append({"source": doc["source"], "clean_text": cleaned})

    for url in DOCUMENT_SOURCES:
        print(f"Fetching {url}")
        raw_text = fetch_raw_text(url)
        if not raw_text:
            continue
        source_name = safe_filename(url)
        raw_path = RAW_DIR / (source_name + ".txt")
        save_text(raw_path, raw_text)
        cleaned = clean_document(raw_text)
        clean_path = CLEAN_DIR / (source_name + ".txt")
        save_text(clean_path, cleaned)
        documents.append({"source": url, "clean_text": cleaned})

    if not documents:
        print("No documents were loaded. Exiting.")
        return

    print_example_document(documents)

    chunks = build_chunks_from_documents(documents)
    print(
        f"Created {len(chunks)} chunks from {len(documents)} cleaned documents")

    OUTPUT_FILE.write_text(json.dumps(
        chunks, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Saved chunks to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
