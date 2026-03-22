"""RAG utilities: text chunking, embeddings, and similarity search."""

import json
import math
import logging
from typing import List, Dict

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = "text-embedding-004"


def chunk_text(text: str, chunk_size: int = 2000, overlap: int = 400) -> List[str]:
    """Split text into chunks at paragraph/sentence boundaries.

    Args:
        text: The input text to chunk.
        chunk_size: Target chunk size in characters (~500 tokens).
        overlap: Overlap between consecutive chunks in characters.
    """
    if len(text) <= chunk_size:
        return [text]

    # Split on double newlines (paragraphs) first
    paragraphs = text.split("\n\n")
    chunks = []
    current_chunk = ""

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        if len(current_chunk) + len(para) + 2 <= chunk_size:
            current_chunk = f"{current_chunk}\n\n{para}" if current_chunk else para
        else:
            if current_chunk:
                chunks.append(current_chunk.strip())
                # Create overlap from end of current chunk
                overlap_text = current_chunk[-overlap:] if len(current_chunk) > overlap else current_chunk
                current_chunk = f"{overlap_text}\n\n{para}"
            else:
                # Single paragraph larger than chunk_size — split on sentences
                sentences = _split_sentences(para)
                for sentence in sentences:
                    if len(current_chunk) + len(sentence) + 1 <= chunk_size:
                        current_chunk = f"{current_chunk} {sentence}" if current_chunk else sentence
                    else:
                        if current_chunk:
                            chunks.append(current_chunk.strip())
                            overlap_text = current_chunk[-overlap:] if len(current_chunk) > overlap else current_chunk
                            current_chunk = f"{overlap_text} {sentence}"
                        else:
                            # Single sentence larger than chunk_size — hard split
                            for i in range(0, len(sentence), chunk_size - overlap):
                                chunks.append(sentence[i:i + chunk_size])
                            current_chunk = ""

    if current_chunk.strip():
        chunks.append(current_chunk.strip())

    return chunks


def _split_sentences(text: str) -> List[str]:
    """Simple sentence splitter."""
    import re
    sentences = re.split(r'(?<=[.!?])\s+', text)
    return [s for s in sentences if s.strip()]


def cosine_similarity(a: List[float], b: List[float]) -> float:
    """Compute cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


async def embed_texts(client, texts: List[str]) -> List[List[float]]:
    """Embed a list of texts using the Gemini embedding model.

    Args:
        client: google.genai.Client instance.
        texts: List of text strings to embed.

    Returns:
        List of embedding vectors.
    """
    embeddings = []
    # Process in batches of 100 (API limit)
    batch_size = 100
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        try:
            response = await client.aio.models.embed_content(
                model=EMBEDDING_MODEL,
                contents=batch,
            )
            for emb in response.embeddings:
                embeddings.append(list(emb.values))
        except Exception as e:
            logger.error(f"Failed to embed batch {i} ({len(batch)} texts): {e}")
            if not embeddings:
                # First batch failed — no usable embeddings at all, raise
                raise RuntimeError(f"Embedding failed: {e}") from e
            # Partial failure: fill with zero vectors (skipped in similarity search)
            logger.warning(f"Partial embedding failure: {len(batch)} chunks will be excluded from RAG")
            for _ in batch:
                embeddings.append([0.0] * 768)
    return embeddings


async def find_relevant_chunks(
    client,
    query: str,
    chunks_with_embeddings: List[Dict],
    top_k: int = 5,
) -> List[Dict]:
    """Find the most relevant chunks for a query using embedding similarity.

    Args:
        client: google.genai.Client instance.
        query: The user's query text.
        chunks_with_embeddings: List of dicts with 'chunk_text' and 'embedding' keys.
        top_k: Number of top results to return.

    Returns:
        List of dicts sorted by relevance (highest first).
    """
    if not chunks_with_embeddings:
        return []

    # Embed the query
    try:
        query_embeddings = await embed_texts(client, [query])
    except Exception as e:
        logger.warning(f"Query embedding failed, returning top-k by position: {e}")
        return chunks_with_embeddings[:top_k]
    if not query_embeddings or all(v == 0.0 for v in query_embeddings[0]):
        return chunks_with_embeddings[:top_k]

    query_vec = query_embeddings[0]

    # Score each chunk
    scored = []
    for chunk in chunks_with_embeddings:
        emb = chunk.get("embedding")
        if isinstance(emb, str):
            emb = json.loads(emb)
        if not emb or all(v == 0.0 for v in emb):
            continue
        score = cosine_similarity(query_vec, emb)
        scored.append((score, chunk))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [item[1] for item in scored[:top_k]]
