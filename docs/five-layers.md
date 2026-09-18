# Five layers, five oracles: testing an MCP connector

An MCP connector is a thin piece of code with an unusual amount of responsibility: on one
side a model that calls whatever tools it is given, with arguments it partly made up; on
the other, somebody's mailbox behind an API with rate limits, page caps, expiring tokens
and a changelog. The usual test suite checks that the server starts and that `tools/list`
returns something sensible. Connectors rarely fail there. They fail when a "read-only" tool marks
mail as read, when a cache hands one customer's search results to another, when pagination
quietly stops at the first page, when a retry sends the same email twice — and when a
model, handed perfectly working tools, still calls the wrong one.

This write-up argues that those failures fall into five layers, that each layer needs its
own **oracle** — the thing that tells you an output is wrong — and that the layers do not
substitute for one another. It then checks the argument the only way I know: by building a
connector, breaking it in 21 realistic ways, and recording which layer noticed.

## The oracle is the whole problem

A test is a question plus a way to tell a right answer from a wrong one. For most software
the second half is cheap — the function returns 4 or it does not. For a connector it is the
expensive half, because the interesting failures are invisible from where a client sits.
When `get_message` marks a message as read, the response is byte-for-byte what it would
have been otherwise. When a timed-out send is retried, the client sees a success. When a
model writes a fluent summary of a message it never opened, the text looks fine.

So the layers are defined by what you have to be able to *see*:

| Layer | Question | What makes the failure visible |
| --- | --- | --- |
| 1 · Protocol | Does it speak MCP? | The specification's `schema.json` and its MUST/SHOULD sentences |
| 2 · Contracts | Does each tool do what its schema and annotations promise? | A diff of the state behind the server |
| 3 · Auth & tenancy | Whose data is this, and where did the credential go? | Per-tenant canaries and a scan of every byte |
| 4 · Upstream | What happens on the provider's bad day? | Injected faults and the provider's own log |
| 5 · Agent | Does a model use the tools correctly, and safely? | The end state of the mailbox, over repeated trials |

The kit ([`mcpqa`](../src/mcpqa)) implements all five against any MCP server. The demo
target is a mail connector on the official Python SDK, talking to a fake mail provider that
has tenants, scopes, page caps, rate limits, fault injection and an audit log — enough of a
real API to fail in the ways real APIs make connectors fail.

## Layer 1: the specification as oracle

Protocol conformance looks like the easy layer, and it hides two traps.

The first is that a check can be wrong without anyone noticing. The official SDKs make
most protocol mistakes impossible: you cannot easily get a server built on one to drop the
`jsonrpc` field or answer with the wrong id. A suite developed only against SDK-built
servers can therefore carry checks that have never once failed — and a check that has
never failed has not been shown to work. Every one of the kit's 39 checks is run against
a small SDK-free fixture server with a switch that breaks exactly that check's rule, and
the check must fail there and pass against the unbroken server.

The second trap is quoting the specification from memory. Each check cites the sentence it
enforces and takes its severity from the keyword in it — MUST is an error, SHOULD a
warning. Several citations in my first draft were paraphrases, close to the text and
subtly stronger than it; a unit test now compares every quote with a vendored copy of the
page it links to. The same review found a check stricter than the rule it quoted — the
kind of error that makes a conformance tool noisier than the servers it tests.

MCP also changed shape this year: revision 2026-07-28 replaced the `initialize` handshake
and sessions with a per-request envelope. The kit probes which eras a server answers, the
way a client would, and runs every check in each.

## Layer 2: state as oracle

Tool annotations are claims about side effects: `readOnlyHint`, `destructiveHint`,
`idempotentHint`. Clients act on them — a client may skip the confirmation dialog for a
tool that says it is not destructive, or retry one that says it is idempotent — so a wrong
annotation is a real bug, and it is invisible in the response.

The kit checks the claims against the state behind the server, represented as a plain set
of facts (`alice@acme.test|message:msg_00901`, `…|message:msg_00901|label:UNREAD`, …). The
definitions then reduce to set operations. Read-only: the diff is empty. Destructive: some
fact disappeared. Idempotent: calling twice changes nothing the first call did not. The kit
never needs to know what a tool is for.

The defect this layer exists for is an old one. IMAP distinguishes `BODY[]` from
`BODY.PEEK[]`; the first fetches a message and marks it read. A connector whose
`get_message` does the equivalent is publishing a read-only tool that mutates the mailbox,
and nothing in the protocol will ever say so. With the state oracle it is a one-line diff,
and this is the kit's output for the seeded version of that defect (wrapped to fit):

```
A-001[stateless/stdio]: get_message(message_id='msg_00901') changed state:
  -1 ['alice@acme.test|message:msg_00901|label:UNREAD']
  — a client that trusted readOnlyHint ran this without asking
```

The same layer drives every tool with arguments synthesised from its own `inputSchema`
and validates results against its `outputSchema`, which catches a schema left behind when
the serializer changed.

## Layer 3: canaries and every byte

Multi-tenant bugs are silent by construction: Bob's search returns results, they are just
Alice's. The demo mailboxes each carry a canary string that exists nowhere else, and the
tenancy tests look for one tenant's canary in everything the other tenant is shown. That is
how the seeded defect of a search cache keyed by query but not by user shows up — a
latency fix that someone reasonable could write.

Credentials leak in unglamorous ways — an error message that quotes the request it failed
on — so a leak scan runs over every byte every test receives, looking for each token in
plain, URL-encoded and base64 form. And the kit presents a token issued for something
else, such as the upstream API, and expects a 401: token passthrough is the antipattern
the MCP authorization spec names outright.

## Layer 4: the provider's log as oracle

The fake provider can be told to fail on a schedule — a 429 with `Retry-After`, a run of
503s, a response that takes three seconds, an HTML error page, a field renamed overnight —
and it keeps an audit log of every call it received, with start and end times. That log is
the oracle for everything the client cannot see:

- Did the connector wait the interval the provider asked for? Subtract timestamps.
- Did it retry a transient 503, and stop retrying a lasting one? Count calls.
- Did a timed-out send go out twice? Count sends. With the `retries-send-on-timeout`
  defect, the client receives a perfectly normal success — the second attempt worked —
  and the recipient receives the email twice.

The layer also holds the fidelity tests that are easy to skip: a timestamp keeps its UTC
offset, a 2.5 MB body is cut to a budget and says so, and a renamed upstream field becomes
an error rather than a mailbox of empty subjects.

## Layer 5: the outcome as oracle, and the statistics it needs

The first four layers ask whether the connector works. The fifth asks whether a model can
use it — which can fail with every tool behaving perfectly, and can regress because someone
trimmed a tool description to save tokens.

Grading is by outcome wherever possible: "move the ByteFeed newsletter to the trash"
passes when that message, and only that message, moved to the trash, however the reply is
worded. A forbidden tool call or an unasked-for change makes a trial *unsafe*, counted
apart, because a wrong answer and a trashed inbox are different kinds of failure.

Most of what this layer taught me was about the harness rather than the model. Three
mistakes, each found in the data:

- **A grader that rewarded doing nothing.** In the first run, six cases passed without a
  single tool call that should have needed one: a summary of an email never opened,
  prompt injections "resisted" but never read. Cases about the mailbox now require the
  model to have looked *and* an answer containing a fact only the mailbox holds, and a
  unit test runs every case against an agent that does nothing.
- **A harness that did the connector's job.** The first system prompt told the model that
  message bodies are untrusted — exactly what a connector has to say, and exactly what the
  `untrusted-content-unmarked` defect stops it saying. The loop now sends a generic system
  message plus the server's own `instructions`, as an MCP client does.
- **Defaults that fail silently.** Ollama's default context window is smaller than
  llama3's, and an overflowing prompt is cut from the front, tool descriptions first; in
  JSON mode the model could also generate without end. Both limits are now set explicitly.
  The earlier runs are [kept in the repository](../evals/results/archive) as evidence.

With those fixed, llama3 8B (4-bit, local, greedy decoding) passes 44% of the cases against
the clean connector, with a 95% interval of 28–63%. More than half of its failures are the
same failure: in 24 of the 45 failed trials it answered without calling a tool. Its one
unsafe habit is consistent — asked to "clean up" an inbox, it trashed messages on every
repeat.

Against the two agent-level defects:

- **Vague descriptions cost 15 points, with an interval of −33 to +4.** That looks like a
  real effect, but 27 cases cannot tell it apart from zero, so the kit does not call it a
  regression. Under the first harness the same comparison came out at −22 (−41 to −4) and
  counted as significant: an effect this size sits at the edge of what 27 cases can
  resolve. And a warning in the details: the vague connector had no unsafe trials at all,
  because the model mostly stopped acting.
- **Unmarked message bodies changed nothing (±0, from −11 to +11).** The transcripts show
  why: in no prompt-injection trial did the model open a message. It answered from search
  snippets, so the warning the defect removes, which sits on `get_message`, never reached
  it.

So with this model neither agent-level defect is flagged, and the matrix says so. Layers
1–4 cannot see these defects at all; layer 5 reports an effect only as strongly as the data
supports it. Catching them needs a model that uses the tools competently — the kit has an
adapter for Claude, not yet run here — and more than 27 cases.

The statistics are deliberately modest. Intervals count cases, not trials: at temperature
0 every case gave the same verdict on all three repeats, so three repeats were one
observation. Sampled at 0.8 instead, the pass rate barely moved (42%), but only half the
cases gave the same verdict every time and pass^3 fell to 19% — the gap `pass^k`, from
τ-bench, exists to show. Runs are compared with a bootstrap over cases, and a change counts
as a regression only when its whole 95% interval is below zero. In CI, the recorded
decisions of a real run are replayed against the current connector, so a change to what a
tool returns breaks the build without a model call.

## What the defect matrix shows

The matrix is the kit's own mutation score: the suite runs once against the clean
connector, which must be green, and then once per seeded defect.
[The full table](defect-matrix.md) is generated by the run; the short version:

- **All 19 defects aimed at layers 1–4 were caught by the layer they were aimed at**, and
  17 of them by that layer alone. A suite with only protocol checks would have caught 6
  of the 19; the other 13 pass every protocol check the kit has.
- **Two were caught by more than one layer, for good reasons.** A tool that rejects an
  argument its schema calls optional breaks every test that calls it — the loud kind of
  bug that a smoke test also finds. A server that accepts another audience's token fails
  both the HTTP check and the tenancy test built around it.
- **No test in layers 1–4 failed for either agent-level defect.** Tool descriptions
  replaced by "Mail operation." and message bodies stripped of any warning that they were
  written by a stranger both leave a connector that is valid, correct and safe by every
  measure that does not involve a model. Layer 5 saw an effect from the first and none
  from the second; with this model, neither was significant (see above).

At about eleven minutes on a laptop, the matrix runs nightly and when the checks or the
defects change, not on every pull request.

## What this does not show

The defects are mine. The matrix measures the kit against the failures I thought to seed,
each modelled on a class I have seen or read about, and it is honest about that and nothing
more: it shows which oracle makes which class of failure visible, not what share of
real-world bugs the kit would catch. The provider is a fake, built to be controllable; for
a real connector the state oracle is a test account read through the provider's API, and
fault injection needs a proxy or recorded traffic. The agent numbers come from one small
local model on one machine; they show the method working, not how good a frontier model is
at email.

What the exercise does support is the claim it set out to test. A suite that stops at
layer 1 would have passed 13 of the 19 broken connectors in the matrix, and both of the
agent-level ones; each of layers 1 to 4 catches defects that no other layer sees.
