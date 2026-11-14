# Extended native Antfly controls: extended-v2

This frozen synthetic subset checks recorded-decision replay, explicit remote
provider identity and protected downstream privacy on the official Antfly
v0.3.0 ARM64 image. It uses the same owned-container mechanics and declared
4 CPU / 12 GiB / nofile=65536:524288 engine configuration as lifecycle-v1, with
one 0.5 CPU / 256 MiB private provider. No hosted service is called.

```sh
python3 -m unittest discover -s evaluations/lifecycle-extended/scripts -p test_extended.py
python3 evaluations/lifecycle-extended/scripts/run.py --out results/extended-antfly-01
```

The provider returns a different decision on every invocation. The driver reads
a committed artifact, makes the decision endpoint fail, abruptly restarts the
server and checks that the recorded value and model-call count survive. It
then repeats the identical source. This qualifies post-commit reuse; it does
not locate a response-before-commit boundary or promise exactly-once calls.

A separate index explicitly pins identity A. Positive ingestion precedes
wrong and missing acknowledgement controls. Hashed provider inputs prove the
target request occurred, queries must not publish that target, and coverage
must remain incomplete. Restoring a provider is not an in-place index migration.
These controls assume truthful provider identities and do not defend against
a provider falsely attesting to different weights.

The perfect detector knows one planted synthetic email. Tests check protected
vector-returned text, full-text matching and recorded embedder traffic, with
nonempty positive retrieval and actual detector/embedder calls. The detector
and original storage are allowed to receive the original. A replacement policy
uses its exact If-Match ETag. A proven held upstream detector gates a concurrent
read until release, after which sanitized retrieval and deletion are checked.

This is not a detector-accuracy study or an exhaustive storage inventory. Raw
originals, historical versions, backups, compressed files, caches, logs, graph,
sparse/SQL/agent surfaces and peer extended controls are not inspected by this
subset. The result lists remaining work. No PII-copy-elimination or real-model
throughput claim follows. Keep this separate from lifecycle-v1 peer controls;
passing Antfly-only controls is not a peer architecture ranking.

Each fresh run emits bounded result/provenance records and checksums. Failed
state is retained; successful owned resources are reclaimed. Policy API or
fixture setup failures remain failed attempts, not product findings.

Version 2 adds a stronger source-supersession schedule: hold v2, commit v3,
release v2 while v3's response is still proven held, and reject any served v2
vector. The previous published v1 is allowed until the derived-ready barrier.
It also releases a captured B reply after the provider alias returns to A,
requiring a restored-A positive read and zero incompatible vectors. These
remain bounded observations, not a formal proof or distributed cutover test.
