import json
from datetime import date
from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Command, interrupt

from camping_agent.llm import get_llm
from camping_agent.prompts import SYSTEM_PROMPT
from camping_agent.tools import ALL_TOOLS


class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]


def build_graph():
    llm = get_llm()
    tools_by_name = {t.name: t for t in ALL_TOOLS}
    llm_with_tools = llm.bind_tools(ALL_TOOLS)

    def agent_node(state: AgentState):
        sys = SystemMessage(
            content=SYSTEM_PROMPT.format(today=date.today().isoformat())
        )
        response = llm_with_tools.invoke([sys] + state["messages"])
        return {"messages": [response]}

    async def tool_node(state: AgentState):
        last = state["messages"][-1]
        outputs = []
        for tc in last.tool_calls:
            tool_fn = tools_by_name[tc["name"]]

            # Confirm before opening reservation page
            if tc["name"] == "open_reservation_page":
                url = tc["args"].get("url", "")
                name = tc["args"].get("campsite_name", "")
                confirmation = interrupt(
                    f"Open reservation page for {name}?\n  {url}\n(y/n)"
                )
                if str(confirmation).lower() not in ("y", "yes"):
                    outputs.append(
                        ToolMessage(
                            content="User declined to open the page.",
                            name=tc["name"],
                            tool_call_id=tc["id"],
                        )
                    )
                    continue

            # Execute the tool
            if hasattr(tool_fn, "ainvoke"):
                result = await tool_fn.ainvoke(tc["args"])
            else:
                result = tool_fn.invoke(tc["args"])

            content = (
                json.dumps(result) if not isinstance(result, str) else result
            )
            outputs.append(
                ToolMessage(
                    content=content,
                    name=tc["name"],
                    tool_call_id=tc["id"],
                )
            )
        return {"messages": outputs}

    def should_continue(state: AgentState):
        last = state["messages"][-1]
        if hasattr(last, "tool_calls") and last.tool_calls:
            return "tools"
        return END

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tool_node)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges(
        "agent", should_continue, {"tools": "tools", END: END}
    )
    graph.add_edge("tools", "agent")

    checkpointer = InMemorySaver()
    return graph.compile(checkpointer=checkpointer)
