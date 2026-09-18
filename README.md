# mcp-connector-testkit

A test kit for MCP connectors, organised in five layers — protocol conformance, tool
contracts and annotations, authorization and tenant isolation, upstream API behaviour, and
agent-level evaluation — each with its own oracle.

The repository has two parts:

- `src/mcpqa` — the kit, with no dependency on any MCP SDK;
- `demo/` — something realistic to point it at: a fake mail provider API and an MCP
  connector in front of it, built on the official Python SDK.

Work in progress.
