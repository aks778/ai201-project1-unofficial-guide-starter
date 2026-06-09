"""Generation stage of the RAG pipeline.

Pipeline (see assets/pipeline.png):
    Document Ingestion -> Chunking -> Embedding + Vector Store -> Retrieval -> [Generation]

This module turns a user question into a grounded, source-attributed answer:
  * retrieve the top-k chunks for the question (Retrieval stage, vector_store.py),
  * build a prompt that passes those chunks as context and instructs the model
    to answer ONLY from that context,
  * call Groq's llama-3.3-70b-versatile to generate the answer,
  * surface the source document(s): the model is told to cite them, and the
    retrieved source titles are also appended programmatically as a guarantee.

Requires a Groq API key in a .env file (GROQ_API_KEY=...). Copy .env.example to
.env and add your key (free at https://console.groq.com).

Usage:
    python generate.py "Which CS professors do students recommend at GMU?"
"""

import argparse
import os
from typing import Dict, List, Optional

from dotenv import load_dotenv
from groq import Groq

import vector_store as vs

load_dotenv()

GROQ_MODEL = "llama-3.3-70b-versatile"
# Exact fallback phrase the model must use when the context is insufficient.
NO_INFO_RESPONSE = "I don't have enough information on that."

SYSTEM_PROMPT = (
    "You are a knowledgeable George Mason University CS student giving honest, "
    "helpful advice to a fellow student, based ONLY on what other students said "
    "in the provided review documents.\n\n"
    "How to write your answer:\n"
    "- Sound natural and conversational, like you're sharing your read on things "
    "with a friend who asked. Avoid bullet-point data dumps and robotic lists.\n"
    "- Synthesize across the reviews: connect related points, note where students "
    "agree or disagree, and pull out the overall themes instead of restating each "
    "comment one by one.\n"
    "- Explain the reasoning behind what students say (the 'why'), not just the facts.\n\n"
    "Grounding rules (these override tone):\n"
    "- Use ONLY information found in the provided documents. Never add facts, "
    "professor names, course numbers, or claims that are not in them.\n"
    "- You may rephrase, summarize, and connect ideas that are in the documents, "
    "but do not invent new ones or overstate how strongly students feel.\n"
    f"- If the documents don't contain enough information to answer, respond with "
    f"exactly \"{NO_INFO_RESPONSE}\" and nothing else.\n"
    "- Do not mention document numbers or list sources in your answer; the source "
    "documents are listed automatically after your answer."
)


def format_context(chunks: List[Dict]) -> str:
    """Render retrieved chunks as numbered documents labeled with their source."""
    blocks = []
    for i, chunk in enumerate(chunks, start=1):
        blocks.append(
            f'[Document {i} - source: "{chunk["source"]}"]\n{chunk["text"]}')
    return "\n\n".join(blocks)


def build_user_prompt(query: str, context: str) -> str:
    return (
        f"Documents:\n{context}\n\n"
        f"Question: {query}\n\n"
        "Using only the information in the documents above, write a natural, "
        "conversational answer that synthesizes what students say -- connect the "
        "related points and highlight the overall themes rather than listing each "
        "comment separately. "
        f"If the documents don't contain enough information to answer, respond with "
        f"exactly \"{NO_INFO_RESPONSE}\" and nothing else. "
        "Do not mention document numbers or sources in your answer."
    )


def _get_client() -> Groq:
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key or api_key == "your_key_here":
        raise RuntimeError(
            "GROQ_API_KEY is not set. Copy .env.example to .env and add your "
            "Groq API key (free at https://console.groq.com)."
        )
    return Groq(api_key=api_key)


def unique_sources(chunks: List[Dict]) -> List[str]:
    """Distinct source document titles, in retrieval order."""
    sources: List[str] = []
    for chunk in chunks:
        source = chunk.get("source")
        if source and source not in sources:
            sources.append(source)
    return sources


def generate_answer(
    query: str,
    k: int = vs.TOP_K,
    chunks: Optional[List[Dict]] = None,
    client: Optional[Groq] = None,
) -> Dict:
    """Answer a question grounded in the retrieved chunks.

    Args:
        query: the user's question.
        k: how many chunks to retrieve (defaults to TOP_K = 4).
        chunks: pre-retrieved chunks; if omitted, retrieve() is called.
        client: optional pre-built Groq client (reused across calls).

    Returns a dict with:
        answer  - the model's answer with a programmatic "Sources:" footer,
        text    - the raw model answer (no footer),
        sources - distinct source document titles used as context,
        chunks  - the retrieved chunks that grounded the answer.
    """
    if not query or not query.strip():
        raise ValueError("query must be a non-empty string.")

    if chunks is None:
        chunks = vs.retrieve(query, k=k)

    # No context retrieved -> nothing to ground an answer on.
    if not chunks:
        return {"answer": NO_INFO_RESPONSE, "text": NO_INFO_RESPONSE, "sources": [], "chunks": []}

    client = client or _get_client()
    context = format_context(chunks)
    user_prompt = build_user_prompt(query, context)

    # The model occasionally returns an empty completion; retry once before
    # giving up so we never emit a blank answer (with a stray Sources footer).
    text = ""
    for _ in range(2):
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.5,  # a bit of warmth for natural prose, still grounded
            max_tokens=1024,
        )
        text = (response.choices[0].message.content or "").strip()
        if text:
            break

    if not text:
        return {"answer": "I wasn't able to generate an answer. Please try again.",
                "text": "", "sources": [], "chunks": chunks}

    # If the model couldn't answer from the context, return ONLY the fallback
    # phrase with no sources -- there is no document the answer was drawn from.
    if NO_INFO_RESPONSE.lower() in text.lower():
        return {"answer": NO_INFO_RESPONSE, "text": text, "sources": [], "chunks": chunks}

    # Otherwise list the retrieved source document(s) once, at the very end.
    sources = unique_sources(chunks)
    answer = text
    if sources:
        footer = "\n\nSources:\n" + \
            "\n".join(f"- {source}" for source in sources)
        answer = text + footer

    return {"answer": answer, "text": text, "sources": sources, "chunks": chunks}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a grounded, source-attributed answer to a question.")
    parser.add_argument("question", help="The question to answer")
    parser.add_argument(
        "-k", type=int, default=vs.TOP_K,
        help=f"Number of chunks to retrieve as context (default {vs.TOP_K})")
    args = parser.parse_args()

    result = generate_answer(args.question, k=args.k)
    print(f"\nQ: {args.question}\n")
    print(result["answer"])


if __name__ == "__main__":
    main()
