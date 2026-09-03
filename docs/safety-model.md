# Safety model

## What is being defended against

1. **A confused or hallucinating model** proposing operations on files that
   don't exist, or in places that don't make sense.
2. **Prompt injection via document content.** File names and contents come from
   the user's Drive and are attacker-controllable.
3. **An outright compromised agent** deliberately attempting to escalate — to
   approve its own plan, reach outside the workspace, or destroy data.
4. **Ordinary operational failure** — API errors, a file moving mid-plan, a
   half-applied plan.

The design assumption is (3): the agent is treated as an untrusted caller
throughout. Defending against (3) makes (1) and (2) fall out for free.

## Invariants

Each maps to at least one test.

| # | Invariant | Enforced in | Tested in |
|---|---|---|---|
| **I1** | `apply_proposal` is the only agent-reachable tool that mutates Drive | `tools/apply.py` | `test_dry_run.py` (incl. a source-level scan) |
| **I2** | A real run requires `dry_run=false` **and** `CWOPS_ALLOW_MUTATIONS=true` **and** an unconsumed, unexpired approval matching the current `plan_hash` | `tools/apply.py`, `store/proposals.py` | `test_approval_gate.py` |
| **I3** | Every target resolves to a descendant of the app-owned root, verified at propose time **and re-verified at apply time** | `rules/validate.py` | `test_validate.py`, `test_approval_gate.py` |
| **I4** | Every file ID in a plan must be one the server itself fetched for that plan | `rules/validate.py` | `test_validate.py`, `test_ai_boundary.py` |
| **I5** | No delete, trash, overwrite, permission or sharing operation exists | `models.py` (`Operation` union) | `test_tool_schemas.py` |
| **I6** | No secret reaches a log line, an audit row, or a tool result | `logging.scrub()` | `test_audit.py`, `test_config.py` |
| **I7** | Every tool call writes exactly one audit row | `tools/base.py` | `test_audit.py`, `test_approval_gate.py` |
| **I8** | Every target must hold a recorded `drive.file` grant | `rules/validate.py`, `store/grants.py` | `test_validate.py`, `test_store.py` |
| **I9** | Only `drive.file` is requested; a broader token is refused | `config.py`, `drive/auth.py` | `test_auth_scope.py` |

## Least privilege: why `drive.file`

`drive.file` is the only Drive scope Google classifies as **non-sensitive**. It
grants per-file access to files the app created, plus files the user explicitly
opened or shared with the app.

| | `drive` (full) | `drive.file` |
|---|---|---|
| Can enumerate the user's whole Drive | yes | **no** |
| Blast radius of a bug or injection | everything | the granted set |
| Google verification review | required | **not required** |
| Refresh-token lifetime in testing mode | **7 days** | normal |
| Can rename/move pre-existing files | any file | only granted files |

The cost is real: a file must be brought under management before the app can
touch it. That cost is the point — the reachable set is an explicit, auditable
list rather than "everything".

**Ungranted files return 404, not 403.** The scope hides existence rather than
reporting a denial, and the fake Drive used in tests reproduces that faithfully,
so tests exercising the boundary test the real contract.

Enforcement is doubled: Google refuses out-of-scope requests, *and* the local
grant ledger causes the validator to reject an ungranted target before a request
is ever made.

## Why the model cannot approve its own plan

An approval row is created by exactly one function, `ProposalStore.approve`,
reachable from exactly one caller, `cwops approve`. That is a CLI command in a
separate process, run by a human with filesystem access.

No token passes through the conversation, so there is nothing for the model to
observe, guess, or replay. It is not that the model is *forbidden* from
approving — there is no mechanism it could invoke.

Approvals are additionally:

- **plan-bound** — tied to a SHA-256 of the exact operation list plus root, so
  editing the plan after approval invalidates it;
- **single-use** — claimed by one atomic `UPDATE`; a lost race is a refusal;
- **time-limited** — `CWOPS_APPROVAL_TTL_SECONDS`, default 15 minutes;
- **world-bound** — the plan is re-validated against freshly fetched Drive state
  immediately before execution.

## Why prompt injection doesn't reach a write

The prompts tell Claude that file names and contents are data and that embedded
instructions must be ignored. **This is not the defence.** Prompts are
mitigations; they reduce noise and improve behaviour, but a sufficiently clever
document may still win an argument with a model.

The defence is that winning the argument achieves nothing:

1. The response schema cannot express a rename, a deletion, or a foreign
   destination.
2. `_compile_draft` supplies the parent folder from configuration.
3. The validator rejects unknown IDs, ungranted targets, names containing path
   separators, cycles, and anything outside the workspace.
4. A human sees a plain-language preview before anything runs.
5. `apply_proposal` re-validates against current Drive state.

`test_ai_boundary.py` substitutes an organizer that returns maximally hostile
plans and asserts that nothing unsafe survives.

## Failure handling

- **Partially invalid plan** — rejected operations are *dropped, never repaired*,
  and shown with reason codes in the proposal, the preview, the CLI approval
  screen, and the audit log. A human approves an explicit list. A plan with
  nothing left cannot be approved.
- **Mid-run failure** — execution halts at the first error; remaining operations
  are marked `skipped`; the proposal becomes `FAILED`. The approval has already
  been consumed, so retrying requires a fresh proposal and a fresh approval.
  That is intentional: a partially-applied plan is a *different* world, and the
  original approval no longer describes it.
- **Concurrent modification** — `expected_old_parent_id` gives optimistic
  concurrency on moves; a file that moved since planning produces a 409.
- **Unprovable containment** — if a file's parent chain leaves the known file
  set, containment is *unprovable* and the operation is refused. Fail closed.

## Known limits

Stated plainly rather than hidden:

- **The MCP client is trusted to be the intended one.** This server assumes the
  transport endpoint is the user's own agent; it does not authenticate callers.
- **The operator is trusted.** Anyone who can run `cwops approve` can approve
  anything, and anyone with write access to the SQLite file can forge an
  approval row. The database is a local, single-user trust boundary — not a
  tamper-proof ledger.
- **The audit log is append-only by convention**, not cryptographically chained.
- **`drive.file` does not protect files already granted.** A file the user
  opened with this app is genuinely reachable. Workspace containment (I3) is a
  *separate* control precisely because grants alone are not enough — and
  `test_validate.py` asserts that a granted file outside the workspace is still
  refused.
- **No rate limiting or spend cap** across sessions beyond per-proposal file
  caps and the Anthropic SDK's own retry behaviour.
- **Windows file permissions.** `os.chmod(0600)` on the token file only clears
  the read-only attribute on Windows; the real protection is the inherited
  user-profile ACL.
