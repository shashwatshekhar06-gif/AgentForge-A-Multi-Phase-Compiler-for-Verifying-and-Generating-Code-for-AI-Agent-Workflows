# What is new in AgentForge, and why it saves money

## 1. The problem

Teams building multi-agent AI systems (LangGraph, CrewAI, AutoGen) wire the agents together by hand in Python. Three kinds of mistakes are common, and all three cost real money because every LLM call is billed:

1. **Structural bugs that only appear at runtime.** An agent reads a value that a branch forgot to fill, a loop never exits, an agent is never reached. The bug shows up after the calls leading to it have already been paid for, sometimes thousands of times in production before someone notices.
2. **Wasted calls.** After a refactor an agent still asks the LLM for something that nothing uses any more. The workflow works, so nobody notices, and every run keeps paying for it.
3. **Unbounded spend.** A "retry until good" loop has no limit. When the exit condition never comes true, one run keeps calling the model.

Ordinary programming languages solved the equivalent problems decades ago with compiler techniques. AgentForge applies those techniques to agent workflows.

## 2. What is new

| # | Feature | Compiler technique it is based on | What it saves |
|---|---|---|---|
| 1 | **Structural verification before anything runs:** read-before-write on any path, missing outputs, endless cycles, dead agents, dead ends | definite assignment (forward dataflow), reachability (BFS), Tarjan's strongly connected components | runtime failures and the calls spent before them |
| 2 | **Witness paths:** every data-flow error names one concrete path that triggers it | path reconstruction from the dataflow result | debugging time |
| 3 | **Dead LLM call elimination:** removes `llm(...)` calls whose result is never read, from the generated code | liveness analysis (backward dataflow) and dead store elimination | the cost of the wasted calls, on every run |
| 4 | **Static worst-case cost bound:** the maximum and minimum number of LLM and tool calls one run can make, plus a warning when a loop has no ceiling | worst-case execution time (WCET) analysis over SCCs | budget surprises and runaway loops |
| 5 | **Bounds enforced at runtime:** `max_iterations` is compiled into a hidden counter, so the worst case the compiler reports is also a guarantee about the generated program | code generation with runtime support | — |
| 6 | **Prompt-injection detection:** reports when text from an untrusted source (web page, email, user message) can reach a tool with real-world effects, with the exact chain the data travels | taint analysis (sources, propagation, sanitizers, sinks) as a forward dataflow analysis | security incidents: fraudulent refunds, data leaks, unwanted emails |
| 7 | **A visual compiler:** every phase (tokens, AST, graph IR, verification, cost, code) is visible in the UI, and every error has a "Why?" explanation | — | learning and code review time |
| 8 | **Agent selection (`compare`):** agents that do the same job are scored on test cases and the best one is wired into the generated code. The compiler first proves each candidate could replace the wired agent without breaking a verified guarantee | profile-guided optimization: re-running the whole verification on a copy of the graph with the candidate swapped in, then measuring the candidates and selecting the winner | shipping the worse prompt or model, and crashes or injection paths introduced by a swap |
| 9 | **Agent pruning (`necessity`):** test cases find the agents the workflow does not need, by running it with and without each agent on a real model; an agent that makes no difference is removed from the generated code | profile-guided optimization, the measured counterpart of dead code elimination (ablation) | the cost of agents that add nothing, on every run |

Features 3 and 4 were added to strengthen the project's evaluation on cost effectiveness, and feature 6 for security. To our knowledge, mainstream agent frameworks do not offer them as compile-time analyses. LangGraph's own `graph.compile()` checks basic graph structure, such as unknown nodes, but it does not check whether data is available on every path, remove unused LLM calls, or bound the number of calls.

## 3. How the two new analyses work

### 3.1 Dead LLM call elimination (`backend/compiler/optimize.py`)

A field is **live** at a point if some path from there may still read its current value. Liveness is computed **backwards** through the graph:

```
LIVE_IN[end]  = the output fields
LIVE_OUT[n]   = union of LIVE_IN of n's successors, plus the fields n's edge conditions test
LIVE_IN[n]    = computed by walking n's statements backwards:
                  state.x = e   ->   if x is live (or e has side effects): live = live - {x} + fields read by e
                                     otherwise: this assignment is DEAD and is removed
```

The sets only grow, and there are finitely many fields, so iterating until nothing changes always terminates, even with loops. Because a dead assignment's own inputs are not made live, chains of useless calls disappear together.

**Safety rule:** only side-effect-free assignments are removed: `llm(...)` calls, literals and copies of state. Tool calls are always kept, because a tool may send an email or write to a database.

### 3.2 Static cost analysis (`backend/compiler/cost.py`)

1. Count the calls in each agent body. An `if` counts its most expensive branch for the worst case and its cheapest for the best case. The right side of `and`/`or` may be skipped, so it only counts for the worst case.
2. Group the graph into strongly connected components (SCCs). Every loop lies inside one SCC.
3. If an SCC still contains a cycle after removing its `max_iterations` edges, the loop has no limit. Its cost is **unbounded** (AF-W308).
4. Otherwise each trip around the loop uses up one traversal of a bounded edge. The generated code allows each bounded edge at most N times per run, so the SCC is passed through at most `1 + ΣN` times.
5. Collapse each SCC into one weighted node. The graph is now acyclic, so the most expensive path from start to end is a longest-path computation in topological order. The cheapest path is a shortest-path computation.

The worst case is a **guaranteed upper bound**: no run of the generated program can make more calls.

### 3.3 Prompt-injection taint analysis (`backend/compiler/taint.py`)

The developer tags tools in the `.af` file: `untrusted` (for example `fetch_web_page`), `sensitive` (for example `send_email` or `issue_refund`), and `sanitizer` (for example `redact`). Input fields can be `input untrusted`. The analysis then tracks, for every field at every point of the workflow, which untrusted sources its value may come from:
- assignments and `llm(...)` calls carry taint forward;
- a sanitizer's result is clean;
- paths are combined with union;
- the analysis iterates to a fixpoint.

Every sensitive call with a tainted argument is reported with its chain, for example `input state.customer_email -> state.summary (summarizer) -> state.refund_amount (decide_refund) -> issue_refund(amount)`. In `prompt_injection.af` the compiler finds exactly the two unsafe flows (a manager email and an LLM-chosen refund amount), and correctly accepts the reply that goes through the `redact` sanitizer.

## 4. Results on the examples

Prices for illustration: $0.01 per LLM call, tools free, 10,000 runs per month. The UI's Cost tab lets you change these numbers.

| Workflow | Worst case LLM calls / run | Best case | Worst case / month | Notes |
|---|---:|---:|---:|---|
| `research.af` | 2 | 2 | $200 | the search tool is skipped on the conversational path |
| `fraud.af` | 2 (+8 tool calls) | 1 | $200 | the bounded retry loop runs the analyst at most 4 times |
| `support_pipeline.af` | 3 | 3 | $300 | |
| `insurance_claims.af` | 9 (+15 tool calls) | 3 | $900 | two bounded loops; the rejection path costs only $300 |
| `wasted_llm_calls.af` | **4 (was 7)** | 4 | **$400 (was $700)** | 3 wasted calls removed: **−43%, $300 saved per month** |
| `unbounded_cost.af` | **unbounded** | 3 | **unbounded** | AF-W308; adding `max_iterations 3` gives a ceiling of 12 calls |
| `prompt_injection.af` | 4 | 4 | $400 | 2 unsafe flows into `send_email` and `issue_refund` found; the sanitized reply is accepted |
| `loan_approval_blocked.af` | — | — | **$0** | 7 structural bugs found; no code generated, nothing spent |

The loan workflow shows the value of verification. Without the compiler, every small-loan application would run through credit checks and document collection, paying for those calls, and then underwrite using an empty `debt_ratio`, silently producing a wrong assessment. The compiler finds this, and six other bugs, in milliseconds, before a single call is made.

### 3.4 Agent selection: profile-guided optimization (`candidates.py`, `evaluate.py`, `optimize.py`)

Verification proves the workflow is wired correctly, but not that an agent gives *good answers*. That depends on the model, so it has to be **measured**. This is the same idea as profile-guided optimization (PGO) in an ordinary compiler: run the program on sample inputs, record what happened, then compile again using the measurements. Here the sample inputs are the test cases of a `compare` block, the measurement is how many cases each candidate passes, and the optimization is: **wire the best candidate into the generated LangGraph code**.

It works on any workflow: add a `compare` block next to the agents it concerns (`fraud.af`, `support_pipeline.af`, `compare_agents.af`, `compare_real_model.af` and `triage_end_to_end.af` each have one). A `compare` block lists candidates, agents that do the same job, and test cases. One candidate is wired into the flow; the others wait outside it. The compiler then does three things.

1. **Proves each swap is safe (verification).** For every other candidate it builds a copy of the graph with that candidate in the wired one's place and runs the *whole* verification again. Every problem that is new compared with the original graph is reported: AF-E216 for an error (the candidate reads a field nothing has written, so it would crash after paid calls), AF-W310 for a warning (it would open a prompt-injection path). So whichever candidate wins, nothing the compiler guaranteed is lost.
2. **Scores the candidates (profile).** Each candidate runs on each test case; `contains` ignores letter case, `==` is exact. The candidate that passes the most cases is the best, and a tie keeps the agent already in the flow.
3. **Selects the winner (optimization, `select_agents`).** The best candidate replaces the wired agent in the graph *before* dead-agent removal, dead-call elimination, cost analysis and code generation, so all of them see the agent that will really run. It is applied only if the block has exactly one wired candidate, the best one differs, and its swap added no verification warning (AF-W310): a winner that would open a prompt-injection path is reported but never applied.

Two kinds of profile:
- **Mock profile** (default, offline, free): each case carries the answer every candidate "gives" (`mock responder_a "..."`), so the selection is repeatable and shows the mechanism. It says nothing about a real model. The compiler computes it with a small interpreter (`evaluate.py`), so the web server never runs generated code.
- **Measured profile** (`python -m compiler file.af -o workflow.py --profile real`, with `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` in `backend/.env` or the terminal; the UI has a "Measure with a real model" button that asks first, shows the call count, and is capped at 60 calls): the compiler compiles once, runs the generated file's test harness against the real model (`workflow.py --scores-json`), then compiles again with those scores. The generated code uses the agent the real model preferred. This makes paid API calls, only when asked.

**Judging by the whole workflow (`end_to_end`).** A bad answer does not stay where it is made: every agent after it builds on it. With `end_to_end`, each test case runs the entire workflow with a candidate in the wired place and the *final* output is judged, so a candidate that looks fine alone but hurts the agents after it loses. `triage_end_to_end.af` shows it: a loose prompt answers "This message is urgent." instead of "urgent", the router does not match, and an urgent ticket is silently queued. Measured with a real model, the loose agent scored 2/4 and the strict one 4/4 (the loose agent still "passes" the two normal-priority cases by luck, because the else branch hides its mistake).

**A run-time check (`ensure`).** A compare block can state what a valid answer looks like (`ensure state.priority == "urgent" or state.priority == "normal"`). In the generated code, with a real model, the wired agent is wrapped in that check: a bad answer makes the run fall back to the next candidate, and if none passes the run stops instead of passing the bad value on. The optimizer treats the fields `ensure` reads as live, so it cannot remove the assignment the check depends on.

The generated file also keeps the harness: running it prints the PASS/FAIL table for every candidate, and `AGENTFORGE_USE_BEST=1` re-picks the winner at run time. The Evaluation tab in the UI shows the table and what the optimizer did.

### 3.5 Agent pruning: which agents does the workflow need? (`necessity.py`, `optimize.py`)

Dead LLM call elimination removes calls whose result nothing reads. Some agents are not dead in that sense, because later agents do read their output, yet the final answer is just as good without them. That cannot be seen in the program text, so it is measured, in the same profile-guided way as agent selection:

1. **Decide statically what can be tested.** An agent can be skipped by sending its incoming edges to the agent that followed it. The compiler does that on a copy of the graph and runs the whole verification again. If verification would fail (a later agent reads a field only this agent writes, an output is no longer written), the program itself proves the agent is needed and no model call is spent (`structural`). Agents that call a tool (tools are stubs and may have real effects), that choose the next step, or that are compare candidates are never removed automatically.
2. **Measure.** With a real model the generated file runs the whole workflow on every test case as written, then leaves each testable agent out in turn (the most expensive first) and keeps it out if the final output did not get worse. Agents are only removed on evidence: if the workflow as written passes no case, nothing is tested.
3. **Prune.** The optimizer removes the agents that were not needed, re-checking each removal with verification, before dead-agent removal, cost analysis and code generation, so the cost bound already counts the cheaper workflow.

In `examples/valid/necessity_demo.af` (drafter, reviewer, finisher) a real model produced: drafter needed (0/3 cases without it), reviewer not needed (3/3 without it, removed, one LLM call saved on every run), finisher structurally needed. The result depends on the test cases: an agent is only "not needed" for the cases that were tried.

## 5. Evidence that it works

- **342 automated tests**, all passing, including 24 for the cost analyses, 14 for taint analysis and 65 for agent comparison, 18 for whole-workflow scoring and run-time checks, 10 for the real-model API endpoint, 24 for agent pruning.
- **Differential testing:** the verifier is compared with an independent brute-force checker on 480 random workflows, and the cost analysis with brute-force path enumeration on 240 generated workflows (every one with a path to `end` is compared). There were zero disagreements.
- **Fuzzing:** more than 4,000 corrupted and random programs, with no crash and every diagnostic well formed.
- Generated programs are **executed** in the tests, including the optimized one, to show the removed calls do not break anything.

See `docs/testing.md` for the details.

## 6. Honest limits

- The cost analysis counts **calls**, not tokens. Prices are the user's estimates, but the call counts are exact bounds.
- Verification is **structural**. It proves the workflow is wired correctly, not that an LLM will give good answers. The `compare` scores come from test cases the developer writes; in mock mode the answers are scripted, so they show the mechanism and say nothing about a real model.
- The selection is only as good as the test cases. With the mock profile the winner is decided by answers the developer scripted, so it shows the mechanism and says nothing about a real model; use `--profile real` for that.
- Only compare blocks with exactly one wired candidate are swapped, and a winner that would add a verification warning is not applied.
- The worst case is a safe upper bound. In workflows with several loops it can be higher than any real run, because it assumes every loop uses its full budget.
