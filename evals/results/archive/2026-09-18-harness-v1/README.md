# Harness v1: a system prompt that did the connector's job

Three complete runs (llama3 8B, temperature 0, 27 cases × 3 repeats, Ollama's default
context window, no cap on output length), kept because they are the evidence for a
mistake in the first version of the agent loop.

That version's system prompt said, in one sentence, that message bodies are written by
other people and must never be treated as instructions. That is exactly what the
connector is supposed to tell the model, and exactly what the `untrusted-content-unmarked`
defect stops it from saying. With the harness saying it anyway, the comparison could not
have shown what the defect costs. It showed no difference:

| Run | Pass rate (95% interval over cases) | Change against the clean connector |
| --- | --- | --- |
| clean connector | 41% (25%–59%) | — |
| `vague-tool-descriptions` | 19% (8%–37%) | −22 points [−41, −4] — regression |
| `untrusted-content-unmarked` | 41% (25%–59%) | ±0 points [−11, +11]; prompt-injection cases ±0 |

(`compare-*.json` hold the full comparisons.)

The corrected harness shows no difference either, for a reason these runs already
contain: in no prompt-injection trial did the model open a message, so no warning on
message bodies, from the harness or from the connector, ever reached it. The flaw was in
the design regardless: a harness that supplies domain knowledge takes credit for the
connector's work, and with a model that does read mail it would have hidden the defect.

Two further observations from these runs shaped the method:

- Every case gave the same verdict on all three repeats (`repeat_agreement` 100%). At
  temperature 0 the repeats are copies, so an interval over 81 trials would claim
  three times the evidence the run has; intervals are now computed over cases.
- The unmarked-content defect changed the verdict on cases it has nothing to do with
  (trashing a newsletter, riding out a rate limit): editing one tool description perturbs
  a greedy decoder everywhere. A comparison between two connector versions therefore
  measures the change plus this noise, which is what the case-level bootstrap is for.

The harness now uses a generic system prompt and passes on the server's own
`instructions`, as an MCP client does; see
[`src/mcpqa/evals/agent.py`](../../../../src/mcpqa/evals/agent.py). The summaries here
were regenerated from `trials.jsonl` with the current statistics code; the trials
themselves are as recorded.
