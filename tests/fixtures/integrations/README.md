# Native integration fixtures

`datacite-native.json` is a native public DataCite response captured on 2026-09-05 from
`https://api.datacite.org/dois?query=Berlin&page%5Bsize%5D=2&page%5Bcursor%5D=1`.
DataCite metadata is CC0. The response is provider metadata, not independently verified
research content or human evaluation labels. Tests replay the response without network access.

`ecb-native.xml` is a native public ECB SDMX response captured on 2026-09-05 from
`https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A?startPeriod=2025-01-02&endPeriod=2025-01-03`.
Source: European Central Bank. Retained for parser regression checks with attribution;
these observations do not constitute an independently labelled retrieval benchmark.

`ror-native.json` is the first native record from the public ROR v2 query
`https://api.ror.org/v2/organizations?query=Humboldt+Universit%C3%A4t+Berlin&all_status=`
captured on 2026-09-05. Its ID is `https://ror.org/01hcx6992`. A subsequent direct
record fetch succeeded. Names and relationships remain provider assertions.

`zenodo-native.json` was captured on 2026-09-05 from
`https://zenodo.org/api/records/2092110`. Only public metadata is included; the
PDF is not redistributed in this fixture. Checksum and multiple-file tests use
explicitly synthetic local documents.

`opencitations-native.json` wraps a native 72-edge public response from
`https://api.opencitations.net/index/v2/references/doi:10.1186/1756-8722-6-59`
in a Noesis snapshot with capture time and content hash. Acquired on 2026-09-05.

`opencitations-incoming-native.json.gz` is a lossless, timestamped snapshot of
217 incoming records captured on 2026-09-05 from
`https://api.opencitations.net/index/v2/citations/doi:10.1186/1756-8722-6-59`.
The snapshot retains native records, request identity, provider observation time
and content digest. OpenCitations Index data is released under CC0. Live
restart/resume and traversal measurements are in `opencitations-live-evaluation.json`.
Citation metadata identifies relationships; it does not independently corroborate
the cited works' claims. No access token is stored in the fixture.

`orcid-public-projection.json` contains unchanged professional-record fields
selected from the public ORCID v3 example record `0000-0002-1825-0097`, captured
on 2026-09-05. The example describes Josiah Carberry; it is not an independently
verified researcher biography or an annotation dataset. Contact fields and other
unneeded profile sections were omitted. Source:
`https://pub.orcid.org/v3.0/0000-0002-1825-0097/record`.


Additional SDMX captures were fetched on 2026-09-05:
- `estat-native.xml`: Eurostat SDMX 2.1 `nama_10_gdp/A.CP_MEUR.B1GQ.DE`, start/endPeriod 2023.
- `estat-jsonstat-native.json`: the existing Eurostat JSON-stat endpoint, `nama_10_gdp`, geo=DE, unit=CP_MEUR, na_item=B1GQ, freq=A, time=2023.
- `estat-dsd-native.xml.gz`: lossless gzip of native Eurostat NAMA_10_GDP data structure and code lists; original source https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/datastructure/ESTAT/NAMA_10_GDP/latest .
- `bbk-native.xml`: Bundesbank SDMX `BBEX3/D.USD.EUR.BB.AC.000`, lastNObservations=2. Source https://api.statistiken.bundesbank.de/rest/data/BBEX3/D.USD.EUR.BB.AC.000 .

These are public statistical facts and structure/code labels, attributed to
Eurostat and Deutsche Bundesbank. They are native responses, not generated
labels. Original numeric values and preliminary/status flags are retained.
`sdmx-live-evaluation.json` records an additional actual live comparison and
capture timestamps/URLs/hashes; its responses may have different message headers
from these frozen captures. No historical provider vintage is inferred.
