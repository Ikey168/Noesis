# Portable Namespaces MCP

Exports are canonical JSON packages with a content-addressed manifest and
disclosure receipt. Imports always support a side-effect-free preview and then
an atomic, idempotent commit under `new-namespace`, `reject`, `keep-both`, or
explicit `remap` policy. Ed25519 detached signatures, AES-256-GCM recipient
encryption, field/sensitivity redaction, and bounded verification are available
from `src.kb.portable_namespaces`.
