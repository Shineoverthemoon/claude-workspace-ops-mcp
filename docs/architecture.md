# Architecture

## The organizing principle

Deterministic Python owns every state transition, validation, authorization
check and safety gate. A language model is consulted only where probabilistic
judgment genuinely adds value — deciding that "FY26 Budget Model" belongs with
finance documents — and its answer is treated as untrusted input on the way
back.

The practical test applied to every feature: *if the model returned the worst
possible answer here, what breaks?* If the answer is "something unsafe", the
decision does not belong to the model.

## Trust zones

```
┌─ Zone D — TRUSTED: the operator ───────────────────────────────────────────┐
│  terminal:    cwops approve <proposal_id>   ← the ONLY source of approvals │
│               cwops auth / workspace init   ← the ONLY provisioning path   │
│  filesystem:  OAuth token (0600), SQLite state, ANTHROPIC_API_KEY          │
└────────────────────────────┬───────────────────────────────────────────────┘
                             │ writes an approval row
┌─ Zone C — DETERMINISTIC CORE (no model, no I/O in rules/) ─────────────────┐
│  rules/validate · rules/naming · rules/duplicates                          │
│  store/proposals (approval semantics) · store/grants · store/audit         │
│  Enforces every invariant below. Cannot be persuaded.                      │
└────────────────────────────┬───────────────────────────────────────────────┘
        ▲ proposals only     │ executes approved plans only
┌─ Zone B — SEMI-TRUSTED: the MCP client / agent ────────────────────────────┐
│  May call read tools. May create proposals. May request a dry run.         │
│  CANNOT approve · CANNOT mutate directly · CANNOT widen scope              │
└────────────────────────────┬───────────────────────────────────────────────┘
┌─ Zone A — UNTRUSTED: Drive content and file names ─────────────────────────┐
│  A prompt-injection vector. Flows INTO Claude; never out into an action    │
│  without passing Zone C.                                                   │
└────────────────────────────────────────────────────────────────────────────┘
```

Zone A deserves emphasis. A document in the user's Drive can contain text
addressed to the model. The prompts instruct Claude to treat file content as
data, but that instruction is *noise reduction*, not the control. The control is
that Zone C re-checks the result: a plan influenced by injected text fails the
same validation as any other bad plan.

## Data flow: `propose_organization` → `apply_proposal`

```
 1  propose_organization(folder_id)
 2    workspace.list_folder(folder_id)              deterministic
 3      └─ every returned file is recorded in the grant ledger
 4    drive.export_text(...) per file, capped        deterministic
 5    organizer.classify(files, snippets)            ▓ CLAUDE ▓
 6    organizer.suggest_taxonomy(...) -> DraftPlan   ▓ CLAUDE ▓
 7    _compile_draft(draft) -> [Operation]           deterministic
 8      └─ parent_id is ALWAYS the configured root, never from the model
 9    workspace.snapshot(files)                      deterministic
10      └─ walks ancestors so containment can be PROVED, not assumed
11    validate_plan(ops, ctx)                        deterministic  ← the gate
12    persist Proposal{ops, plan_hash, preview, validation}
13    return the proposal.  NOTHING HAS BEEN WRITTEN.

14  [human]  cwops approve <id>        separate process, Zone D
15             └─ refuses if validation failed or the proposal isn't pending
16             └─ creates a single-use, TTL-bound approval for THIS plan_hash

17  apply_proposal(id, dry_run=false)
18    proposal must be PENDING
19    gate 1: dry_run is false (default is true)
20    gate 2: CWOPS_ALLOW_MUTATIONS is true
21    _revalidate() against FRESHLY FETCHED Drive state
22    gate 3: consume_approval() — atomic, single-use, hash-bound, unexpired
23    execute in order, binding #refs to real IDs, halting on first failure
24    set status APPLIED / FAILED; write the audit row
```

Step 21 matters more than it looks. An approval authorizes *a specific plan
against a specific world*. If a file moved, lost its grant, or left the
workspace between approval and execution, the plan no longer validates and the
approval does not apply.

## Why `DraftPlan` is not a list of operations

`propose_organization` could have asked Claude for operations directly. It
doesn't. The model answers in:

```python
class DraftPlan(BaseModel):
    folders:     list[DraftFolder]      # ref (#slug) + name
    assignments: list[DraftAssignment]  # file_id + folder_ref
    rationale:   str
```

There is no field for a rename, a deletion, or an arbitrary destination. The
model has **no vocabulary** for those actions, so refusing them is not a check
that could be bypassed — it is an absence. `_compile_draft` then supplies the
parent folder from configuration.

Layered on top: schema-level `pattern` constraints on refs, a folder cap, and
then full validation. Four layers, of which only the last is a "check".

## Plan-local refs

A useful plan must create a folder *and* move files into it, but the folder has
no Drive ID at planning time. Operations may therefore target a `#ref` declared
by an earlier `create_folder` in the same plan. The validator resolves ref
chains to a real anchor folder to check containment and detect cycles; the
executor binds refs to real IDs as folders are created.

Forward references are rejected — a ref must be declared by an *earlier*
operation.

## Persistence

One SQLite file, four tables: `proposals`, `approvals`, `grants`, `audit`.
Timestamps are fixed-width UTC ISO-8601 so lexicographic ordering matches
chronological ordering — approval expiry is enforced partly by a SQL string
comparison, and a variable-width format would make that subtly wrong.

The single-use guarantee is one statement:

```sql
UPDATE approvals SET consumed_at = ?
WHERE id = ? AND consumed_at IS NULL AND plan_hash = ? AND expires_at > ?
```

`rowcount != 1` means the claim was lost, and the apply is refused. Concurrent
applies cannot both win.

## The grant ledger

Google already refuses out-of-scope requests, so why keep a local record?

- **Answerability.** "What can this agent reach?" is a question you can answer
  offline by reading one table, rather than by probing an API.
- **Defence in depth.** The validator rejects an ungranted target *before* a
  call is attempted, so a stale or hallucinated ID never becomes a request.
- **Provenance.** Each grant records whether the file was app-created, verified,
  or (reserved) picker-selected.

`Workspace.observe()` records every file the Drive API actually returned, so the
ledger stays accurate without hand-maintenance.

## Observability

Every tool call gets a correlation ID and produces **exactly one** audit row —
enforced in `tools/base.py` rather than in each tool, so a new tool cannot
forget. Logs are single-line JSON on stderr. Both log fields and audit details
pass through `scrub()`, which redacts secret-shaped keys and values.

`ClaudeOrganizer` logs `input_tokens`, `output_tokens`,
`cache_read_input_tokens`, `stop_reason` and the request ID for every call.

## Errors and retry

Domain errors are typed and carry a stable `reason_code` that appears in the
audit log and in the message returned to the MCP client. Refusals
(`approval_required`, `mutations_disabled`, …) are audited as `refused`;
genuine failures as `error`.

Drive retries are transient-only: `408, 429, 500, 502, 503, 504` with jittered
exponential backoff, capped. **404 is never retried** — under `drive.file` it
means "not granted", and retrying would turn a clear authorization signal into a
slow timeout. `403` is also excluded, since from Drive it usually means quota.

`sleep` is injected, so the tests exercise the real backoff logic instantly.
