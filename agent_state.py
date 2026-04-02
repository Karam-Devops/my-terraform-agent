# agent_state.py

from typing import TypedDict, Annotated, Sequence, Dict
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

class AgentState(TypedDict):
    """
    Represents the state of our agent. This version includes an
    iteration_count for the self-correction loop.
    """
    messages: Annotated[Sequence[BaseMessage], add_messages]
    files_to_write: Dict[str, str]
    final_report: str
    
    # Counter to prevent infinite correction loops.
    iteration_count: int