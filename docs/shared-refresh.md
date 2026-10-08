# Shared GPU scheduler integration

Search refresh requires the local Archon shared-lease API. Missing support
skips changed-file embedding and preserves indexed retrieval. Explicit bulk
indexing continues using its existing exclusive priority lease.

The companion patch in [integrations/archon-shared-refresh.patch](../integrations/archon-shared-refresh.patch)
adds the API to Archon with its existing background-companion support. It is
an integration patch, not an automatic installer. Apply with `git apply --check`
first in the compatible Archon checkout; preserve unrelated local edits.
Restart the scheduler only when no external/shared lease or queued GPU work
is active. Its resident LLM runs independently and should remain loaded.

Protocol on the loopback Archon endpoint:

- POST `/lease/shared/acquire` with the refresh child PID. Success returns a
  private token, `allocator_mib: 256`, and `batch_size: 8`. Searchfu validates
  these against its authoritative constants before loading CUDA.
- POST `/lease/shared/check` with the token before each batch and commit.
  A refusal stops embedding and preserves the previous file transaction.
- POST `/lease/shared/release` on every exit path. Archon retains the child's
  identity until process exit, so CUDA context release precedes conflicting work.

Tokens and process identities stay local and ephemeral. Status exposes only
an active boolean. Only the installed, same-user refresh child is eligible.
Shared work has a 60-second lease; CUDA loading/batches are cooperative.
Admission uses a 640 MiB free-memory floor; continuation requires 192 MiB free.
This complements Searchfu's allocator cap, which excludes CUDA library/context
memory. It is not a driver-enforced total-VRAM reservation.
