import json
import logging
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage
from backend.agents.prompts import ENVIRONMENTAL_AGENT_PROMPT
from backend.config import get_settings

logger = logging.getLogger(__name__)


def get_llm():
    settings = get_settings()
    return ChatOpenAI(
        model=settings.llm_model,
        openai_api_key=settings.openai_api_key,
        max_tokens=2048,
    )


async def environmental_agent(state: dict) -> dict:
    raw_data = state.get("raw_data", {})
    prompt = ENVIRONMENTAL_AGENT_PROMPT.format(
        address=state.get("address", ""),
        lat=state.get("lat", 0),
        lon=state.get("lon", 0),
        fema_data=json.dumps(raw_data.get("fema", {})),
        epa_data=json.dumps(raw_data.get("epa", {})),
        elevation_data=json.dumps(raw_data.get("usgs", {})),
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
        logger.error(f"Environmental agent failed: {e}")
        report = {
            "risks": [],
            "sub_scores": {"flood_score": 50, "pollution_score": 50, "elevation_score": 50},
            "sources_used": [],
            "summary": f"Analysis failed: {e}",
        }
    return {"env_report": report}
