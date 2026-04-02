# agent_state.py

from typing import TypedDict

class AgentState(TypedDict):
    """
    Represents the state of our agent, which is passed between nodes.

    Attributes:
        user_prompt: The initial request from the user.
        terraform_files: A dictionary where keys are filenames (e.g., "main.tf")
                         and values are the code content.
        validation_error: Any error message from 'terraform validate'.
        max_iterations: The maximum number of correction attempts.
        iteration_count: The current number of correction attempts.
    """
    user_prompt: str
    terraform_files: dict[str, str]
    validation_error: str
    max_iterations: int
    iteration_count: int