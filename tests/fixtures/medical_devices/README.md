# Medical devices fixtures (#2654)

Authored, fictional responses in the documented or observed shape of openFDA device endpoints (510(k), PMA,
classification, recall, enforcement, MAUDE event and count queries), AccessGUDID device lookup and the EUDAMED
public site's actor, device and certificate searches. Every organisation, device, identifier and report here is
invented (the Exampla Medical and Northwind Medtech devices, K999001, P999001, product codes ZXA/ZXB, DI
00899999000011, SRN US-MF-000099902, notified body 9999); nothing is live coverage. The responses deliberately
include personal fields (MAUDE patient sections and contact names, 510(k) contacts, GUDID customer contacts,
EUDAMED contact persons and PRRCs, street addresses) so that tests can show the MD01 minimisation decision drops
them.

`v2/` holds later publisher revisions: PMA supplement S003, the recall terminated, a new GUDID public version with
a package DI and the EUDAMED certificate suspended. `tests/unit/medical_devices_harness.py` composes these files
into the pinned source-pack fixtures under `tests/fixtures/source_packs/clinical-devices-*.json`.
