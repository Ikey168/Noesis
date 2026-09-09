from src.ingestion.pdf_evaluation import score


def test_score_reports_page_locator_and_bbox_overlap():
    expected = {
        "expected": [
            {"text": "Evidence", "page": 1, "bbox": [0, 0, 10, 10]},
            {"text": "Result", "page": 1, "bbox": [10, 0, 20, 10]},
        ],
        "table_cells": [],
        "references": [],
    }
    result = {
        "text": "Evidence Result",
        "locators": [
            {"text": "Evidence", "page": 1, "bbox": [0, 0, 10, 10]},
            {"text": "Result", "page": 1, "bbox": [10, 0, 20, 10]},
        ],
    }
    metrics = score(expected, result)
    assert metrics["page_text_locator_recall"] == 1.0
    assert metrics["mean_bbox_iou"] == 1.0


def test_score_marks_locator_metrics_unavailable_without_boxes():
    expected = {
        "expected": [{"text": "Evidence", "page": 1, "bbox": [0, 0, 10, 10]}],
        "table_cells": [],
        "references": [],
    }
    metrics = score(expected, {"text": "Evidence", "locators": []})
    assert metrics["page_text_locator_recall"] == 0.0
    assert metrics["mean_bbox_iou"] is None


def test_table_position_metric_does_not_reward_scrambled_cells():
    expected = {
        "expected": [{"text": "A B", "page": 1}],
        "tables": [
            {
                "page": 1,
                "cells": [
                    {"row": 0, "col": 0, "text": "A"},
                    {"row": 0, "col": 1, "text": "B"},
                ],
            }
        ],
    }
    observed = {
        "text": "A B",
        "tables": [
            {
                "page": 1,
                "cells": [
                    {"row": 0, "col": 1, "text": "A"},
                    {"row": 0, "col": 0, "text": "B"},
                ],
            }
        ],
    }
    result = score(expected, observed)
    assert result["token_recall"] == 1 and result["table_positional_cell_recall"] == 0


def test_docling_coordinate_origin_and_tei_reference_links_are_preserved():
    from src.ingestion.pdf_evaluation import normalize_docling, normalize_tei

    result = normalize_docling(
        {
            "pages": {"1": {"size": {"height": 800}}},
            "texts": [
                {
                    "text": "Evidence",
                    "prov": [
                        {
                            "page_no": 1,
                            "bbox": {
                                "l": 10,
                                "t": 780,
                                "r": 100,
                                "b": 760,
                                "coord_origin": "BOTTOMLEFT",
                            },
                        }
                    ],
                }
            ],
        }
    )
    assert result["locators"][0]["bbox"] == [10, 20, 100, 40]
    native = b'<TEI xmlns="http://www.tei-c.org/ns/1.0"><text><body><p coords="1,10,20,30,40">Evidence <ref type="bibr" target="#b1">Smith</ref></p></body><back><biblStruct xml:id="b1" coords="1,1,2,3,4"><title>Reference</title></biblStruct></back></text></TEI>'
    result = normalize_tei(native)
    assert result["locators"][0]["bbox"] == [10, 20, 40, 60]
    assert result["references"][0]["id"] == "b1" and result["citation_links"][0][
        "targets"
    ] == ["#b1"]
