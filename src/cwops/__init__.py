"""Claude Workspace Ops MCP.

A least-privilege, approval-gated MCP server for Google Drive housekeeping.

Architecture rule: deterministic Python owns every state transition, validation,
authorization check and safety gate. Claude is confined to two advisory
functions whose output is treated as untrusted input.
"""

__version__ = "0.1.0"
