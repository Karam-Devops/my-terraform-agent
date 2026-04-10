# graph_builder.py

from langgraph.graph import StateGraph, END
from .agent_state import AgentState
from .agent_nodes import generate_code_node, validate_code_node, file_writer_node, fix_code_node
from .config import config

def route_after_validation(state: AgentState) -> str:
    """
    Conditional edge that decides the next step after validation.
    This creates the self-correction loop.
    """
    print("--- 4. CHECKING VALIDATION RESULT ---")
    last_message = state['messages'][-1].content
    
    if "Terraform code is valid" in last_message:
        print("--> Validation successful. Proceeding to write files.")
        return "write_files"
    else:
        print("--> Validation failed. Checking iteration count...")
        # Check if we have exceeded the max number of retries.
        if state.get('iteration_count', 0) >= config.MAX_ITERATIONS:
            print(f"!! Max iterations ({config.MAX_ITERATIONS}) reached. Halting.")
            return END
        else:
            # If we have retries left, route to the 'fix_code' node.
            iteration = state.get('iteration_count', 0)
            print(f"--> Iteration {iteration + 1}. Routing to 'fix_code' node.")
            return "fix_code"

def build_agent_graph():
    """Builds and compiles the LangGraph agent with a self-correction loop."""
    workflow = StateGraph(AgentState)

    # Add all the nodes to the graph
    workflow.add_node("generate", generate_code_node)
    workflow.add_node("validate", validate_code_node)
    workflow.add_node("fix_code", fix_code_node)
    workflow.add_node("write_files", file_writer_node)

    # Define the graph's edges and structure
    workflow.set_entry_point("generate")
    workflow.add_edge("generate", "validate")
    workflow.add_edge("write_files", END)

    # This is the core of the loop.
    # After the 'fix_code' node runs, it goes back to 'validate' to check the new code.
    workflow.add_edge("fix_code", "validate")

    # The conditional router after validation decides where to go next.
    workflow.add_conditional_edges(
        "validate",
        route_after_validation,
        {
            "write_files": "write_files",
            "fix_code": "fix_code",
            END: END
        }
    )

    app = workflow.compile()
    print("\n--- Self-Correcting Agent Graph Compiled Successfully ---\n")
    return app