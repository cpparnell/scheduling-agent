from dotenv import load_dotenv

# Load .env before any submodule imports; detector.py creates the Anthropic
# client at import time and needs ANTHROPIC_API_KEY already in the environment.
load_dotenv()
