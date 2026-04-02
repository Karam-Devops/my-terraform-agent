# main.py

import os
from config import GCP_PROJECT_ID, MAX_ITERATIONS
from graph_builder import build_agent_graph

def main():
    """The main function to run the Terraform agent."""
    
    # Check for GCP Project ID configuration
    if not GCP_PROJECT_ID or GCP_PROJECT_ID == "your-gcp-project-id":
        print("❌ ERROR: Please set your GCP_PROJECT_ID in config.py before running.")
        return
    os.environ["GCLOUD_PROJECT"] = GCP_PROJECT_ID

    # Get user input
    user_request = input("▶ Please enter the GCP infrastructure you want to build: ")
    if not user_request:
        print("No input provided. Exiting.")
        return
        
    # Build the agent graph
    app = build_agent_graph()

    # Define the initial state for the agent run
    initial_state = {
        "user_prompt": user_request,
        "terraform_files": {},
        "max_iterations": MAX_ITERATIONS,
    }

    print("\n🚀 Starting Terraform Agent...")
    print("-" * 30)

    # Invoke the agent graph
    final_state = app.invoke(initial_state)

    print("-" * 30)
    print("🏁 Agent Finished!")
    print("-" * 30)
    
    # Display the final results
    if not final_state['validation_error']:
        print("✅ Final Validated Terraform Module:")
        for filename, content in final_state['terraform_files'].items():
            print("=" * 30)
            print(f"📄 {filename}")
            print("=" * 30)
            print(content)
    else:
        print("❌ Agent failed to produce a valid module.")
        print("=" * 30)
        print(f"Final Error:\n{final_state['validation_error']}")

if __name__ == "__main__":
    main()