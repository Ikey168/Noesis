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
