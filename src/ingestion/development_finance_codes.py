"""Bundled code lists for the Funding & Grants ``development-finance`` feature (#1932, D06).

These are *subsets* of the IATI Standard code lists (version 2.03) and of the
OECD DAC/CRS code lists, bundled so an offline deployment can normalise the
codes its fixtures use. They are published as ontology modules through
:class:`src.kb.ontology.OntologyAlignmentStore` (one module per list, versioned
by the list's release), and normalisation reads them back from the registry.
A code that is not in the published module stays exactly as reported and is
flagged ``unknown-code``; nothing is guessed from a neighbouring code.

Labels are English short names. Every list states its release and that it is a
subset: before relying on a list, verify it against the published code list
(https://iatistandard.org/en/iati-standard/203/codelists/ and the DAC-CRS code
lists) and publish the complete list as a new module version.
"""

from __future__ import annotations

from typing import Any

IATI_RELEASE = "2.03"
DAC_RELEASE = "2024"
SUBSET_NOTE = "bundled subset; verify against the published code list before relying on completeness"

# name -> {"release", "source", "codes": {code: label}, "broader": {code: parent code}}
CODELISTS: dict[str, dict[str, Any]] = {
    "iati-transaction-type": {
        "release": IATI_RELEASE,
        "source": "IATI Standard code list TransactionType",
        "codes": {
            "1": "Incoming Funds",
            "2": "Outgoing Commitment",
            "3": "Disbursement",
            "4": "Expenditure",
            "5": "Interest Payment",
            "6": "Loan Repayment",
            "7": "Reimbursement",
            "8": "Purchase of Equity",
            "9": "Sale of Equity",
            "10": "Credit Guarantee",
            "11": "Incoming Commitment",
            "12": "Outgoing Pledge",
            "13": "Incoming Pledge",
        },
    },
    "iati-organisation-role": {
        "release": IATI_RELEASE,
        "source": "IATI Standard code list OrganisationRole",
        "codes": {
            "1": "Funding",
            "2": "Accountable",
            "3": "Extending",
            "4": "Implementing",
        },
    },
    "iati-activity-status": {
        "release": IATI_RELEASE,
        "source": "IATI Standard code list ActivityStatus",
        "codes": {
            "1": "Pipeline/identification",
            "2": "Implementation",
            "3": "Finalisation",
            "4": "Closed",
            "5": "Cancelled",
            "6": "Suspended",
        },
    },
    "dac-flow-type": {
        "release": DAC_RELEASE,
        "source": "OECD DAC-CRS code list FlowType (IATI FlowType)",
        "codes": {
            "10": "ODA",
            "20": "OOF",
            "21": "Non-export credit OOF",
            "22": "Officially supported export credits",
            "30": "Private Development Finance",
            "35": "Private market",
            "36": "Private Foreign Direct Investment",
            "37": "Other Private flows at market terms",
            "40": "Non flow",
            "50": "Other flows",
        },
    },
    "dac-aid-type": {
        "release": DAC_RELEASE,
        "source": "OECD DAC-CRS code list AidType (IATI AidType, vocabulary 1)",
        "codes": {
            "A01": "General budget support",
            "A02": "Sector budget support",
            "B01": "Core support to NGOs, other private bodies, PPPs and research institutes",
            "B02": "Core contributions to multilateral institutions",
            "B03": "Contributions to specific-purpose programmes and funds managed by implementing partners",
            "B04": "Basket funds/pooled funding",
            "C01": "Project-type interventions",
            "D01": "Donor country personnel",
            "D02": "Other technical assistance",
            "E01": "Scholarships/training in donor country",
            "E02": "Imputed student costs",
            "F01": "Debt relief",
            "G01": "Administrative costs not included elsewhere",
            "H01": "Development awareness",
            "H02": "Refugees/asylum seekers in donor countries",
        },
    },
    # DAC 3-digit sector categories (IATI SectorCategory, vocabulary 2) and 5-digit purpose codes (vocabulary 1).
    "dac-sector-category": {
        "release": DAC_RELEASE,
        "source": "OECD DAC-CRS sector categories (IATI SectorCategory)",
        "codes": {
            "111": "Education, Level Unspecified",
            "112": "Basic Education",
            "122": "Basic Health",
            "140": "Water Supply & Sanitation",
            "151": "Government & Civil Society-general",
            "231": "Energy Policy",
            "311": "Agriculture",
            "430": "Other Multisector",
            "720": "Emergency Response",
        },
    },
    "dac-purpose-code": {
        "release": DAC_RELEASE,
        "source": "OECD DAC-CRS purpose codes (IATI Sector, vocabulary 1)",
        "codes": {
            "11110": "Education policy and administrative management",
            "11220": "Primary education",
            "12220": "Basic health care",
            "12240": "Basic nutrition",
            "14030": "Basic drinking water supply and basic sanitation",
            "15110": "Public sector policy and administrative management",
            "23110": "Energy policy and administrative management",
            "31120": "Agricultural development",
            "43010": "Multisector aid",
            "72010": "Material relief assistance and services",
        },
        # A purpose code's category is its first three digits (the DAC structure); normalisation looks the category
        # up in "dac-sector-category" only for a code this list knows.
        "category_list": "dac-sector-category",
    },
    "dac-channel": {
        "release": DAC_RELEASE,
        "source": "OECD DAC-CRS channel codes (top-level groups)",
        "codes": {
            "10000": "Public Sector Institutions",
            "11000": "Donor Government",
            "12000": "Recipient Government",
            "20000": "Non-Governmental Organisations (NGOs) and Civil Society",
            "40000": "Multilateral Organisations",
            "41000": "United Nations agency, fund or commission (UN)",
            "44000": "World Bank Group (WB)",
            "51000": "University, college or other teaching institution, research institute or think-tank",
            "60000": "Private Sector Institutions",
        },
    },
    # DAC regional recipient codes are aggregates of countries: never a country and never apportioned.
    "dac-recipient-region": {
        "release": DAC_RELEASE,
        "source": "OECD DAC-CRS recipient codes, regional and unspecified aggregates (IATI Region, vocabulary 1)",
        "codes": {
            "89": "Europe, regional",
            "189": "North of Sahara, regional",
            "289": "South of Sahara, regional",
            "298": "Africa, regional",
            "389": "North & Central America, regional",
            "489": "South America, regional",
            "498": "America, regional",
            "589": "Middle East, regional",
            "619": "Central Asia, regional",
            "679": "South Asia, regional",
            "689": "South & Central Asia, regional",
            "789": "Far East Asia, regional",
            "798": "Asia, regional",
            "889": "Oceania, regional",
            "998": "Developing countries, unspecified (bilateral, unallocated)",
        },
    },
}

# Which list a reported code belongs to, by the element (and vocabulary) it was reported in.
FIELD_LISTS = {
    "transaction_type": "iati-transaction-type",
    "organisation_role": "iati-organisation-role",
    "activity_status": "iati-activity-status",
    "flow_type": "dac-flow-type",
    "aid_type": "dac-aid-type",
    "channel": "dac-channel",
    "recipient_region": "dac-recipient-region",
}
# IATI sector vocabularies: 1 = DAC 5-digit purpose code (the default), 2 = DAC 3-digit category.
SECTOR_VOCABULARIES = {
    None: "dac-purpose-code",
    "1": "dac-purpose-code",
    "2": "dac-sector-category",
}
# IATI region vocabulary 1 is the DAC region list; another vocabulary is kept as reported.
REGION_VOCABULARIES = {None: "dac-recipient-region", "1": "dac-recipient-region"}


def semver(release: str) -> str:
    """A code-list release as the ontology module's semantic version ('2.03' -> '2.3.0', '2024' -> '2024.0.0')."""
    parts = [int(p) for p in str(release).split(".") if p.isdigit()]
    while len(parts) < 3:
        parts.append(0)
    return ".".join(str(p) for p in parts[:3])


def concepts(name: str) -> list[dict[str, Any]]:
    spec = CODELISTS[name]
    out = []
    for code, label in sorted(spec["codes"].items()):
        concept = {
            "concept_id": code,
            "definition": f"{spec['source']}: {label}",
            "labels": [{"value": label, "language": "en", "kind": "preferred"}],
            "code": code,
            "release": spec["release"],
        }
        out.append(concept)
    return out
