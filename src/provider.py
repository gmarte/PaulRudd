"""
Prints the configured LLM provider and writes it to $GITHUB_OUTPUT, so the action
installs LiteLLM only for OpenAI and Gemini. Claude goes through the Anthropic SDK.
"""

import os

from config import configured_provider

if __name__ == "__main__":
    provider = configured_provider()
    print(f"Provider: {provider}")
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as f:
            f.write(f"provider={provider}\n")
