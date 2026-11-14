"""Independent synthetic embedding oracle; not an inference performance model."""

import hashlib
import math


def embedding(text, revision):
    if revision not in {"A", "B"}:
        raise ValueError("unknown fixture model")
    digest = hashlib.sha256(text.encode()).digest()
    x = (int.from_bytes(digest[:4], "big") + 1) / (2**32 + 1)
    norm = math.sqrt(x * x + 1)
    vector = [x / norm, 1 / norm]
    return vector if revision == "A" else [-vector[1], vector[0]]


def matches(actual, text, revision):
    expected = embedding(text, revision)
    return len(actual) == len(expected) and all(
        abs(a - b) < 1e-5 for a, b in zip(actual, expected)
    )


def documents():
    return {
        f"doc{i}": {"body": f"lifecycle source {i} revision 1", "version": 1}
        for i in range(8)
    }


def assess(hits, ledger, revision):
    """Score complete observed IDs; empty/unavailable reads cannot pass."""
    if not ledger:
        raise ValueError("nonempty live source ledger required")
    missing = len(set(ledger) - set(hits))
    unexpected = len(set(hits) - set(ledger))
    mismatched = sum(
        not matches(hits[k], ledger[k]["body"], revision)
        for k in set(hits) & set(ledger)
    )
    return {
        "passed": not (missing or unexpected or mismatched),
        "missing": missing,
        "unexpected": unexpected,
        "vector_mismatches": mismatched,
        "checked_hits": len(hits),
    }
