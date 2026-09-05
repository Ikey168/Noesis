"""Opt-in GLiNER2 domain extraction with exact original-text offsets."""

from .common import IntegrationError, digest, finite, receipt
from .models import model_path, pin

SCHEMAS = {
    "de": {
        "AUTHORITY": "Behörde oder öffentliche Verwaltung, einschließlich Senatsverwaltungen",
        "FUNDING_PROGRAMME": "Name eines Förderprogramms der EU, Deutschlands oder Berlins",
        "ORGANIZATION": "Benannte Organisation, Hochschule, Unternehmen oder Verein",
        "LEGAL_REFERENCE": "Benanntes Gesetz, Verordnung, Artikel oder Paragraph",
    },
    "en": {
        "AUTHORITY": "Named public authority or government department",
        "FUNDING_PROGRAMME": "Named EU, German or Berlin funding programme",
        "ORGANIZATION": "Named organisation, university, company or association",
        "LEGAL_REFERENCE": "Named law, regulation, article or statutory section",
    },
}


class GLiNER2Extractor:
    """Evaluation backend; model scores are uncalibrated, not human judgements.

    Provision the pinned checkpoint explicitly using ``model_path(download=True)``.
    Normal construction only loads local files. Overlaps and repeated mentions are
    retained; no whitespace normalisation or entity merging changes source offsets.
    """

    MODEL = "fastino/gliner2-multi-v1"

    def __init__(self, *, device="cpu", threshold=0.5, max_chars=8000):
        from gliner2 import GLiNER2

        self.threshold = finite(threshold, "threshold", 0, 1)
        if type(max_chars) is not int or not 1 <= max_chars <= 32000:
            raise IntegrationError("input_limit", "max_chars must be 1..32000")
        self.max_chars = max_chars
        self.max_tokens = 512
        self.model = GLiNER2.from_pretrained(
            model_path(self.MODEL), local_files_only=True, map_location=device
        ).eval()

    def extract(self, text, *, language, article_id, revision_id, labels=None):
        if language not in SCHEMAS:
            raise IntegrationError("unsupported_language", "Evaluation supports de/en")
        if not isinstance(text, str) or not text.strip() or len(text) > self.max_chars:
            raise IntegrationError("input_limit", "Nonempty bounded text is required")
        if not all(isinstance(x, str) and x.strip() for x in (article_id, revision_id)):
            raise IntegrationError(
                "missing_source", "Article and revision IDs required"
            )
        selected = list(SCHEMAS[language]) if labels is None else labels
        if (
            not isinstance(selected, (list, tuple))
            or not selected
            or any(
                not isinstance(x, str) or x not in SCHEMAS[language] for x in selected
            )
            or len(set(selected)) != len(selected)
        ):
            raise IntegrationError("unsupported_label", "Select unique domain labels")
        schema = {label: SCHEMAS[language][label] for label in selected}
        tokenizer = self.model.processor.tokenizer
        # Reserve room for schema/control tokens; reject rather than truncate.
        token_count = len(tokenizer.encode(text, add_special_tokens=False))
        schema_tokens = sum(
            len(tokenizer.encode(k + " " + v, add_special_tokens=False))
            for k, v in schema.items()
        )
        if token_count + schema_tokens + 64 > self.max_tokens:
            raise IntegrationError("token_limit", "Text and schema exceed token budget")
        raw = self.model.extract_entities(
            text,
            schema,
            threshold=self.threshold,
            include_confidence=True,
            include_spans=True,
            overlap_policy="allow",
        )
        entities = self._map(text, raw, schema)
        request = {
            "model": self.MODEL,
            "revision": pin(self.MODEL)["revision"],
            "language": language,
            "schema": schema,
            "threshold": self.threshold,
            "max_tokens": self.max_tokens,
            "article_id": article_id,
            "revision_id": revision_id,
            "text_sha256": digest(text),
            "offset_unit": "unicode_codepoint",
            "overlap_policy": "allow",
            "confidence_semantics": "uncalibrated_model_score",
        }
        run = receipt("gliner2", "gliner2", request, entities)
        return {"entities": entities, "receipt": run}

    @staticmethod
    def _map(text, raw, schema):
        groups = raw.get("entities") if isinstance(raw, dict) else None
        if not isinstance(groups, dict) or any(k not in schema for k in groups):
            raise IntegrationError("invalid_output", "Unexpected entity schema")
        result = []
        for label, items in groups.items():
            if not isinstance(items, list) or len(items) > 1000:
                raise IntegrationError("invalid_output", "Invalid entity collection")
            for item in items:
                if not isinstance(item, dict):
                    raise IntegrationError("invalid_output", "Expected spanned entity")
                start, end = item.get("start"), item.get("end")
                if (
                    type(start) is not int
                    or type(end) is not int
                    or not 0 <= start < end <= len(text)
                    or item.get("text") != text[start:end]
                ):
                    raise IntegrationError(
                        "invalid_span", "Entity differs from source span"
                    )
                score = finite(item.get("confidence", -1), "confidence", 0, 1)
                result.append(
                    {
                        "text": text[start:end],
                        "type": label,
                        "confidence": score,
                        "start_position": start,
                        "end_position": end,
                    }
                )
        return sorted(
            result, key=lambda e: (e["start_position"], e["end_position"], e["type"])
        )
