from podcast.config import ScreeningConfig, Topic
from podcast.models import Paper
from podcast.screen import RELEVANCE_LEVELS, build_request, parse_response

TOPIC = Topic(name="Muscle MRI", keywords=["muscle MRI", "fat fraction"])
PAPER = Paper(source="x", source_id="1", title="T", abstract="A", is_preprint=True)


def answers(rel=3.4, qual=2.8, noul=0.9, study="cohort_or_case_control"):
    return {"model": "perplexity/pplx-decider-v1.1-27b-20261006", "answers": {
        "relevance": {"type": "score", "score": rel}, "quality": {"type": "score", "score": qual},
        "study_type": {"type": "choice", "choice": study, "confidence": 0.8},
        "engaging": {"type": "noul", "noul": noul}}}


def test_build_request_shape():
    body = build_request("m", TOPIC, PAPER)
    assert body["model"] == "m" and set(body["questions"]) == {"relevance", "quality", "study_type", "engaging"}
    assert body["state"]["podcast_keywords"] == ["muscle MRI", "fat fraction"]
    assert "preprint" in body["state"]["publication_status"]
    q = body["questions"]
    assert q["relevance"]["criteria"] == RELEVANCE_LEVELS and q["engaging"]["criteria"].keys() == {"true", "false"}
    assert "other" in q["study_type"]["criteria"]


def test_parse_scales_to_ten_and_applies_thresholds():
    cfg = ScreeningConfig()
    s = parse_response(answers(), cfg, PAPER)
    assert s.relevance == 8.5 and s.quality == 7.0 and s.keep and s.model.endswith("20261006")
    assert not parse_response(answers(rel=1.0), cfg, PAPER).keep  # irrelevant
    assert not parse_response(answers(qual=1.0), cfg, PAPER).keep  # weak
    assert not parse_response(answers(noul=0.2), cfg, PAPER).keep  # not engaging
    assert not parse_response(answers(), ScreeningConfig(allow_preprints=False), PAPER).keep
    assert parse_response(answers(rel=9, qual=-1), cfg, PAPER).relevance == 10.0  # clamped
    assert parse_response(answers(rel=9, qual=-1), cfg, PAPER).quality == 0.0
