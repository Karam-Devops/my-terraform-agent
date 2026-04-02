# graph_builder.py

from langgraph.graph import StateGraph, END
from agent_state import AgentState
from agent_nodes import generate_code_node, validate_code_node, file_writer_node

def should_continue_after_validation(state: AgentState) -> str:
    """
    Conditional edge that decides the next step after the validation node.
    It inspects the last message added by the validator to decide the path.
    """
    print("--- 4. CHECKING VALIDATION RESULT ---")
    
    last_message = state['messages'][-1]
    
    # --- THIS IS THE CRITICAL FIX ---
    # We now adopt a strict "allow-list" approach. Only proceed if the
    # message explicitly says the code is valid.
    if "Terraform code is valid" in last_message.content:
        print("--> Validation successful. Proceeding to write files.")
        return "continue_to_write"
    else:
        # Any other message (including "Failed", "Error", or "Skipped")
        # is now treated as a failure.
        print("--> Validation failed or was skipped. Halting workflow.")
        return "end_with_error"

def build_agent_graph():
    """
    Builds and compiles the LangGraph agent with a clear, linear workflow:
    1. Generate Code -> 2. Validate Code -> 3. Write Files
    """
    workflow = StateGraph(AgentState)

    # --- Add the nodes to the graph ---
    # Each node corresponds to a function in agent_nodes.py
    workflow.add_node("generate", generate_code_node)
    workflow.add_node("validate", validate_code_node)
    workflow.add_node("write_files", file_writer_node)

    # --- Define the workflow edges (the path the agent takes) ---

    # 1. The graph starts at the 'generate' node.
    workflow.set_entry_point("generate")

    # 2. After 'generate', it always goes to 'validate'.
    workflow.add_edge("generate", "validate")

    # 3. After 'validate', we use our conditional edge to decide what to do next.
    workflow.add_conditional_edges(
        "validate",  # The source node
        should_continue_after_validation,  # The function that decides the path
        {
            # If the function returns "continue_to_write", move to the 'write_files' node.
            "continue_to_write": "write_files",
            # If the function returns "end_with_error", terminate the graph.
            "end_with_error": END
        }
    )

    # 4. After 'write_files' successfully completes, the graph ends.
    workflow.add_edge("write_files", END)

    # Compile the workflow into a runnable application.
    app = workflow.compile()
    print("\n--- Agent Graph Compiled Successfully ---\n")
    return app