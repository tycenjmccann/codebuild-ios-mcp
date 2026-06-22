"""Connect an agent to the codebuild-ios-mcp Gateway (AWS_IAM auth, SigV4).

The Gateway speaks MCP over HTTPS and authorizes callers with AWS_IAM, so every
request must be SigV4-signed. This module provides a drop-in httpx auth class
that signs each request, wires it into the MCP streamable-HTTP transport, and
hands the four discovered tools (ios_test, ios_build_status, list_schemes,
get_test_logs) to a Strands Agent.

For an AGENTCORE RUNTIME agent there is nothing extra to configure: the runtime
already executes under an IAM execution role, and botocore picks those ambient
credentials up automatically. Just grant that role `bedrock-agentcore:InvokeGateway`
on the gateway ARN. No Cognito, no token vault, no client secret.

Run locally (uses your AWS profile / env credentials):
    pip install "strands-agents" "mcp" "botocore" "httpx"
    export GATEWAY_URL="https://<gateway-id>.gateway.bedrock-agentcore.<region>.amazonaws.com/mcp"
    python examples/connect_agent.py

Auth-only smoke test (no Strands, no model needed):
    python examples/connect_agent.py --list
"""

import os
import sys

import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.session import Session

# AgentCore Gateway authorizes against this service namespace.
SERVICE = "bedrock-agentcore"


class SigV4Auth_httpx(httpx.Auth):
    """httpx auth that SigV4-signs every outgoing request.

    Works with any boto/botocore credential source — env vars, shared config,
    instance/container role, or (inside AgentCore Runtime) the agent's execution
    role. Region is taken from the arg, else the standard botocore resolution.
    """

    requires_request_body = True  # body must be present to sign correctly

    def __init__(self, region: str | None = None, service: str = SERVICE):
        self._session = Session()
        self._creds = self._session.get_credentials()
        if self._creds is None:
            raise RuntimeError(
                "No AWS credentials found. Configure a profile, env vars, or run "
                "inside a role-backed environment (e.g. AgentCore Runtime)."
            )
        self._region = region or self._session.get_config_variable("region") or "us-east-1"
        self._service = service

    def auth_flow(self, request: httpx.Request):
        aws_req = AWSRequest(
            method=request.method,
            url=str(request.url),
            data=request.content,
            headers=dict(request.headers),
        )
        SigV4Auth(self._creds.get_frozen_credentials(), self._service, self._region).add_auth(aws_req)
        # Copy the signed headers (Authorization, X-Amz-Date, X-Amz-Security-Token) back.
        request.headers.update(dict(aws_req.headers))
        yield request


def _gateway_url() -> str:
    url = os.environ.get("GATEWAY_URL")
    if not url:
        sys.exit("Set GATEWAY_URL (printed by scripts/register-gateway.sh).")
    return url


def _region_from_url(url: str) -> str:
    # https://<id>.gateway.bedrock-agentcore.<region>.amazonaws.com/mcp
    try:
        return url.split(".bedrock-agentcore.")[1].split(".amazonaws.com")[0]
    except IndexError:
        return os.environ.get("AWS_REGION", "us-east-1")


def make_mcp_client():
    """Return an MCPClient pointed at the gateway, signing requests with SigV4."""
    from mcp.client.streamable_http import streamablehttp_client
    from strands.tools.mcp import MCPClient

    url = _gateway_url()
    auth = SigV4Auth_httpx(region=_region_from_url(url))
    return MCPClient(lambda: streamablehttp_client(url, auth=auth))


def main():
    # --list: prove auth + tool discovery without needing a model.
    if "--list" in sys.argv:
        mcp = make_mcp_client()
        with mcp:
            tools = mcp.list_tools_sync()
            names = [getattr(t, "tool_name", getattr(t, "name", str(t))) for t in tools]
            print("Discovered tools:", names)
        return

    from strands import Agent

    mcp = make_mcp_client()
    with mcp:
        agent = Agent(tools=mcp.list_tools_sync())
        agent(
            "Run the tests for scheme 'MyApp' on branch 'main'. Start the build, "
            "poll until it finishes, then summarize pass/fail and any failing tests."
        )


if __name__ == "__main__":
    main()
