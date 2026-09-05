# Zotero libraries and bibliography

Install `pip install '.[bibliography]'` for Pyzotero and bibtexparser. Noesis uses
Pyzotero's v3 read methods with a bounded, redirect-disabled transport and explicit
backoff failures. This keeps synchronization within a time, byte, and item budget.

`sync_zotero_library(namespace, library_id, library_type, mode)` imports into the
current owner's namespace. `library_type` is user/group; mode is explicitly web
or local. Public Web libraries need no credential. Private Web access uses a
configured `NOESIS_ZOTERO_...` environment reference and current
`credential:<reference>:use` permission, or operator. The key is sent only in a
header and is never saved in item data or receipts. No Web key is sent locally.
Calls also require `knowledge:zotero:sync` and namespace write access.

The client verifies API v3. Desktop synchronization requires an instance's
`Zotero-Server-ID`; older local versions without that identity fail explicitly.
Web, local, and different desktop instances have separate checkpoints, following
[Zotero's local versioning rules](https://www.zotero.org/support/dev/web_api/v3/local_api).
An unavailable desktop/API, credential failure, server backoff, changed library,
or exceeded budget does not publish a partial checkpoint. Rerun from the saved
version after addressing the reported condition.

Items retain keys, versions, bibliographic data, creators, collections, tags, and
raw notes/annotations. Attachments remain stable item references marked
`not-fetched`; Noesis does not follow file redirects or assume attachment bytes
exist. Zotero annotation coordinates are retained with an explicit unsupported
coordinate-space status rather than invented text offsets.

Synchronization follows [Zotero's versioned change/deletion protocol](https://www.zotero.org/support/dev/web_api/v3/syncing).
Trash and external deletion are lifecycle states; local revisions and independent
review records remain. Items, lifecycle changes, and the checkpoint commit in one
transaction. These private imports live in owner-scoped tables and are not
automatically copied into the shared corpus. `list_zotero_items` and
`inspect_zotero_item` require current namespace/Zotero read access; another owner
cannot read the imported library through these APIs. Historical versions can be
reopened even after external deletion. Write-back is not implemented.

## Bibliography export

`export_zotero_bibliography` selects item keys and optionally exact item versions.
It preserves Zotero's CSL JSON and BibTeX representations, rewrites only citation
identities to stable Noesis keys, and verifies a BibTeX parse/write/parse round trip.
The export retains item-version provenance separately from cited evidence
revisions. Missing representations fail explicitly. No DOI, personal author name,
edition, or publication type is invented; corporate names and Unicode remain in
the source representations. Explicit historical versions can export retained,
externally deleted entries with that lifecycle disclosed.

Use the returned citation keys in authored reports. Passing a report ID checks
that every report citation resolves to an exported bibliography entry and records
the report revision/hash and its separate evidence dependencies. Report read
access is checked as well. Save the export's item-version manifest to reproduce
the bibliography later. Exports return CSL JSON and BibTeX without publishing or
changing the Zotero library.

Tests exercise the real Pyzotero parser/transport against controlled v3 responses,
including desktop partitioning, remote-version changes, private access, deletion,
editions and Unicode. No user's private library was imported during development.
