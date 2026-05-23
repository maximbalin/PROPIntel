import json
import logging
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage
from backend.agents.prompts import SYNTHESIZER_PROMPT
from backend.config import get_settings
from backend.scoring.engine import compute_scores

logger = logging.getLogger(__name__)


def get_llm():
    settings = get_settings()
    return ChatOpenAI(
        model=settings.llm_model,
        openai_api_key=settings.openai_api_key,
        max_tokens=4096,
    )


async def synthesizer_node(state: dict) -> dict:
    env_report = state.get("env_report", {})
    infra_report = state.get("infra_report", {})
    neighborhood_report = state.get("neighborhood_report", {})

    raw_data = state.get("raw_data", {})
    fallback_scores = compute_scores(env_report, infra_report, neighborhood_report, raw_data=raw_data)

    prompt = SYNTHESIZER_PROMPT.format(
        address=state.get("address", ""),
        mode=state.get("mode", "buyer"),
        env_report=json.dumps(env_report),
        infra_report=json.dumps(infra_report),
        neighborhood_report=json.dumps(neighborhood_report),
    )
    try:
        llm = get_llm()
        response = await llm.ainvoke([HumanMessage(content=prompt)])
        content = response.content.strip()
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        result = json.loads(content)
        scores = result.get("scores", fallback_scores)
        for k, v in scores.items():
            scores[k] = max(0, min(100, int(v)))
        all_risks = []
        for report in [env_report, infra_report, neighborhood_report]:
            all_risks.extend(report.get("risks", []))
        return {
            "scores":           scores,
            "risks":            all_risks,
            "narrative":        result.get("narrative", ""),
            "mode_advice":      result.get("mode_advice", ""),
            "overall_confidence": int(result.get("overall_confidence", 50)),
            "score_evidence":   result.get("score_evidence"),
            "recommendation":   result.get("recommendation"),
            "price_impact":     result.get("price_impact"),
        }
    except Exception as e:
        logger.error(f"Synthesizer failed: {e}")
        all_risks = []
        for report in [env_report, infra_report, neighborhood_report]:
            all_risks.extend(report.get("risks", []))
        return {
            "scores":           fallback_scores,
            "risks":            all_risks,
            "narrative":        "Analysis could not be synthesized due to an error.",
            "mode_advice":      "Please review individual risk factors manually.",
            "overall_confidence": 30,
            "score_evidence":   None,
            "recommendation":   None,
            "price_impact":     None,
        }


async def mode_adapter_node(state: dict) -> dict:
    return state
