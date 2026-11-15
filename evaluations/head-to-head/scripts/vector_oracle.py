"""Independent, fixture-specific vector acceptance; never used by serving code."""

import base64
import hashlib
import json
import math
import re


class VectorOracleError(AssertionError):
    pass


def require(condition, message):
    if not condition:
        raise VectorOracleError(message)


def fingerprint(text):
    return hashlib.sha256(text.encode()).hexdigest()


def verify_snapshot(
    snapshot, hit, index, model, policy_version, source, embed, checkpoint=None
):
    """Check a single-page, single-chunk fixture against independent reads/model."""
    require(isinstance(snapshot, dict), "missing inspection")
    require(
        snapshot.get("format") == "antfly.dense-index-inspection.v1",
        "inspection format",
    )
    require(snapshot.get("current") is True, "publication is incomplete")
    require(snapshot.get("index") == index, "wrong index")
    require(snapshot.get("document_id") == hit["id"], "wrong document")
    require(snapshot.get("policy_version") == policy_version, "stale policy")
    for field in ("index_fingerprint", "document_fingerprint"):
        require(
            re.fullmatch("[0-9a-f]{64}", snapshot.get(field, "")) is not None, field
        )
    require(
        snapshot.get("publication_authority")
        in ("standalone_guarded_coverage", "ordered_causal_provenance"),
        "publication authority",
    )
    require(snapshot.get("checkpoint_status") == "clean", "dirty checkpoint")
    for field in (
        "index_config_hash",
        "checkpoint_config_hash",
        "checkpoint_generation",
        "applied_sequence",
        "replay_target_sequence",
    ):
        require(
            type(snapshot.get(field)) is int and 0 <= snapshot[field] < 2**64, field
        )
    require(
        snapshot["index_config_hash"] == snapshot["checkpoint_config_hash"],
        "checkpoint recipe mismatch",
    )
    if checkpoint is not None:
        for field in ("checkpoint_config_hash", "checkpoint_generation"):
            require(snapshot[field] == checkpoint[field], "stale live " + field)
    require(
        snapshot["applied_sequence"] >= snapshot["replay_target_sequence"],
        "replay debt",
    )
    generation = snapshot.get("index_generation")
    require(type(generation) is int and generation >= 0, "index generation")
    document_json = snapshot.get("document_json", "")
    require(
        fingerprint(document_json) == snapshot["document_fingerprint"],
        "document digest",
    )
    document = json.loads(document_json)
    for field in ("body", "tenant", "version", "page"):
        require(
            field in hit and document.get(field) == hit[field],
            "serving/inspection mismatch: " + field,
        )
    for field in ("tenant", "version", "page"):
        require(hit[field] == source[field], "stale source: " + field)
    require("@" not in hit["body"], "unprotected serving text")
    vectors = snapshot.get("vectors", [])
    # The authored protocol promises one short page per document. Reject a
    # changed chunking/prompt protocol instead of embedding self-reported text.
    require(len(vectors) == 1, "expected exactly one fixture chunk")
    vector = vectors[0]
    require(
        vector.get("provider") == "antfly" and vector.get("model") == model,
        "wrong model",
    )
    require(
        vector.get("source_text") == hit["body"], "stale or different embedding input"
    )
    require(
        vector.get("source_fingerprint") == fingerprint(hit["body"]),
        "embedding input digest",
    )
    require(
        re.fullmatch("[0-9a-f]{64}", vector.get("producer_fingerprint", ""))
        is not None,
        "producer digest",
    )
    parts = vector.get("artifact_id", "").split(":")
    require(len(parts) >= 4 and parts[:2] == ["af1", "embedding"], "embedding identity")
    require(
        base64.urlsafe_b64decode(parts[2] + "=" * (-len(parts[2]) % 4)).decode()
        == hit["id"],
        "artifact document",
    )
    require(
        base64.urlsafe_b64decode(parts[3] + "=" * (-len(parts[3]) % 4)).decode()
        == vector.get("producer"),
        "artifact producer",
    )
    require(
        re.fullmatch("[0-9a-f]{64}", vector.get("artifact_fingerprint", ""))
        is not None,
        "artifact digest",
    )
    require(
        re.fullmatch("[0-9a-f]{16}", vector.get("provider_cache_identity", ""))
        is not None,
        "provider cache identity",
    )
    observed = vector.get("values", [])
    expected = embed(hit["body"])
    require(len(observed) == len(expected) == 384, "vector dimensions")
    require(
        all(type(x) in (float, int) and math.isfinite(x) for x in observed + expected),
        "nonfinite vector",
    )
    error = sum((observed[i] - expected[i]) ** 2 for i in range(384))
    require(error <= 1e-5, "independent vector mismatch")
    return (snapshot["index_generation"], snapshot["index_fingerprint"])


def validate_space(app, queries, index, model, sources, embed, receipts):
    before = app.inspection_witness(index)
    policy = before["policy_version"]
    seen = set()
    recipe = None
    for tenant in ("alpha", "beta"):
        result = app.query(tenant, queries[0], index)
        require(result["available"], "serving unavailable")
        for hit in result["hits"]:
            identity = hit["id"]
            require(
                identity in sources and identity not in seen,
                "unexpected or duplicate document",
            )
            require(hit["tenant"] == tenant, "unauthorized document")
            snapshot = app.inspect_vectors(identity, index)
            receipt = {
                "index": index,
                "document_id": identity,
                "expected_source": dict(sources[identity]),
                "expected_model": model,
                "expected_witness": before,
                "serving_hit": hit,
                "snapshot": snapshot,
                "verification": "pending",
            }
            receipts.append(receipt)

            def independent_embedding(text, record=receipt):
                expected = embed(text)
                record["expected_embedding"] = expected
                return expected

            try:
                current = verify_snapshot(
                    snapshot,
                    hit,
                    index,
                    model,
                    policy,
                    sources[identity],
                    independent_embedding,
                    before,
                )
                require(recipe is None or recipe == current, "mixed index incarnations")
            except Exception as exc:
                receipt.update(verification="failed", failure=str(exc)[:4096])
                raise
            receipt["verification"] = "accepted"
            recipe = current
            seen.add(identity)
    require(seen == set(sources), "incomplete document set")
    deleted = app.inspect_vectors("temporary", index)
    if deleted is not None:
        receipts.append(
            {
                "index": index,
                "document_id": "temporary",
                "snapshot": deleted,
                "expected_witness": before,
                "verification": "failed",
                "failure": "deleted document retained",
            }
        )
    require(deleted is None, "deleted document retained")
    for tenant in ("alpha", "beta"):
        try:
            app.inspect_vectors(next(iter(sources)), index, tenant=tenant)
        except OSError as exc:
            require("HTTP 403 " in str(exc), "inspection authorization response")
        else:
            raise VectorOracleError("ordinary reader can inspect raw vectors")
    require(
        before == app.inspection_witness(index),
        "catalog changed during vector inspection",
    )
    return True


def negative_controls(snapshot, source, embed):
    """Challenge the oracle using a real snapshot that already passed acceptance."""
    import copy

    hit = {"id": snapshot["document_id"], **json.loads(snapshot["document_json"])}
    reference = embed(hit["body"])
    expected_model = snapshot["vectors"][0]["model"]
    controls = {}
    mutations = {
        "wrong-model": lambda item: item["vectors"][0].update(model="wrong-model"),
        "wrong-vector": lambda item: item["vectors"][0]["values"].__setitem__(
            0, item["vectors"][0]["values"][0] + 0.01
        ),
        "missing-vector": lambda item: item.update(vectors=[]),
        "unpublished": lambda item: item.update(current=False),
        "dirty-checkpoint": lambda item: item.update(checkpoint_status="dirty"),
        "replay-debt": lambda item: item.update(
            applied_sequence=item["replay_target_sequence"] - 1
        ),
        "wrong-checkpoint-recipe": lambda item: item.update(
            checkpoint_config_hash=item["index_config_hash"] + 1
        ),
        "stale-policy": lambda item: item.update(policy_version="0" * 64),
        "deleted-identity": lambda item: item.update(document_id="temporary"),
    }
    for name, mutate in mutations.items():
        candidate = copy.deepcopy(snapshot)
        mutate(candidate)
        try:
            verify_snapshot(
                candidate,
                hit,
                snapshot["index"],
                expected_model,
                snapshot["policy_version"],
                source,
                lambda _: reference,
            )
        except VectorOracleError:
            controls[name] = True
        else:
            controls[name] = False
    stale = {**source, "version": source["version"] + 1}
    try:
        verify_snapshot(
            snapshot,
            hit,
            snapshot["index"],
            expected_model,
            snapshot["policy_version"],
            stale,
            lambda _: reference,
        )
    except VectorOracleError:
        controls["stale-source"] = True
    else:
        controls["stale-source"] = False
    return controls
