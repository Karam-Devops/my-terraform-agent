# agent_state.py

from typing import TypedDict, Annotated, Sequence, Dict
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

class AgentState(TypedDict):
    """
    Represents the state of our agent, which is passed between nodes.

    This new structure is more robust and aligns with LangGraph best practices.

    Attributes:
        messages: A sequence of messages that represents the full conversation
            history. The `add_messages` reducer automatically appends new
            messages to this list, rather than overwriting it. This is the
            core of the agent's memory.

        files_to_write: A dictionary where keys are the full relative file paths
            (e.g., "modules/gke-cluster/main.tf") and values are the code
            content for those files. This will be populated by the generation
            node and used by the file-writing node.

        final_report: A string message that summarizes the final result of the
            agent's run (e.g., success or failure report), which can be
            printed to the user at the end.
    """

    # --- Core Conversational Memory ---
    # This `Annotated` type with `add_messages` is the standard, powerful
    # way to manage history in LangGraph.
    messages: Annotated[Sequence[BaseMessage], add_messages]

    # --- Data for File Generation ---
    # This field will hold the structured output from the LLM.
    files_to_write: Dict[str, str]

    # --- Final Output ---
    # This field will be populated by the last node to give a summary to the user.
    final_report: str