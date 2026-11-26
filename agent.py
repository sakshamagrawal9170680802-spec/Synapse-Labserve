import json
import os
import sys
#Any ka matlab hai koi bhi python type acceptable hai
#Literal given list mein se koi ek hona kuch aur nahi ho sakta
#Annotated extra information dalne ke liye jaise ki ek REDUCER
#TypedDict python ki dictionary jaisa object banane ke liye jisme konsi keys rahegi unka type kya rahega sab pehle se hi define karne ke liye basically ek speccific type ki dictionary ka schema
from typing import Annotated, Any, Literal, TypedDict

#BaseMessage-AIMessage,SystemMessage,HumanMessage,ToolMessage ki parent class hai
from langchain_core.messages import BaseMessage
from langchain_google_genai import ChatGoogleGenerativeAI
#REDUCER
from langgraph.graph.message import add_messages
#StdioServerParameters-batata hai mcp_server ko kaise launch karna hai
#ClientSession-MCP protocol ke operations perform karta hai
from mcp import ClientSession, StdioServerParameters
#STDIO ke through client server communication esthablish karta hai
from mcp.client.stdio import stdio_client
#Field ka use fields ke defaults,constraints aur metadata define karne ke liye hota hai
from pydantic import BaseModel, Field

from config import get_settings

#Planner ko kis format mein output dena hai
class PlanStep(BaseModel):
    """Single tool call planned by the Planner node."""

    tool_name: Literal[
        "inventory_search",
        "operator_clearance_check",
        "borrow_return",
        "bay_booking",
        "ordering",
        "budget_check",
    ]
    #arguments that has t be passed to tool
    arguments: dict[str, Any] = Field(default_factory=dict)
    #will contain reason for choosing this tool to execute
    rationale: str = ""

#full plan returned by planner as planner can return plan in steps in which it has to be followed with each step having its own tool to be executed
class ExecutionPlan(BaseModel):
    """Structured output returned by the Planner LLM."""

    goal_summary: str
    steps: list[PlanStep] = Field(default_factory=list)
    requires_chief_signoff: bool = False
    #if not tool is required to be called we use this direct_response to show lanner message
    direct_response: str | None = None


class LabServeState(TypedDict):
    """Shared state flowing across LangGraph nodes and persisted via checkpointer."""
    
    #stores conversation history
    messages: Annotated[list[BaseMessage], add_messages]
    #ismein user ki original text store hogi
    request_text: str
    #ismein mcp server wala rules notebook store hoga
    rulebook_text: str
    #planner node se aaya hua plan ismein store hoga
    plan: ExecutionPlan | None
    #step of plan that is currently getting executed
    current_step_index: int
    #ismein tool se aane wale result store hoge
    tool_results: list[dict[str, Any]]
    #replan_count aur max_replans check karte hai kitni baar execution plan fail hua aur hume replanning karni padhi aur hum max kitni baar replannng karenge
    replan_count: int
    max_replans: int
    chief_approved: bool
    #pending_action for approval
    pending_approval: dict[str, Any] | None
    #ismein llm ka final response hoga 
    final_response: str


def _mcp_server_params() -> StdioServerParameters:
    """Spawns mcp_server.py using the active Python interpreter and current env."""
    env = {
        k: v
        for k, v in os.environ.items()
        if v is not None
    }
    return StdioServerParameters(
        command=sys.executable,
        args=["mcp_server.py"],
        env=env,
    )


def _parse_mcp_result(raw_result: Any) -> dict[str, Any]:
    """Converts MCP CallToolResult into a predictable Python dictionary."""
    if getattr(raw_result, "isError", False):
        return {
            "status": "ERROR",
            "rule_violated": "MCP_TOOL_ERROR",
            "reason": "MCP server returned a tool execution error.",
        }

    chunks: list[str] = []
    for block in getattr(raw_result, "content", []) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            chunks.append(text)

    combined = "\n".join(chunks).strip()
    if not combined:
        return {"status": "SUCCESS", "raw": ""}

    try:
        parsed = json.loads(combined)
        if isinstance(parsed, dict):
            return parsed
        return {"status": "SUCCESS", "data": parsed}
    except json.JSONDecodeError:
        return {"status": "SUCCESS", "text": combined}


async def load_rulebook_from_mcp() -> str:
    """Reads the live rulebook from the labserve://rulebook MCP system resource."""
    async with stdio_client(_mcp_server_params()) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            resource_resp = await session.read_resource("labserve://rulebook")
            parts: list[str] = []
            for item in getattr(resource_resp, "contents", []) or []:
                text = getattr(item, "text", None)
                if isinstance(text, str):
                    parts.append(text)
            return "\n".join(parts).strip()


async def call_mcp_tool(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Executes one registered tool on mcp_server.py over stdio."""
    async with stdio_client(_mcp_server_params()) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            raw_result = await session.call_tool(tool_name, arguments=arguments)
            return _parse_mcp_result(raw_result)


def _get_llm() -> ChatGoogleGenerativeAI:
    """Initializes Gemini configured in config.py with deterministic temperature=0."""
    settings = get_settings()
    return ChatGoogleGenerativeAI(
        model=settings.gemini_model,
        temperature=0,
        google_api_key=settings.gemini_api_key,
    )