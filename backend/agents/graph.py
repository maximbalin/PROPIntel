from typing import TypedDict, List, Optional, Annotated
import asyncio
from langgraph.graph import StateGraph, END
from backend.agents.environmental import environmental_agent
from backend.agents.infrastructure import infrastructure_agent
from backend.agents.neighborhood import neighborhood_agent
from backend.agents.supervisor import synthesizer_node, mode_adapter_node


class PropertyState(TypedDict):
    address: str
    lat: float
    lon: float
    mode: str
    risk_tolerance: str
    raw_data: dict
    env_report: Optional[dict]
    infra_report: Optional[dict]
    neighborhood_report: Optional[dict]
    scores: Optional[dict]
    risks: Optional[List[dict]]
    narrative: Optional[str]
    mode_advice: Optional[str]
    overall_confidence: Optional[int]


async def parallel_agents_node(state: PropertyState) -> PropertyState:
    results = await asyncio.gather(
        environmental_agent(state),
        infrastructure_agent(state),
        neighborhood_agent(state),
        return_exceptions=True,
    )
    update = {}
    for r in results:
        if isinstance(r, Exception):
            continue
        update.update(r)
    return {**state, **update}


def build_graph():
    workflow = StateGraph(PropertyState)
    workflow.add_node("parallel_agents", parallel_agents_node)
    workflow.add_node("synthesizer", synthesizer_node)
    workflow.add_node("mode_adapter", mode_adapter_node)
    workflow.set_entry_point("parallel_agents")
    workflow.add_edge("parallel_agents", "synthesizer")
    workflow.add_edge("synthesizer", "mode_adapter")
    workflow.add_edge("mode_adapter", END)
    return workflow.compile()


_graph = None


def get_graph():
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph
