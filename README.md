# Claude Workspace Ops MCP

An MCP server that lets an AI agent tidy a Google Drive workspace — without ever
giving it the authority to do so unsupervised.

The interesting part is not that Claude can propose a folder structure. It's
that **a fully compromised model cannot cause an unsafe write**, and the tests
prove it rather than assert it.

```
Agent proposes  ──▶  Deterministic validator  ──▶  Human approves (separate process)  ──▶  Apply
   (may lie)            (cannot be argued with)         (no tool can do this)
```

---

## The three claims this project makes

**1. Least privilege is structural, not procedural.**
The only OAuth scope requested is `drive.file` — Google's one *non-sensitive*
Drive scope. It grants per-file access to files this app created or that the
user explicitly opened/shared with it. The rest of the user's Drive is not
"forbidden", it is **invisible**: a request for an ungranted file returns 404,
not 403. There is no configuration option to widen this, and a stored token that
somehow carries a broader scope is [rejected at load
time](src/cwops/drive/auth.py) as a configuration error.

**2. The model advises; deterministic code decides.**
Claude is consulted in exactly two places, and it answers in a schema that
*cannot express an unsafe action*. It returns folder names and file assignments
— there is no field for "rename", "delete", or "put this outside the workspace".
Deterministic code then compiles that draft into operations whose parent folder
comes from configuration, never from the model, and re-validates every one.

**3. Approval lives in a different trust zone than the agent.**
An approval record is created only by `cwops approve` — a CLI command in a
separate process that refuses to run without an interactive terminal, `--yes`
included. No MCP tool creates one, and a shell-capable agent gets a pipe rather
than a TTY, so an agent driving this server has no code path to approval. It can
ask; it cannot produce.

---

## Try it in 30 seconds, with no Google account

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"   # Windows: .venv\Scripts\pip
cwops demo
```

`cwops demo` runs the entire pipeline against an in-memory Drive and an offline
organizer, narrating each gate: what the app can see, deterministic duplicate
detection, a proposed plan, a dry run, **a real run refused for lack of
approval**, a human approval, a successful run, a replay refused because
approvals are single-use, and the resulting audit trail.

It is also a test (`test_demo_runs_the_whole_pipeline_offline`), so the
walkthrough in [docs/demo.md](docs/demo.md) cannot rot.

```
========================================================================
5. Real run WITHOUT approval  (must be refused)
========================================================================
  Refused: [approval_required] Proposal prop_241cb99db035 has no human
           approval. Run: cwops approve prop_241cb99db035
  Drive mutations so far: 0
```

---

## The tools

Nine tools. Eight are read-only. One can write.

| Tool | Model? | Writes? |
|---|---|---|
| `drive_search` | no | no |
| `drive_list_folder` | no | no |
| `drive_read_file` | no | no |
| `find_duplicates` | no | no |
| `classify_documents` | **yes** | no |
| `propose_organization` | **yes** | no |
| `propose_operations` | no | no |
| `get_proposal` | no | no |
| `apply_proposal` | no | **yes — the only one** |

`apply_proposal` defaults to `dry_run=true`. A real run additionally requires
`CWOPS_ALLOW_MUTATIONS=true` **and** an unconsumed, unexpired approval bound to
that exact plan.

**There is no delete, trash, overwrite, permission or sharing capability
anywhere in this server.** Only create-folder, rename and move exist, and all
three are reversible. That is a deliberate MVP boundary, not an oversight — see
[Deliberate limits](#deliberate-limits).

### Duplicate detection is honest about what it can't know

- `checksum` — exact match on Drive's `md5Checksum` + size. Trustworthy.
- `metadata` — normalized title + size + MIME type. Always labelled `heuristic`.

Google-native Docs/Sheets/Slides **have no checksum at all**. Rather than
silently treating them as unique, they are reported in a separate `unscannable`
list with the reason. The `metadata` strategy exists precisely to catch the
copies that `checksum` structurally cannot.

---

## Running it against real Google Drive

```bash
cp .env.example .env          # then edit
cwops auth                    # browser consent, drive.file only
cwops workspace init          # creates the app-owned root folder
                              # -> prints CWOPS_ROOT_FOLDER_ID to paste into .env
cwops workspace seed          # optional: demo files, incl. an exact duplicate pair
```

You need a Google Cloud project with the Drive API enabled and an **OAuth client
ID of type "Desktop app"**, downloaded as `client_secret.json`. Because
`drive.file` is non-sensitive, **no verification review is required and refresh
tokens do not expire on the 7-day testing-mode clock** that restricted scopes
impose. This is the practical payoff of the scope choice.

To bring an *existing* file under the app's control, either move it into the
workspace folder or open it with this app; then `cwops grant <file_id|url>`
verifies and records the grant. Programmatic file-picking (Google Picker) is
deliberately deferred — the `GrantSource.PICKER` value already exists so adding
it later needs no schema change.

### Wiring it into an MCP client

```json
{
  "mcpServers": {
    "workspace-ops": {
      "command": "C:\\Projects\\claude-workspace-ops-mcp\\.venv\\Scripts\\cwops-mcp.exe",
      "env": {
        "CWOPS_DRIVE_BACKEND": "google",
        "CWOPS_AI_BACKEND": "claude",
        "CWOPS_ROOT_FOLDER_ID": "<from workspace init>",
        "CWOPS_ALLOW_MUTATIONS": "true"
      }
    }
  }
}
```

Transport is stdio, so **stdout carries the JSON-RPC stream**. Every log line
goes to stderr as single-line JSON with a correlation ID; a stray `print()`
would corrupt the protocol.

---

## Where Claude is used — and where it deliberately isn't

| Concern | Handled by |
|---|---|
| Classifying an ambiguous document | **Claude** (`claude-opus-5`, adaptive thinking, low effort) |
| Suggesting a folder taxonomy | **Claude** |
| Everything else | Deterministic Python |

"Everything else" means: authorization, workspace containment, grant checks,
name policy, duplicate detection, plan compilation, approval, execution
ordering, retry, and audit. None of it consults a model, and none of it can be
influenced by one.

Claude's output is treated as **untrusted input**. File names and contents are
attacker-controllable, so a persuasive document could talk the model into
suggesting something hostile. The system prompt says to ignore embedded
instructions — but that is noise reduction, not the defence. The defence is that
[the validator](src/cwops/rules/validate.py) rejects the result afterwards
regardless of what the model was persuaded to say.

`tests/test_ai_boundary.py` demonstrates this with an organizer that returns
deliberately hostile plans.

---

## Architecture

```
src/cwops/
  config.py          env vars; drive.file scope hard-coded; fails fast
  logging.py         structured JSON to stderr + secret scrubbing
  models.py          the MCP contract (Pydantic -> published JSON Schema)
  clock.py           injectable time, so TTLs are tested not slept through
  container.py       dependency wiring: real backends vs. offline fakes
  workspace.py       read-side ops; keeps the grant ledger honest
  provision.py       CLI-only workspace bootstrap (2nd of 2 write sites)
  server.py          MCP wiring only
  cli.py             the human half of the approval gate
  drive/             DriveClient Protocol · OAuth · Google impl · retry · fake
  ai/                Organizer Protocol · Claude impl · prompts · fake
  rules/             naming · duplicates · validate   (pure functions, no I/O)
  store/             SQLite: proposals · approvals · grants · audit
  tools/             read (8 read-only) · organize · apply (the write path)
```

Two `Protocol` seams — `DriveClient` and `Organizer` — are what let every safety
gate be **built and fully tested before any real Google or Anthropic code
existed**, and what let the whole system run offline today.

Full data flow and trust zones: [docs/architecture.md](docs/architecture.md).
The invariants and the threat model: [docs/safety-model.md](docs/safety-model.md).

---

## Testing

```bash
pytest                                   # 240 passed, 1 skipped, ~7s, no network
ruff check . && mypy                     # both clean
pytest --cov=cwops                       # 89% overall; rules/ and store/ 96-100%
```

| Suite | What it proves |
|---|---|
| `test_approval_gate.py` | No tool can create an approval · single-use · TTL · plan-hash binding · tampering invalidates · operator switch · world-changed detection |
| `test_validate.py` | 23 adversarial plans from a JSON fixture — hallucinated IDs, path traversal, cycles, escapes — all rejected |
| `test_ai_boundary.py` | A hostile organizer cannot cause an unsafe operation |
| `test_dry_run.py` | Zero mutating calls across every read tool and every dry run — asserted against the fake's call log, plus a source-level check that only `apply.py` and `provision.py` mutate |
| `test_duplicates.py` | Exact vs. heuristic; Google-native files unscannable, not "unique"; output is deterministic |
| `test_retry.py` | Backoff bounded and jittered; 404 never retried; Drive query values escaped against injection |
| `test_auth_scope.py` | Only `drive.file` requested; a broader token is refused |
| `test_audit.py` | One row per call; refusals recorded; **no secret can reach the table** |
| `test_tool_schemas.py` | Golden snapshot of the published MCP contract |

No test touches the network. Two fakes at the Protocol seams make that possible
without mocking libraries.

---

## Deliberate limits

Documented as scope, not omissions:

- **No destructive operations.** No delete, trash, overwrite, permissions or
  sharing. Everything the MVP can do is reversible.
- **No Google Picker.** Existing files come under management by being moved into
  the workspace or opened with the app. The `picker` grant source is reserved.
- **No Shared Drives**, no multi-user, no concurrent proposals across processes.
- **No undo journal.** Rename and move are reversible in principle; a one-command
  rollback is not built.
- **stdio transport only.** No HTTP/SSE.
- **A partially-invalid plan is trimmed, not rejected.** Rejected operations are
  dropped and shown in full — with reason codes — in the proposal, the preview,
  and the audit log, so the human approves an explicit list. A plan with nothing
  left cannot be approved at all.

## Secrets

`.env`, `client_secret*.json`, `*token.json` and `*.db` are gitignored. The
Anthropic SDK reads `ANTHROPIC_API_KEY` from the environment directly; no
credential is ever passed through this code. The audit log and every log line
are scrubbed on the way out — `test_audit.py` asserts that a credential passed
in by mistake cannot reach the database.
