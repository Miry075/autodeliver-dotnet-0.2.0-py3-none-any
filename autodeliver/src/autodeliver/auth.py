"""How the Claude CLI behind the Agent SDK authenticates.

Three ways, in the CLI's own precedence order:
- ANTHROPIC_API_KEY: a Claude Console API key, billed per token. Required for shared or company pipelines
  (Anthropic's terms: products and services built on the Agent SDK use API keys).
- CLAUDE_CODE_OAUTH_TOKEN: a long-lived token from `claude setup-token`, drawing on a Pro/Max/Team/Enterprise
  subscription. For personal use on your own repositories and tickets.
- Neither set: the Claude Code login saved on this machine (`claude`, then /login). Local runs only.

When both variables are set the CLI uses the API key, so a run you meant to put on your subscription is
billed to the key instead; doctor warns about that.
"""
from __future__ import annotations

import os
from typing import MutableMapping

API_KEY = "ANTHROPIC_API_KEY"
OAUTH_TOKEN = "CLAUDE_CODE_OAUTH_TOKEN"
CLAUDE_CREDENTIALS = (API_KEY, OAUTH_TOKEN)

# Secrets that may arrive as pipeline variables. An undefined Azure Pipelines variable is passed through
# literally ("$(NAME)"); the Claude CLI would then try that text as a credential and fail to log in.
PIPELINE_SECRETS = (*CLAUDE_CREDENTIALS, "SONAR_TOKEN", "SYSTEM_ACCESSTOKEN", "AZURE_DEVOPS_PAT")


def is_set(value: str | None) -> bool:
    """Set and not an unexpanded Azure Pipelines macro such as '$(ANTHROPIC_API_KEY)'."""
    return bool(value) and not (value.startswith("$(") and value.endswith(")"))


def drop_unexpanded(environ: MutableMapping[str, str] | None = None) -> list[str]:
    """Remove empty or unexpanded pipeline secrets from the environment; returns the names removed."""
    env = os.environ if environ is None else environ
    removed = [name for name in PIPELINE_SECRETS if name in env and not is_set(env[name])]
    for name in removed:
        del env[name]
    return removed


def mode(environ: MutableMapping[str, str] | None = None) -> str:
    """'api_key', 'subscription_token' or 'saved_login' (whatever `claude` /login stored on this machine)."""
    env = os.environ if environ is None else environ
    if is_set(env.get(API_KEY)):
        return "api_key"
    if is_set(env.get(OAUTH_TOKEN)):
        return "subscription_token"
    return "saved_login"


DESCRIPTION = {
    "api_key": f"{API_KEY} (Console API key, billed per token)",
    "subscription_token": f"{OAUTH_TOKEN} (your Claude subscription, from `claude setup-token`)",
    "saved_login": "the Claude Code login on this machine (your subscription; run `claude` then /login once)",
}
