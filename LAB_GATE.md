# Live Gate Checklist

Normal live execution must remain disabled until this checklist passes on the exact pinned `agy` image used for production.

The account and project pools are dynamic. Test with the concurrency you actually intend to use; do not assume that a pass at a smaller scale validates a larger scale.

1. Bind every account selected for the test and verify one stable identity hash per master volume.
2. Run one lane on one account for at least 70 minutes and observe at least one token refresh.
3. Run two concurrent lanes on the same account for at least 70 minutes; both must retain the bound identity.
4. Repeat the concurrent-refresh test for every account intended for live use.
5. Run a full-scale lab wave using the intended `per_account_slots` and `max_workers` values.
6. Require zero foreign identity events, zero host-HOME mounts, and zero cross-lane credential writes.
7. Kill the controller during a lab run; within 30 seconds there must be no containers carrying that run label and no disposable lane volumes.
8. Verify no lane changed Git HEAD directly, pushed, merged to main, or deployed.
9. Repeat the full-scale test twice on the same pinned image.
10. Any `agy` version change invalidates the gate and requires re-validation.

Known open validation item: cloned OAuth refresh-token state must remain valid when several clones refresh concurrently. This cannot be proven by static inspection.
