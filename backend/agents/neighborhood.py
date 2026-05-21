import json
import logging
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage
from backend.agents.prompts import NEIGHBORHOOD_AGENT_PROMPT
from backend.config import get_settings

logger = logging.getLogger(__name__)


def get_llm():
    settings = get_settings()
    return ChatAnthropic(
        model=settings.llm_model,
        anthropic_api_key=settings.anthropic_api_key,
        max_tokens=2048,
    )


async def neighborhood_agent(state: dict) -> dict:
    raw_data = state.get("raw_data", {})
    prompt = NEIGHBORHOOD_AGENT_PROMPT.format(
        address=state.get("address", ""),
        lat=state.get("lat", 0),
        lon=state.get("lon", 0),
        census_data=json.dumps(raw_data.get("census", {})),
    )
    try:
        llm = get_llm()
        response = await llm.ainvoke([HumanMessage(content=prompt)])
        content = response.content.strip()
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        report = json.loads(content)
    except Exception as e:
        logger.error(f"Neighborhood agent failed: {e}")
        report = {
            "risks": [],
            "sub_scores": {"income_score": 50, "vacancy_score": 50, "employment_score": 50},
            "sources_used": [],
            "summary": f"Analysis failed: {e}",
        }
    return {"neighborhood_report": report}
