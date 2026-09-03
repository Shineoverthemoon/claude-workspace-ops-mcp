# Demo

Two walkthroughs: one that needs nothing, and one against real Google Drive.

---

## 1. Offline walkthrough (no Google account, no API key)

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"    # Linux/macOS: .venv/bin/pip
.venv/Scripts/cwops demo
```

This runs the **real** server, tools, validator, approval store and audit log
against an in-memory Drive (`src/cwops/drive/seed_data.json`) and an offline
keyword organizer. Only the two Protocol seams are substituted.

It is also `test_demo_runs_the_whole_pipeline_offline`, so this transcript
cannot drift from the code.

### What you'll see

**Step 1 — what the app can reach.** 19 files. The seed also contains
`f-not-granted` ("Confidential HR Review.pdf") sitting *in the same folder*,
which never appears: under `drive.file` it is invisible, not merely forbidden.

```
========================================================================
1. What can this app see?  (drive.file scope only)
========================================================================
  fol-inbox              Inbox
  f-inv-q1               Invoice Q1 2026.pdf
  f-inv-q1-copy          Copy of Invoice Q1 2026.pdf
  f-inv-q1-1             Invoice Q1 2026 (1).pdf
  ...
  19 files reachable.
  Files outside the app's grant are invisible - not merely forbidden.
```

**Step 2 — deterministic duplicates.** Three byte-identical invoices found by
checksum. Three Google-native docs reported as *unscannable* rather than
silently assumed unique:

```
  [exact] Invoice Q1 2026.pdf, Copy of Invoice Q1 2026.pdf, Invoice Q1 2026 (1).pdf

  3 file(s) have no checksum (Google-native) and are reported as unscannable,
  not as unique.
```

**Step 3 — a proposal, in plain language.** Claude (here, the offline stand-in)
suggests groupings; deterministic code compiles and validates them:

```
  Proposal prop_4a4f5c437d6a
    CREATE FOLDER  'Invoices'  in  'Claude Workspace Ops'
    MOVE           'Invoice Q1 2026.pdf'  from  'Inbox'  ->  'Invoices' (new)
    ...
  accepted=20  rejected=0
  Nothing has been changed.
```

**Steps 4–5 — the gates.** A dry run touches nothing; a real run without
approval is refused:

```
  dry_run=True  applied=False  approved=False
  Drive mutations so far: 0

  Refused: [approval_required] Proposal prop_4a4f5c437d6a has no human approval.
           Run: cwops approve prop_4a4f5c437d6a
  Drive mutations so far: 0
```

**Steps 6–8 — approve, apply, and fail to replay:**

```
  Approved by demo-operator. Single-use, expires 2026-09-03T03:52:37+00:00.

  applied=True  executed=20  failed=0

  Refused: [proposal_not_pending] Proposal prop_4a4f5c437d6a is already applied.
```

**Step 9 — the audit trail.** One row per call, refusals included:

```
  0c180cf4158e  mcp_client drive_list_folder      ok
  e592312616fb  mcp_client find_duplicates        ok
  323cc2280f7e  mcp_client propose_organization   ok
  d9dd8afc639d  mcp_client apply_proposal         ok
  f8c78539c606  mcp_client apply_proposal         refused   reason=approval_required
  4b21e0aa71c3  cli        cwops_approve          ok        approved_by=demo-operator
  7cc1b60c0222  mcp_client apply_proposal         ok        approved_by=demo-operator
  a956948640da  mcp_client apply_proposal         refused   reason=proposal_not_pending

  8 audit rows, one per tool call.
```

### Driving it by hand instead

```bash
export CWOPS_DRIVE_BACKEND=fake CWOPS_AI_BACKEND=fake
export CWOPS_ROOT_FOLDER_ID=root-workspace CWOPS_ALLOW_MUTATIONS=true

cwops-mcp                      # the MCP server, stdio
# ...from your MCP client: propose_organization(folder_id="fol-inbox")

cwops list                     # see the pending proposal
cwops approve <proposal_id>    # shows the preview, asks for confirmation
cwops audit                    # the trail
cwops audit --export audit.jsonl
```

---

## 2. Against real Google Drive

### One-time setup

1. Create a Google Cloud project and **enable the Drive API**.
2. Configure the OAuth consent screen (External is fine; add yourself as a test
   user). Because `drive.file` is non-sensitive, **no verification review is
   required**.
3. Create an OAuth client ID of type **Desktop app** and download it as
   `client_secret.json` in the repo root.
4. `cp .env.example .env`.

### Authorize and provision

```bash
cwops auth
# → browser consent. Confirm the scope shown is ONLY:
#   https://www.googleapis.com/auth/drive.file

cwops workspace init
# → Created workspace folder: Claude Workspace Ops  (1AbC...)
#   Add this to your .env:
#     CWOPS_ROOT_FOLDER_ID=1AbC...

cwops workspace seed     # optional demo files, incl. an exact duplicate pair
cwops grants             # what the app can reach
```

Then set in `.env`:

```
CWOPS_DRIVE_BACKEND=google
CWOPS_AI_BACKEND=claude
CWOPS_ROOT_FOLDER_ID=1AbC...
CWOPS_ALLOW_MUTATIONS=true
ANTHROPIC_API_KEY=sk-ant-...
```

### The live loop

```bash
cwops-mcp                                    # terminal 1: the MCP server
# agent calls: drive_list_folder → find_duplicates → propose_organization
#              → apply_proposal (dry run)

cwops list                                   # terminal 2
cwops approve <proposal_id>                  # review the preview, confirm

# agent calls: apply_proposal(proposal_id=..., dry_run=false)
cwops audit
```

### Bringing an existing file under management

`drive.file` covers files the app created **and** files the user explicitly
opens or shares with it. Without Google Picker (deliberately deferred), the
practical route is:

- move the file into the workspace folder in the Drive UI, **or** open it with
  this app;
- then `cwops grant <file_id-or-URL>` to verify and record the grant.

`cwops grant` does not *create* access — only you can. It probes whether access
exists and registers it, and says so plainly when it doesn't:

```
No access to 1XyZ...: File not found: 1XyZ...

Under the drive.file scope this app can only see files it created or that you
explicitly opened/shared with it. Move the file into the app's workspace folder,
or open it with this app, then retry.
```

---

## Things worth trying

| Try this | What should happen |
|---|---|
| `apply_proposal(dry_run=false)` before approving | Refused, `approval_required`, zero mutations |
| Approve, then approve again and apply twice | Second apply refused — approvals are single-use |
| Approve, wait past `CWOPS_APPROVAL_TTL_SECONDS`, apply | Refused, `approval_invalid` (expired) |
| Set `CWOPS_ALLOW_MUTATIONS=false` and apply an approved plan | Refused, `mutations_disabled`; the approval is *not* burned |
| `propose_operations` with a made-up file ID | Rejected at validation with `unknown_file` |
| `propose_operations` with folder name `"../escape"` | Rejected with `invalid_name` |
| Move a file in the Drive UI between approval and apply | Refused, `plan_rejected` — the world changed |
| Point `find_duplicates` at Google Docs | They appear under `unscannable`, not as unique |
