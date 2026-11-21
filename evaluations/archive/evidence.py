"""Physical-page and evidence-graph oracles shared across database adapters."""

import re
from collections import Counter
from pathlib import Path


def citations(row, counts):
    """Publisher citations are one-based physical pages; never infer missing pages."""
    pages = set()
    source = row["source_docs"]
    # Descriptions can contain semicolons. Split on keyed record starts.
    records = re.findall(
        r"corpus_file=([^|;]+?)\s*\|\s*pdf_page_number=(\d+)\b", source
    )
    if records and len(records) != source.count("corpus_file="):
        raise ValueError("unrecognized keyed citation")
    if not records:
        # Two publisher formats in the frozen V2 CSV omit field labels.
        for line in source.strip().splitlines():
            match = re.match(
                r"^N/A\s+\d{4}\s+(\d+)\s+(\S+?\.txt)(?:,|\s|$)", line.strip()
            )
            if match:
                records.append((match[2], match[1]))
                continue
            match = re.fullmatch(r"([\w.-]+)\s+page\s+(\d+)", line.strip())
            if not match:
                raise ValueError(f"unrecognized citation format in {row['uid']}")
            records.append((match[1] + ".txt", match[2]))
    for name, number in records:
        name = name.strip()
        if name != Path(name).name or Path(name).suffix.lower() not in {".pdf", ".txt"}:
            raise ValueError(f"invalid cited file: {name}")
        doc = str(Path(name).with_suffix(".pdf"))
        page = int(number)
        if not 1 <= page <= counts[doc]:
            raise ValueError(f"citation outside PDF: {doc}, {page}")
        pages.add((doc, page))
    return pages


def page_id(document, page):
    return f"{document}#page={page}"


def score_pages(judgments, rankings, k):
    if k < 1 or not judgments or judgments.keys() != rankings.keys():
        raise ValueError("expected a complete paired query set and positive k")
    per_query = {}
    for query, expected in judgments.items():
        hits = rankings[query]
        if not isinstance(hits, list) or not all(isinstance(hit, str) for hit in hits):
            raise ValueError(f"invalid ranking for {query}")
        eligible = set(expected["eligible"])
        relevant = set(expected["pages"])
        if not relevant:
            raise ValueError(f"empty evidence for {query}")
        for hit in hits:
            if not re.fullmatch(r".+\.pdf#page=[1-9][0-9]*", hit):
                raise ValueError(f"invalid physical page ID: {hit}")
        unique = list(dict.fromkeys(hits))
        found = len(relevant.intersection(unique[:k]))
        per_query[query] = {
            "found": found,
            "required": len(relevant),
            "recall": found / len(relevant),
            "all": found == len(relevant),
            "ineligible": sum(
                hit.rsplit("#page=", 1)[0] not in eligible for hit in hits
            ),
        }
    return {
        "k": k,
        "queries": len(per_query),
        "recall": sum(x["recall"] for x in per_query.values()) / len(per_query),
        "complete": sum(x["all"] for x in per_query.values()) / len(per_query),
        "ineligible": sum(x["ineligible"] for x in per_query.values()),
        "per_query": per_query,
    }


def fuse(channels, limit):
    scores = Counter()
    for hits in channels:
        for rank, hit in enumerate(dict.fromkeys(hits), 1):
            scores[hit] += 1 / (60 + rank)
    return sorted(scores, key=lambda hit: (-scores[hit], hit))[:limit]


def score_assertions(expected, observed):
    """A supported assertion is a (subject, predicate, object, page ID) tuple."""
    gold, found = set(map(tuple, expected)), set(map(tuple, observed))
    if any(len(item) != 4 for item in gold | found):
        raise ValueError("expected four fields per supported assertion")
    true = len(gold & found)
    return {
        "correct": true,
        "required": len(gold),
        "returned": len(found),
        "precision": true / len(found) if found else None,
        "recall": true / len(gold) if gold else None,
        "missing": sorted(gold - found),
        "unexpected": sorted(found - gold),
    }
