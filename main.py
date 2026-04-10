# main.py

import os
from langchain_core.messages import HumanMessage
from .config import config  # Import the unified config object
from .graph_builder import build_agent_graph

def main():
    """
    The main entrypoint for running the Terraform IaC Agent.
    """
    
    # --- 1. Configuration and Sanity Checks ---
    print("--- Terraform IaC Agent Initializing ---")
    if "your-gcp-project-id" in config.GCP_PROJECT_ID:
        print(f"❌ ERROR: Please set your GCP_PROJECT_ID in the config.py file.")
        return
    
    os.environ["GCLOUD_PROJECT"] = config.GCP_PROJECT_ID
    print(f"✅ GCP Project configured: {config.GCP_PROJECT_ID}")

    # --- 2. Get User Input (NEW MULTI-LINE VERSION) ---
    print("\nTell me what Google Cloud infrastructure you want to build.")
    print("You can paste a multi-line prompt. Type 'END' on a new line when you are finished.")
    
    prompt_lines = []
    while True:
        line = input()
        # The user types "END" (case-insensitive) on its own line to finish.
        if line.strip().upper() == 'END':
            break
        prompt_lines.append(line)
    
    # Join the collected lines back into a single string.
    user_request = "\n".join(prompt_lines)

    if not user_request:
        print("No input provided. Exiting.")
        return
        
    # --- 3. Build the Agent Graph ---
    app = build_agent_graph()

    # --- 4. Define the Initial State ---
    initial_state = {
        "messages": [HumanMessage(content=user_request)]
    }

    # --- 5. Invoke the Agent (MODIFIED FOR DEBUGGING) ---
    # The original 'app.invoke' call is now wrapped in a try...except block
    # to add debugging prints and catch any errors during the agent's run.
    print("\n🚀 Invoking agent... generating, validating, and writing files.")
    print("-" * 50)
    
    try:
        print(">>> [DEBUG] About to call app.invoke(). The agent is now running...")
        
        final_state = app.invoke(initial_state)

        print(">>> [DEBUG] app.invoke() has completed successfully.")

    except Exception as e:
        print(f"❌ [DEBUG] A CRITICAL ERROR occurred during the agent's run (app.invoke): {e}")
        # Create a minimal final_state so the script can finish gracefully.
        final_state = {
            "messages": [
                HumanMessage(content=user_request),
                HumanMessage(content=f"AGENT CRASHED with error: {e}")
            ],
            "final_report": None # Ensure final_report is None on error
        }
        
    print("-" * 50)
    print("🏁 Agent run complete!")
    
    # --- 6. Display Final Report ---
    if final_state and final_state.get("final_report"):
        print(f"\n✅ Result: {final_state['final_report']}")
        print(f"   Please check the '{config.OUTPUT_DIR}' directory to see your generated files.")
    else:
        print("\n❌ Agent failed to complete the task or an error occurred.")
        if final_state and final_state.get('messages'):
             # Safely access the last message
            if final_state['messages']:
                last_message = final_state['messages'][-1]
                print("   Here is the final message history for debugging:")
                print(f"   - [{last_message.type}]: {last_message.content}")

if __name__ == "__main__":
    main()