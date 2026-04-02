# graph_builder.py

from langgraph.graph import StateGraph, END
from agent_state import AgentState
from agent_nodes import draft_code_node, validate_code_node, fix_code_node

def route_after_validation(state: AgentState) -> str:
    """Conditional edge that decides the next step after validation."""
    print("--- CHECKING FOR ERRORS ---")
    if state.get("validation_error"):
        if state["iteration_count"] >= state["max_iterations"]:
            print("! Max iterations reached. Halting.")
            return "end"
        print("--> Errors found. Routing to 'fix_code' node.")
        return "fix_code"
    else:
        print("--> No errors found. Code is valid.")
        return "end"

def build_agent_graph():
    """Builds and compiles the LangGraph agent."""
    workflow = StateGraph(AgentState)

    # Add the nodes
    workflow.add_node("draft_code", draft_code_node)
    workflow.add_node("validate_code", validate_code_node)
    workflow.add_node("fix_code", fix_code_node)

    # Define the workflow edges
    workflow.set_entry_point("draft_code")
    workflow.add_edge("draft_code", "validate_code")
    workflow.add_edge("fix_code", "validate_code") # This creates the correction loop
    workflow.add_conditional_edges(
        "validate_code",
        route_after_validation,
        {"fix_code": "fix_code", "end": END}
    )

    # Compile and return the graph
    return workflow.compile()