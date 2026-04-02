# llm_provider.py

# This is the ONLY file in our entire project that will import a specific LLM class.
# When the deprecation happens, this is the only file we will need to edit.
from langchain_google_vertexai import ChatVertexAI

from config import GEMINI_MODEL, GCP_PROJECT_ID

def get_llm_client():
    """
    Initializes and returns the configured LLM client.
    All LLM-specific configuration lives here.
    """
    
    # The DeprecationWarning will originate from this line, but it is safely
    # contained within this function and will not affect any other part of the system.
    llm = ChatVertexAI(
        model_name=GEMINI_MODEL,
        project=GCP_PROJECT_ID,
        temperature=0.2,
        convert_system_message_to_human=True
    )
    
    return llm