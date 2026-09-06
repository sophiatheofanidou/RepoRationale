"""A lean, deterministic offline BM25 lexical retrieval baseline over the
persisted `SourceChunk` corpus.

`BM25Retriever` precomputes corpus statistics once from a fixed sequence of
already-validated chunks and answers any number of queries against that same
precomputed state — it never rebuilds the corpus, never touches the
filesystem, and never calls an embedding or GitHub API. `load_lexical_retriever`
is the one read-only entry point that turns an already-published chunk
artifact into a reusable retriever.

This is intentionally a small, standard Okapi BM25 implementation: no
external BM25 package, no persisted lexical index, no stemming, stop-word
removal, or query expansion, and no default result limit or chunk size.
"""

import math
import re
from collections.abc import Sequence
from pathlib import Path

from reporationale.adapters.snapshot_store import load_chunk_artifact, load_snapshot
from reporationale.application.chunking import CHUNKER_ALGORITHM_VERSION
from reporationale.domain.chunk import SOURCE_CHUNK_SCHEMA_VERSION, SourceChunk
from reporationale.domain.retrieval import RankedEvidence

# Standard Okapi BM25 free parameters.
k1 = 1.5
b = 0.75

# Unicode-aware: a token is a maximal run of letters or digits (`\w` minus
# `_`); underscores and every other punctuation/whitespace character are
# separators. No stemming, stop-word removal, or other language-specific
# processing — this is the one deterministic tokenizer used by both
# indexing and querying, kept private to this module.
_TOKEN_PATTERN = re.compile(r"[^\W_]+")


def _tokenize(text: str) -> tuple[str, ...]:
    return tuple(match.casefold() for match in _TOKEN_PATTERN.findall(text))


def _require_limit(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("limit must be a strict positive integer")
    return value


class BM25Retriever:
    """A reusable, precomputed Okapi BM25 index over a fixed sequence of
    `SourceChunk`s.

    All corpus statistics (per-document term frequencies, document
    frequencies, document lengths, and the average document length) are
    computed once in `__init__`; `search` reuses that same precomputed
    state for every call, so scoring one more query never re-scans or
    re-tokenizes the corpus.
    """

    def __init__(self, chunks: Sequence[SourceChunk]) -> None:
        chunks_tuple = tuple(chunks)
        seen_chunk_ids: set[str] = set()
        for chunk in chunks_tuple:
            if chunk.chunk_id in seen_chunk_ids:
                raise ValueError(f"duplicate chunk_id: {chunk.chunk_id!r}")
            seen_chunk_ids.add(chunk.chunk_id)
        self._chunks = chunks_tuple

        term_frequencies: list[dict[str, int]] = []
        document_lengths: list[int] = []
        document_frequency: dict[str, int] = {}
        for chunk in chunks_tuple:
            tokens = _tokenize(chunk.text)
            document_lengths.append(len(tokens))
            frequencies: dict[str, int] = {}
            for token in tokens:
                frequencies[token] = frequencies.get(token, 0) + 1
            term_frequencies.append(frequencies)
            for token in frequencies:
                document_frequency[token] = document_frequency.get(token, 0) + 1

        self._term_frequencies = tuple(term_frequencies)
        self._document_lengths = tuple(document_lengths)
        self._document_frequency = document_frequency
        self._document_count = len(chunks_tuple)
        self._average_document_length = (
            sum(document_lengths) / self._document_count
            if self._document_count > 0
            else 0.0
        )

    def search(self, query: str, *, limit: int) -> tuple[RankedEvidence, ...]:
        """Return up to `limit` ranked chunks for `query`, ordered by
        descending BM25 score with ties broken deterministically by
        `chunk_id`. Chunks with a final score of zero (no lexical overlap
        with `query`) are never included. Returns an empty tuple for an
        empty corpus or a query whose terms occur in no chunk.
        """
        validated_limit = _require_limit(limit)
        query_terms = sorted(set(_tokenize(query)))
        if not query_terms:
            raise ValueError("query must contain at least one searchable token")

        if self._document_count == 0 or self._average_document_length == 0:
            return ()

        scored: list[tuple[float, str, int]] = []
        for index, chunk in enumerate(self._chunks):
            document_length = self._document_lengths[index]
            frequencies = self._term_frequencies[index]
            score = 0.0
            for term in query_terms:
                term_frequency = frequencies.get(term, 0)
                if term_frequency == 0:
                    continue
                document_frequency = self._document_frequency.get(term, 0)
                idf = math.log(
                    1
                    + (self._document_count - document_frequency + 0.5)
                    / (document_frequency + 0.5)
                )
                denominator = term_frequency + k1 * (
                    1 - b + b * document_length / self._average_document_length
                )
                score += idf * (term_frequency * (k1 + 1)) / denominator
            if score > 0:
                scored.append((score, chunk.chunk_id, index))

        scored.sort(key=lambda item: (-item[0], item[1]))
        limited = scored[:validated_limit]

        return tuple(
            RankedEvidence(
                evidence_id=self._chunks[index].chunk_id,
                rank=rank,
                score=score,
                score_kind="bm25",
                chunk=self._chunks[index],
            )
            for rank, (score, _chunk_id, index) in enumerate(limited, start=1)
        )


def load_lexical_retriever(*, snapshot_dir: Path, max_chars: int) -> BM25Retriever:
    """Load the persisted normalized-source snapshot and its existing
    derived chunk artifact at `snapshot_dir`, and construct a reusable
    `BM25Retriever` from those already-chunked, already-validated chunks.

    This is a read-only offline baseline: it never chunks, rebuilds, or
    writes anything, and never calls GitHub. `max_chars` selects which
    already-published chunk configuration to load; it is not a default and
    must be supplied explicitly, matching the parameter the chunk artifact
    was actually built with. Raises whatever `load_snapshot`/
    `load_chunk_artifact` raise (`SnapshotNotFound`, `SnapshotCorrupted`,
    `SnapshotIncompatible`, propagated unchanged) for a missing, corrupted,
    or incompatible normalized-source snapshot or chunk artifact.
    """
    source_snapshot = load_snapshot(snapshot_dir)
    loaded_chunks = load_chunk_artifact(
        snapshot_dir,
        expected_source_schema_version=source_snapshot.manifest.source_schema_version,
        expected_sources_digest=source_snapshot.manifest.sources_digest,
        expected_chunk_schema_version=SOURCE_CHUNK_SCHEMA_VERSION,
        expected_chunker_algorithm_version=CHUNKER_ALGORITHM_VERSION,
        expected_max_chars=max_chars,
    )
    return BM25Retriever(loaded_chunks.chunks)
