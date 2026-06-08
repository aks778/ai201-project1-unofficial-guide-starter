"""Embedding + Vector Store and Retrieval stages of the RAG pipeline.

Pipeline (see assets/pipeline.png):
    Document Ingestion -> Chunking -> [Embedding + Vector Store] -> [Retrieval] -> Generation

This module implements the two middle stages:

Embedding + Vector Store (build_index):
  * load the chunks produced by chunk_documents.py (chunks.json),
  * embed each chunk with all-MiniLM-L6-v2 (per planning.md Retrieval Approach),
  * store the vectors, chunk text, and source metadata in a persistent ChromaDB
    collection.

Retrieval (retrieve):
  * embed a query with the same model, then return the top-k most relevant
    chunks (k=4 per planning.md) with their source information.

Usage:
    python vector_store.py build               # (re)build the index from chunks.json
    python vector_store.py build --append      # add to the existing index instead
    python vector_store.py query "your question here"     # retrieve top-k chunks
"""

import argparse
import json
from pathlib import Path
from typing import Dict, List, Optional

import chromadb
from chromadb.api import ClientAPI
from chromadb.api.models.Collection import Collection
from chromadb.api.types import Metadata
from chromadb.utils import embedding_functions

BASE_DIR = Path(__file__).resolve().parent
CHUNKS_FILE = BASE_DIR / "chunks.json"
CHROMA_DIR = BASE_DIR / "chroma_db"

# These must match the planning.md Retrieval Approach section.
EMBEDDING_MODEL = "all-MiniLM-L6-v2"
COLLECTION_NAME = "gmu_cs_reviews"
TOP_K = 4

# Contextual chunking: every chunk is embedded with its document's title as a
# prefix, so deep chunks (e.g. a comment) carry the thread's topic and don't
# lose to the keyword-rich first chunk. See planning.md Anticipated Challenges.
CONTEXT_TEMPLATE = "[Thread: {title}]\n{text}"

# Number of chunks to embed/add per ChromaDB call. Keeps memory bounded and
# gives readable progress on larger corpora.
ADD_BATCH_SIZE = 128


def get_embedding_function() -> embedding_functions.EmbeddingFunction:
    """The shared embedding model.

    Used both to index chunks here and (later) to embed queries at retrieval
    time, so indexing and search always live in the same vector space.
    """
    return embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name=EMBEDDING_MODEL
    )


def get_client() -> ClientAPI:
    return chromadb.PersistentClient(path=str(CHROMA_DIR))


def get_collection(client: Optional[ClientAPI] = None) -> Collection:
    """Return the reviews collection, creating it if needed.

    Cosine distance is the right metric for sentence-transformer embeddings,
    whose semantic similarity is measured by angle rather than magnitude.
    """
    if client is None:
        client = get_client()
    return client.get_or_create_collection(
        name=COLLECTION_NAME,
        embedding_function=get_embedding_function(),
        metadata={"hnsw:space": "cosine"},
    )


def load_chunks(path: Path = CHUNKS_FILE) -> List[Dict]:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Run chunk_documents.py first to produce chunks.json."
        )
    with path.open(encoding="utf-8") as fh:
        chunks = json.load(fh)
    if not chunks:
        raise ValueError(
            f"{path} is empty. Run chunk_documents.py to (re)build it.")
    return chunks


def build_index(append: bool = False) -> chromadb.Collection:
    """Embed every chunk in chunks.json and store it in ChromaDB.

    By default the collection is rebuilt from scratch so repeated runs stay
    idempotent (no stale or duplicated chunks). Pass append=True to add to an
    existing collection instead.
    """
    chunks = load_chunks()
    client = get_client()

    if not append:
        try:
            client.delete_collection(COLLECTION_NAME)
        except Exception:
            # Nothing to delete on a fresh store; safe to ignore.
            pass

    collection = get_collection(client)

    ids = [chunk["chunk_id"] for chunk in chunks]
    # Embed each chunk prefixed with its thread title for context. chunks.json
    # keeps the raw chunk text; the title prefix is added only here at embed time.
    documents = [
        CONTEXT_TEMPLATE.format(
            title=chunk.get("title") or chunk["source"], text=chunk["text"])
        for chunk in chunks
    ]
    # Keep only what attribution needs: the source document name (the Reddit
    # post title) and the chunk's position within that document. chunk_id
    # ("doc{N}_chunk{M}") encodes the position M (the chunk's order in its doc).
    metadatas: List[Metadata] = [
        {
            "source": chunk.get("title") or chunk["source"],
            "position": int(chunk["chunk_id"].split("_chunk")[-1]),
        }
        for chunk in chunks
    ]

    print(
        f"Embedding {len(chunks)} chunks with '{EMBEDDING_MODEL}' "
        f"and storing in '{COLLECTION_NAME}'..."
    )
    for start in range(0, len(ids), ADD_BATCH_SIZE):
        end = start + ADD_BATCH_SIZE
        # ChromaDB embeds the documents via the collection's embedding function.
        collection.add(
            ids=ids[start:end],
            documents=documents[start:end],
            metadatas=metadatas[start:end],
        )
        print(f"  indexed {min(end, len(ids))}/{len(ids)} chunks")

    print(
        f"Done. Collection '{COLLECTION_NAME}' now holds {collection.count()} chunks at {CHROMA_DIR}")
    return collection


def retrieve(query: str, k: int = TOP_K, collection: Optional[Collection] = None) -> List[Dict]:
    """Return the top-k chunks most relevant to a query, with source info.

    The query is embedded with the same model used to build the index, then
    ChromaDB runs a cosine-similarity search over the stored chunk vectors.

    Args:
        query: the user's question / search string.
        k: how many chunks to return (defaults to TOP_K = 4 per planning.md).
        collection: optional pre-opened collection (reused across calls to
            avoid reloading the model each time); opened on demand if omitted.

    Returns:
        A list of up to k dicts, ordered most relevant first, each with:
            text       - the chunk text,
            source     - the source document name (Reddit post title),
            position   - the chunk's position within that document,
            similarity - cosine similarity to the query in [0, 1] (higher = closer).
    """
    if not query or not query.strip():
        raise ValueError("query must be a non-empty string.")

    if collection is None:
        collection = get_collection()
    result = collection.query(
        query_texts=[query],
        n_results=k,
        include=["documents", "metadatas", "distances"],
    )

    # ChromaDB returns one list per query; we only sent one query, so take [0].
    # documents/metadatas/distances are Optional in the result type (a field is
    # None if it wasn't requested in `include`); default to empty if absent.
    documents = result["documents"][0] if result["documents"] else []
    metadatas = result["metadatas"][0] if result["metadatas"] else []
    distances = result["distances"][0] if result["distances"] else []

    hits = []
    for text, meta, distance in zip(documents, metadatas, distances):
        hits.append(
            {
                "text": text,
                "source": meta.get("source"),
                "position": meta.get("position"),
                # Collection uses cosine distance; similarity = 1 - distance.
                "similarity": round(1 - distance, 4),
            }
        )
    return hits


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Embedding + Vector Store stage")
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_parser = subparsers.add_parser(
        "build", help="Embed chunks.json and store them in ChromaDB"
    )
    build_parser.add_argument(
        "--append",
        action="store_true",
        help="Add to the existing collection instead of rebuilding from scratch",
    )

    query_parser = subparsers.add_parser(
        "query", help="Retrieve the top-k chunks most relevant to a query"
    )
    query_parser.add_argument("text", help="The query / question string")
    query_parser.add_argument(
        "-k", type=int, default=TOP_K, help=f"Number of chunks to return (default {TOP_K})"
    )

    args = parser.parse_args()
    if args.command == "build":
        build_index(append=args.append)
    elif args.command == "query":
        hits = retrieve(args.text, k=args.k)
        print(f"\nTop {len(hits)} chunks for: {args.text!r}\n")
        for rank, hit in enumerate(hits, start=1):
            print(f"[{rank}] similarity={hit['similarity']} "
                  f"| source: {hit['source']} (position {hit['position']})")
            print(f"    {hit['text']}\n")


if __name__ == "__main__":
    main()
