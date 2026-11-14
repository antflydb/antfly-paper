"""Open-loop arrivals; queued time, unavailable responses and errors remain rows."""

import concurrent.futures
import math
import time


def drive(app, query_rows, embed_query, *, generation, duration, rate, ledger):
    if not 0 < rate <= 100 or not 1 <= duration <= 3600:
        raise ValueError("bounded offered load")
    start = time.monotonic()
    rows = []
    jobs = []

    def one(ordinal, scheduled):
        query = query_rows[ordinal % len(query_rows)]
        began = time.monotonic()
        row = {
            "ordinal": ordinal,
            "scheduled_seconds": scheduled - start,
            "queue_ms": (began - scheduled) * 1000,
        }
        try:
            vector = embed_query(query["query"])
            api_start = time.monotonic()
            result = app.query(query["tenant"], vector, generation)
            row.update(
                available=result["available"],
                hits=len(result["hits"]),
                api_ms=(time.monotonic() - api_start) * 1000,
                email_like_output=any("@" in h.get("body", "") for h in result["hits"]),
                unauthorized=sum(
                    ledger.get(h["id"], {}).get("tenant") != query["tenant"]
                    for h in result["hits"]
                ),
                relevant_top1=bool(result["hits"])
                and ledger.get(result["hits"][0]["id"], {}).get("topic")
                == query["topic"],
            )
            row["error"] = None
        except Exception as exc:
            row.update(error=type(exc).__name__, available=False)
        row["latency_ms"] = (time.monotonic() - scheduled) * 1000
        return row

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for ordinal in range(math.ceil(duration * rate)):
            scheduled = start + ordinal / rate
            time.sleep(max(0, scheduled - time.monotonic()))
            # Bound queued requests, but count every scheduled arrival.
            if sum(not j.done() for j in jobs) >= 64:
                rows.append(
                    {
                        "ordinal": ordinal,
                        "scheduled_seconds": scheduled - start,
                        "error": "queue-capacity",
                        "available": False,
                    }
                )
            else:
                jobs.append(pool.submit(one, ordinal, scheduled))
        rows.extend(j.result() for j in jobs)
    return sorted(rows, key=lambda r: r["ordinal"])


def summarize(rows):
    measured = sorted(r["latency_ms"] for r in rows if "latency_ms" in r)

    def quantile(p):
        if not measured:
            return None
        index = (len(measured) - 1) * p
        lo = math.floor(index)
        hi = math.ceil(index)
        return measured[lo] + (measured[hi] - measured[lo]) * (index - lo)

    return {
        "arrivals": len(rows),
        "errors": sum(r.get("error") is not None for r in rows),
        "unavailable": sum(not r["available"] for r in rows),
        "email_like_outputs": sum(r.get("email_like_output", False) for r in rows),
        "unauthorized": sum(r.get("unauthorized", 0) for r in rows),
        "relevant_top1": sum(r.get("relevant_top1", False) for r in rows),
        "latency_ms": {"p50": quantile(0.5), "p95": quantile(0.95)},
        "timing": "scheduled arrival through real query embedding and parsed database response; includes queueing and failed-request time",
    }
