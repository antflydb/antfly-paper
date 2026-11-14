# Configured descriptor-limit attempt: paper-default-v030-v5

The inputs, calibration/holdout partitions, index definitions, candidate grids,
search setting, query counts, engine/client caps, phase ordering, deadlines and
three-lifecycle rotation are exactly those of [v4](protocol.md). The Antfly
subject remains the official v0.3.0 image and actual default search effort 0.5.

The one declared resource change is `nofile=65536:524288`, applied to every
owned container with `PAPER_CONTAINER_NOFILE`. Set
`PAPER_PROTOCOL=paper-default-v030-v5` explicitly; the harness rejects this
override under v4 and records it in provenance. Labels also identify v5.

This is an operational workaround for antflydb/antfly#1670, not a fixed release
or proof of bounded descriptor consumption. The 100k diagnostic passed at this
limit; 1M quality, timings and errors still require actual measurement. Preserve
v4's failures and peer outcomes; do not pool lifecycle timings across versions.
Use a new output root. No settings are changed after observing holdout.
