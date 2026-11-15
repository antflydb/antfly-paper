# Frozen shard-checkpoint qualification

The Antfly candidate passed all 14 `document-main-v3` checks with fresh PDF
extraction and real native models. The independent vector comparison accepted
eight documents in each of B (`b1`), still-serving A (`a0`), and rollback A
(`a2`): 24 stored 384-dimensional vectors and three stable owner/catalog
witness pairs. Maximum squared distance from independently computed vectors
was 0.0 for this fixture (acceptance threshold: 1e-5). All ten deliberately
invalid controls were rejected.

The frozen instrument is `53ae5c8b19416b39733b642598170430d031404d`.
The subject is `194b8cca085997a9be6585fabd3b4368af358bee`, tree-identical
to Antfly #1712's merge `b2cf59fab747ee7d1218e76bddd5a1edf75a9eea`.
It excludes the later #1714 readiness optimization. The exact immutable image,
binary hash, model manifest hash, checks and receipt digests are recorded in the
[compact receipt](shard-checkpoint-53ae5c8.json).

The runner recorded `source_unchanged: true` and `fresh_pdf_frontend: true`.
All 18 entries in the original receipt manifest were independently rehashed
and verified. The previous failed run remains separate and immutable.
The full local archives are retained under
`results/shard-checkpoint-qualification-53ae5c8/` (ignored generated results).

This is an explicit eight-document, one-shard lifecycle qualification. It does
not establish matched PostgreSQL results for this instrument, comparative
latency/throughput, broad corpus quality, or an on-disk privacy audit.
