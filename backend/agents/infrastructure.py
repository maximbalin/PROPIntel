import json
import logging
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage
from backend.agents.prompts import INFRASTRUCTURE_AGENT_PROMPT
from backend.config import get_settings

logger = logging.getLogger(__name__)


def get_llm():
    settings = get_settings()
    return ChatOpenAI(
        model=settings.llm_model,
        openai_api_key=settings.openai_api_key,
        max_tokens=2048,
    )


async def infrastructure_agent(state: dict) -> dict:
    raw_data = state.get("raw_data", {})
    prompt = INFRASTRUCTURE_AGENT_PROMPT.format(
        address=state.get("address", ""),
        lat=state.get("lat", 0),
        lon=state.get("lon", 0),
        osm_data=json.dumps(raw_data.get("osm", {})),
        elevation_data=json.dumps(raw_data.get("usgs", {})),
        traffic_data=json.dumps(raw_data.get("traffic", {})),
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
        logger.error(f"Infrastructure agent failed: {e}")
        report = {
            "risks": [],
            "sub_scores": {"power_line_score": 50, "highway_noise_score": 50, "rail_score": 50},
            "sources_used": [],
            "summary": f"Analysis failed: {e}",
        }
    return {"infra_report": report}
