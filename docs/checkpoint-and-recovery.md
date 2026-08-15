# Checkpoint and recovery

Checkpoints contain structured facts, failures, next steps, and artifact references. Writes are atomic and integrity checked. Worker provider boundaries and control transitions create durable checkpoint records.

On startup, the Store validates the newest durable replay snapshot (falling
back to an older generation when necessary), replays only newer journal
records, quarantines a corrupt journal tail, rejects non-tail corruption, and
reclaims stale claims. A safe checkpoint can requeue an attempt or create a
new recovery Attempt with explicit lineage; an unsafe or missing checkpoint
blocks recovery.

Snapshot creation is atomic and content-hashed. Physical journal compaction is
currently an explicit safe no-op: the journal remains retained because there
is not yet an immutable segment-retention policy that would make deleting old
bytes recoverable after snapshot corruption.

SIGKILL-equivalent tests cover claim recovery and journal continuation. Full production process supervision and multi-process recovery remain outside this local service slice.
