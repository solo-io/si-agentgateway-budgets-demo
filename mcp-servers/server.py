#!/usr/bin/env python3
"""Three small synthetic MCP servers for the demo, one process each.

MCP_SERVER selects which one runs:
  tickets  Jira-like issues for the Atlas, Beacon and Cobalt projects
  docs     design docs and runbooks
  repo     code search over fictional repositories

All data is fake. Every server speaks streamable HTTP on PORT at /mcp.
"""

import os

from mcp.server.fastmcp import FastMCP

SERVER = os.environ.get("MCP_SERVER", "tickets")
PORT = int(os.environ.get("PORT", "8000"))

mcp = FastMCP(SERVER, host="0.0.0.0", port=PORT, stateless_http=True)

TICKETS = [
    {"id": "ATL-101", "project": "atlas", "status": "open", "title": "Claims API returns 500 on duplicate claim number", "assignee": "diego.ramos"},
    {"id": "ATL-104", "project": "atlas", "status": "open", "title": "Map legacy CLMHDR copybook to the claims API schema", "assignee": "maria.gomez"},
    {"id": "ATL-112", "project": "atlas", "status": "in_progress", "title": "Claims API pagination for adjuster dashboard", "assignee": "ana.lopez"},
    {"id": "ATL-118", "project": "atlas", "status": "done", "title": "Summarizer prompt: redact policy numbers", "assignee": "maria.gomez"},
    {"id": "BCN-020", "project": "beacon", "status": "open", "title": "Generate regression suite for the quote engine", "assignee": "sam.ortiz"},
    {"id": "BCN-027", "project": "beacon", "status": "open", "title": "Flaky test: premium rounding on leap years", "assignee": "priya.nair"},
    {"id": "BCN-031", "project": "beacon", "status": "in_progress", "title": "Test data generator for multi-vehicle policies", "assignee": "maria.gomez"},
    {"id": "CBT-007", "project": "cobalt", "status": "open", "title": "Content agent drifts from style guide on long-form", "assignee": "kate.reed"},
    {"id": "CBT-009", "project": "cobalt", "status": "open", "title": "Headline A/B variants for the autumn campaign", "assignee": "luis.mora"},
]

DOCS = [
    {"id": "atlas-claims-api-design", "project": "atlas", "title": "Atlas: claims API design",
     "body": "The claims API exposes /claims and /claims/{id}/documents. Claim numbers are unique per carrier; "
             "duplicates must return 409, not 500 (see ATL-101). Pagination is cursor-based."},
    {"id": "atlas-migration-runbook", "project": "atlas", "title": "Atlas: COBOL migration runbook",
     "body": "Each copybook maps to one DTO. The code migrator agent proposes the mapping; a human approves it."},
    {"id": "beacon-test-strategy", "project": "beacon", "title": "Beacon: test generation strategy",
     "body": "Generated tests run in a quarantine lane for two days before they join the main suite."},
    {"id": "cobalt-style-guide", "project": "cobalt", "title": "Cobalt: content style guide",
     "body": "Short sentences. No superlatives. Headlines under 70 characters."},
]

FILES = {
    ("atlas-claims", "src/claims/handler.py"): "def create_claim(req):\n    # TODO ATL-101: return 409 on duplicate\n    return repo.insert(req)\n",
    ("atlas-claims", "legacy/CLMHDR.cpy"): "01 CLM-HDR.\n   05 CLM-NUM    PIC X(12).\n   05 CLM-DATE   PIC 9(8).\n",
    ("beacon-quote", "tests/test_premium.py"): "def test_leap_year_rounding():\n    assert premium(date(2028, 2, 29)) == Decimal('412.10')\n",
    ("cobalt-content", "agents/headline.py"): "MAX_HEADLINE = 70\n",
}

if SERVER == "tickets":

    @mcp.tool()
    def list_tickets(project: str = "", status: str = "") -> list[dict]:
        """List tickets, optionally filtered by project (atlas, beacon, cobalt) and status (open, in_progress, done)."""
        return [t for t in TICKETS
                if (not project or t["project"] == project.lower())
                and (not status or t["status"] == status.lower())]

    @mcp.tool()
    def get_ticket(ticket_id: str) -> dict:
        """Get one ticket by ID, for example ATL-101."""
        for t in TICKETS:
            if t["id"].lower() == ticket_id.lower():
                return t
        return {"error": f"no ticket {ticket_id}"}

    @mcp.tool()
    def search_tickets(query: str) -> list[dict]:
        """Full-text search over ticket titles."""
        q = query.lower()
        return [t for t in TICKETS if q in t["title"].lower()]

elif SERVER == "docs":

    @mcp.tool()
    def search_docs(query: str, project: str = "") -> list[dict]:
        """Search design docs and runbooks. Returns id, title and a snippet."""
        q = query.lower()
        return [{"id": d["id"], "title": d["title"], "snippet": d["body"][:160]}
                for d in DOCS
                if (not project or d["project"] == project.lower())
                and (q in d["title"].lower() or q in d["body"].lower())]

    @mcp.tool()
    def get_doc(doc_id: str) -> dict:
        """Get the full text of one doc by ID."""
        for d in DOCS:
            if d["id"] == doc_id:
                return d
        return {"error": f"no doc {doc_id}"}

elif SERVER == "repo":

    @mcp.tool()
    def search_code(query: str, repo: str = "") -> list[dict]:
        """Search code across the project repositories."""
        q = query.lower()
        return [{"repo": r, "path": p, "match": next((l for l in body.splitlines() if q in l.lower()), "")}
                for (r, p), body in FILES.items()
                if (not repo or r == repo) and q in body.lower()]

    @mcp.tool()
    def get_file(repo: str, path: str) -> str:
        """Read one file from a repository."""
        return FILES.get((repo, path), f"not found: {repo}/{path}")

else:
    raise SystemExit(f"unknown MCP_SERVER {SERVER}")


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
