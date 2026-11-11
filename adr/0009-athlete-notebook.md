# ADR 0009 · The athlete notebook

Status: accepted

## Context

An athlete keeps standing documents that outlive any plan: a profile of
long-term traits, a log of the current block. Kept as files beside one client,
they are invisible to every other client of the server and to the athlete in
the app.

## Decision

- **Named pages in the database.** `notebook_pages (name, body, updated_at)`;
  the name is a slug (lowercase letters, digits, hyphens; at most 40
  characters) and is the page's identity.
- **Latest version only.** A write replaces the whole body; there is no history
  table and no partial edit, so a writer reads the page first. This is the
  overwrite-in-place stance of ADR 0005.
- **No schema for contents.** A page is markdown text. miles never parses it
  and gives no meaning to any page name, so what the pages hold is up to
  whoever writes them.
- **Ground truth.** Like notes and plan documents, pages are exempt from
  `derive_all` and untouched by sync.
- **Read-only in the app.** `doc.html?page=<name>` renders a page with the same
  renderer and `doc.html` alone lists the pages; writes go through
  the MCP tools. Each tool result carries the page's `url`, absolute when
  `MILES_PUBLIC_URL` is set.

Concurrent writers are not merged: the last write wins.
