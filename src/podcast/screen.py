"""Abstract screening with a *decision model* via OpenRouter's Decisions API.

Decision models do not generate text: we send a ``state`` and named ``questions`` (with criteria) and
get back probabilities / positions on a scale. Thresholds are therefore applied in code.
Docs: https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-request
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from .config import ScreeningConfig, Topic, require_env
from .http import _retryable
from .models import Paper, Screening

log = logging.getLogger(__name__)
URL = "https://openrouter.ai/api/alpha/decisions"

RELEVANCE_LEVELS = [
    "Off-topic: the paper does not address the podcast topic or keywords, or matches them only incidentally.",
    "Tangential: touches the topic but it is not the paper's focus.",
    "Related: addresses a neighbouring problem, or the topic as one secondary component.",
    "Relevant: the topic is a main focus of the paper.",
    "Central: squarely about the podcast topic; a listener of the topic would want to hear about it.",
]
QUALITY_LEVELS = [
    "Poor: vague or uninformative abstract, opinion without data, claims unsupported by the described methods.",
    "Weak: very small or uncontrolled study, major apparent limitations, or no clear results reported.",
    "Adequate: reasonable design and clearly reported results, with some limitations.",
    "Good: sound design (adequate sample, controls or validation), clearly reported quantitative results.",
    "Excellent: rigorous design (e.g. randomized, multi-centre, systematic review, large validated dataset) and substantive, novel findings.",
]
STUDY_TYPES = {
    "randomized_trial": "A randomized controlled trial.",
    "cohort_or_case_control": "Prospective or retrospective cohort, or case-control study.",
    "cross_sectional": "Cross-sectional study, survey, or diagnostic accuracy study.",
    "case_report_or_series": "Case report or small case series.",
    "systematic_review_or_meta_analysis": "Systematic review or meta-analysis.",
    "narrative_review_or_opinion": "Narrative review, perspective, editorial, or opinion.",
    "method_or_tool": "New method, algorithm, software, instrument or dataset.",
    "modelling_or_simulation": "Computational modelling or simulation study.",
    "basic_or_preclinical": "Basic science, animal, cell or other preclinical experiment.",
    "other": "None of the above.",
}


def build_request(model: str, topic: Topic, paper: Paper) -> dict:
    return {
        "model": model,
        "state": {
            "podcast_topic": topic.name,
            "podcast_keywords": topic.keywords,
            "publication_status": "preprint (not peer reviewed)" if paper.is_preprint else "published",
            "title": paper.title,
            "abstract": paper.abstract,
        },
        "questions": {
            "relevance": {
                "type": "score",
                "instructions": "How relevant is this paper to the podcast topic and keywords given in the state? "
                                "Judge from the title and abstract only.",
                "criteria": RELEVANCE_LEVELS,
            },
            "quality": {
                "type": "score",
                "instructions": "How strong is the scientific quality of this paper, judged only from what its abstract reveals?",
                "criteria": QUALITY_LEVELS,
            },
            "study_type": {
                "type": "choice",
                "instructions": "What type of study is this paper?",
                "criteria": STUDY_TYPES,
            },
            "engaging": {
                "type": "noul",
                "instructions": "Would this paper make an interesting, trustworthy segment in a research podcast "
                                "for specialists in the podcast topic?",
                "criteria": {
                    "true": "It reports concrete, novel or practically useful findings that can be explained to specialists.",
                    "false": "It is routine, incremental, purely administrative, too narrow, or too uninformative to discuss.",
                },
            },
        },
    }


def _scale10(score: float, levels: int) -> float:
    return round(max(0.0, min(float(levels - 1), score)) / (levels - 1) * 10, 2)


def parse_response(data: dict, cfg: ScreeningConfig, paper: Paper) -> Screening:
    a = data["answers"]
    study = a["study_type"]
    s = Screening(
        relevance=_scale10(a["relevance"]["score"], len(RELEVANCE_LEVELS)),
        quality=_scale10(a["quality"]["score"], len(QUALITY_LEVELS)),
        study_type=study["choice"],
        study_type_confidence=study.get("confidence"),
        engaging=float(a["engaging"]["noul"]),
        model=data.get("model"),
        keep=False,
    )
    s.keep = (s.relevance >= cfg.min_relevance and s.quality >= cfg.min_quality
              and s.engaging >= cfg.min_engaging and (cfg.allow_preprints or not paper.is_preprint))
    return s


@retry(retry=retry_if_exception(_retryable), stop=stop_after_attempt(4),
       wait=wait_exponential(multiplier=2, max=30), reraise=True)
def _call(client: httpx.Client, body: dict) -> dict:
    r = client.post(URL, json=body)
    r.raise_for_status()
    return r.json()


def screen_one(client: httpx.Client, cfg: ScreeningConfig, topic: Topic, paper: Paper) -> Screening | None:
    try:
        return parse_response(_call(client, build_request(cfg.model, topic, paper)), cfg, paper)
    except Exception as e:  # network, malformed answer: leave unscreened so it is retried next run
        log.warning("screening failed for %s: %s", paper.key, e)
        return None


def screen(cfg: ScreeningConfig, topic: Topic, papers: list[Paper]) -> list[Paper]:
    headers = {"Authorization": f"Bearer {require_env('OPENROUTER_API_KEY')}", "X-Title": "auto-scientific-podcast"}
    with httpx.Client(headers=headers, timeout=120) as client, ThreadPoolExecutor(cfg.concurrency) as pool:
        results = list(pool.map(lambda p: screen_one(client, cfg, topic, p), papers))
    for p, s in zip(papers, results):
        p.screening = s
    return papers
