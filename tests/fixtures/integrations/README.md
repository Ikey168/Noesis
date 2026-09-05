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
Citation metadata identifies relationships; it does not independently corroborate
the cited works' claims. No access token is stored in the fixture.

`orcid-public-projection.json` contains unchanged professional-record fields
selected from the public ORCID v3 example record `0000-0002-1825-0097`, captured
on 2026-09-05. The example describes Josiah Carberry; it is not an independently
verified researcher biography or an annotation dataset. Contact fields and other
unneeded profile sections were omitted. Source:
`https://pub.orcid.org/v3.0/0000-0002-1825-0097/record`.
