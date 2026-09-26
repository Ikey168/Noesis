# Market company research workspace

The authenticated company dashboard is available at `/api/v1/market/workspace` when the market routes are enabled. It is a same-origin, dependency-free browser client for `POST /api/v1/market/company-dashboard`.

Enter the namespace and listing ID from the market instrument master, or resolve an exact ticker through the authenticated instrument lookup. Ambiguous symbols require an explicit venue/listing choice. Peer IDs are listing IDs; universe and industry selection are optional. Currency selection is an exact-currency filter only—the dashboard does not silently convert currencies. Public-information and acquisition cutoffs are explicit, and source-reported periods remain distinct.

The workspace shows price history, filed facts, available server-calculated metrics, corporate events, peer exclusions, panel errors, input revisions and source freshness. Price points and statement rows can be opened to inspect record and source revisions. The optional metric form submits selected fact revision IDs to the server's formula registry; it does not calculate formulas in the browser. For a valuation multiple, the numerator must already be a properly sourced point-in-time value (for example, market capitalization) and the denominator a period-compatible fact. This workspace does not infer market capitalization from a quote and a share count.

The bearer token is sent only to the same-origin dashboard endpoint and held in `sessionStorage` for the current tab. “Forget token” removes it. No third-party scripts, fonts, analytics, or chart libraries are loaded. The static shell and assets are public; dashboard data remains behind the API's existing authentication, namespace, entitlement and record-cutoff checks.

The page makes empty panels, request errors, peer exclusions, missing metrics, absent source references and known limitations visible. A working local UI does not establish that a provider is licensed or that production market coverage is complete. UI fixture coverage and live-provider/human acceptance remain separate evidence.
