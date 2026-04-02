# main.py

import os
from langchain_core.messages import HumanMessage
from config import config  # Import the new, unified config object
from graph_builder import build_agent_graph

def main():
    """
    The main entrypoint for running the Terraform IaC Agent.
    """
    
    # --- 1. Configuration and Sanity Checks ---
    print("--- Terraform IaC Agent Initializing ---")
    if "your-gcp-project-id" in config.GCP_PROJECT_ID:
        print(f"❌ ERROR: Please set your GCP_PROJECT_ID in the config.py file.")
        return
    
    # This sets an environment variable that can be useful for other Google Cloud libraries.
    os.environ["GCLOUD_PROJECT"] = config.GCP_PROJECT_ID
    print(f"✅ GCP Project configured: {config.GCP_PROJECT_ID}")

    # --- 2. Get User Input ---
    print("\nTell me what Google Cloud infrastructure you want to build.")
    print("Example: 'A regional GCS bucket with versioning enabled in us-central1'")
    user_request = input("▶ Your request: ")
    if not user_request:
        print("No input provided. Exiting.")
        return
        
    # --- 3. Build the Agent Graph ---
    # This compiles all our nodes and edges into a runnable application.
    app = build_agent_graph()

    # --- 4. Define the Initial State ---
    # This is the new, correct way to start the graph.
    # The state is a dictionary where the 'messages' key contains a list
    # with the first HumanMessage from the user.
    initial_state = {
        "messages": [HumanMessage(content=user_request)]
    }

    # --- 5. Invoke the Agent ---
    print("\n🚀 Invoking agent... generating, validating, and writing files.")
    print("-" * 50)

    # The `app.invoke()` method runs the agent from the entry point to an end point.
    final_state = app.invoke(initial_state)

    print("-" * 50)
    print("🏁 Agent run complete!")
    
    # --- 6. Display Final Report ---
    # We no longer print the code. Instead, we print the final report generated
    # by the file_writer_node, which tells us if the files were created.
    if final_state and final_state.get("final_report"):
        print(f"\n✅ Result: {final_state['final_report']}")
        print(f"   Please check the '{config.OUTPUT_DIR}' directory to see your generated files.")
    else:
        print("\n❌ Agent failed to complete the task.")
        print("   Here is the final message history for debugging:")
        if final_state and final_state.get('messages'):
             # If something went wrong, the error will be in the last message.
             last_message = final_state['messages'][-1]
             print(f"  - [{last_message.type}]: {last_message.content}")

if __name__ == "__main__":
    main()