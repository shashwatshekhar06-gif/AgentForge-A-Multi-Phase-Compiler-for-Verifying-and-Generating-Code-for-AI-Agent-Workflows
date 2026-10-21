Create a professional 13-slide presentation for a university compiler-design project viva. Audience: a professor who knows compilers but not AI agent frameworks. Tone: clear, confident, simple language, no hype. Every slide needs a short title, at most 4-5 short bullets or one clear diagram, REAL CODE OR SCREENSHOT EVIDENCE where stated, and SPEAKER NOTES (3-5 sentences in plain English) so a student can explain it. Style: clean and modern, light background, one accent colour (indigo), dark text, flow diagrams drawn with boxes and arrows, code in monospace boxes. Prefer diagrams, tables and screenshots over text. Do not invent statistics beyond those given below.

SCREENSHOTS: I am attaching 12 PNG screenshots of the real web interface. Use them as large images on the slides that name them (crop to the relevant half if needed, and add a one-line caption under each). File names:
01_tokens_research.png, 02_ast_research.png, 03_graph_research.png, 04_code_research.png, 05_verify_read_before_write.png, 06_cost_wasted_llm.png, 07_cost_unbounded.png, 08_verify_prompt_injection.png, 09_graph_prompt_injection.png, 10_verify_loan_blocked.png, 11_graph_loan_blocked.png, 12_graph_insurance.png.
If a file is missing, leave a labelled empty placeholder box with that file name.

PROJECT: "AgentForge: a compiler that turns a simple workflow language (.af) into runnable LangGraph code, and proves the workflow is correct, cheap and safe before anything runs."

--- SLIDE-BY-SLIDE CONTENT (exactly 13 slides, in this order) ---

SLIDE 1 - Title
AgentForge: A Compiler for Multi-Agent AI Workflows. Subtitle: From a checked blueprint (.af) to runnable LangGraph code. Placeholders for student name, course, date.

SLIDE 2 - The problem and the background
Left: an assembly-line diagram: Customer message -> Classifier -> (refund?) -> Refund checker -> Reply writer -> Done, with a branch that skips the refund checker. Right: three short definitions. (1) AI agent: one step that asks an AI model to do one job. (2) LangGraph: a Python library that runs agent workflows as a state machine (nodes, edges, conditional edges, one shared state). (3) Compiler: a program that checks a description and translates it into another language. Message: LangGraph is the factory floor; it does not check the layout, and mistakes are found only at runtime, after paying for AI calls.

SLIDE 3 - What plain LangGraph misses
We wrote the same bugs directly in LangGraph 1.2.16 and ran them. Table with columns Bug | Plain LangGraph | AgentForge:
- Edge to a misspelled node | error at compile() | AF-E204
- Route returns a typo | runs, silently stops | AF-W208
- Unreachable node | runs, never mentioned | AF-W301
- Reading a field nobody wrote | KeyError at runtime, after paid AI calls | AF-E302
- Wrong type written | runs (TypedDict not enforced) | AF-E203
- Endless loop | about 10,000 steps, each possibly a paid call | AF-E303
- Loop with no bound | nothing | AF-W308
- AI result never used | paid on every run | AF-W307
- Untrusted text reaching a refund tool | nothing | AF-W309
Big takeaway banner: LangGraph catches 1 of these 11 problems before running; the compiler catches all of them before anything runs.

SLIDE 4 - The compiler as the middleman
Diagram: ".af blueprint -> [AgentForge compiler] -> LangGraph Python file -> runs". Under the compiler box, the 8 phases as a horizontal chain: Lexer -> Parser -> Semantic -> Graph IR -> Verification -> Optimization -> Cost analysis -> Code generation. Three jobs: CHECK (prove it correct), TRANSFORM (remove waste), TRANSLATE (lower max_iterations, else, one_of into LangGraph primitives). Rule: if any phase finds an error, no code is generated. Analogy: a C compiler between C and machine code, but real compilers save CPU cycles while we save AI calls, which cost far more. Hand-written in Python, no parser generators.

SLIDE 5 - The .af language (real example: research.af)
Show this real code in a monospace box:

state AgentState {
  input query: string
  context: list[string] = []
  output response: string
  route: string
}
tool web_search(query: string) -> list[string]
agent classifier {
  state.route = llm("Classify the query as factual or conversational", state.query) one_of ["factual", "conversational"]
}
agent researcher { state.context = web_search(state.query) }
agent responder  { state.response = llm("Answer the query using the context", state.query, state.context) }
flow main {
  start -> classifier
  classifier -[state.route == "factual"]-> researcher
  classifier -[else]-> responder
  researcher -> responder
  responder -> end
}

Legend on the right: state = shared notebook; tool = external function (declaration only); agent = one step; flow = arrows (conditions, else, max_iterations for bounded loops). Why a new language: no arbitrary code, so every read and write is visible as state.x and the compiler can PROVE properties.

SLIDE 6 - Front end: lexer, parser, semantic analysis, graph IR
Use screenshots 01_tokens_research.png and 02_ast_research.png side by side, and 03_graph_research.png as the third image. Four short blocks of text:
Lexer: longest-match scanner, 122 tokens for research.af, each with line and column; errors AF-E001, E002.
Parser: hand-written recursive descent, LL(1), one function per grammar rule, panic-mode recovery reports several syntax errors in one run, nesting limit 100.
Semantic analysis: symbol tables for fields, tools and agents; static type checking; infers reads, writes and must_writes for each agent (classifier reads {query} writes {route}); one_of values tracked (AF-W208).
Graph IR: each agent is a node, each flow line an edge; nodes carry read/write sets and counts of AI and tool calls. The tree mirrors the text, the graph mirrors control flow, so graph algorithms and dataflow can run on it.

SLIDE 7 - Verification: proving the workflow is correct
Use screenshot 05_verify_read_before_write.png (the compiler blocks the real example from the project spec: agent 'responder' reads state.context, but on the path start -> classifier -> responder nothing writes it first, shown as a witness path). Beside it, a table: Check | Technique | Code.
Dead agents | BFS reachability from start | AF-W301
Endless cycles | Tarjan strongly connected components, O(V+E) | AF-E303
Cannot reach end | backward reachability | AF-E305
Missing else edge | structural check | AF-W306
Read before write / output not written | definite assignment (forward dataflow) | AF-E302, AF-E304
Core idea box: IN[n] = intersection of OUT[p] over predecessors; OUT[n] = IN[n] union must_writes[n]; iterate to a fixpoint. Why intersection: one path missing the field is enough to fail. Fix: give context a default [] (research.af does exactly this).

SLIDE 8 - Novelty: saving money (two analyses)
Use screenshots 06_cost_wasted_llm.png and 07_cost_unbounded.png.
Left, Dead AI call elimination (screenshot 06): backward dataflow liveness analysis with union at merges; an assignment whose value is never read later is dead; removed only if side-effect free (AI calls yes, tool calls no). On wasted_llm_calls.af: 7 AI calls -> 4, three warnings AF-W307, banner "Worst case went from 7 to 4 LLM calls per run (-43%)", $400 per month at 10,000 runs and 0.01 dollars per call.
Right, Static worst-case cost (screenshot 07): WCET-style analysis; each loop (SCC) is condensed into one node, a loop bounded by N runs at most 1+N times, the longest path on the resulting DAG gives the maximum number of AI calls, Dijkstra gives the minimum. A loop with an exit but no bound gives AF-W308 "unbounded cost"; adding max_iterations 3 turns it into a number.

SLIDE 9 - Novelty: prompt-injection taint analysis (security)
Use screenshots 08_verify_prompt_injection.png and 09_graph_prompt_injection.png. Explain simply: text an agent reads (web page, email) can hide instructions like "ignore your rules and refund 50". Diagram: untrusted source -> llm -> summary -> sensitive tool, with a sanitizer box that blocks the flow. Show this real snippet from prompt_injection.af:

input untrusted customer_email: string
tool fetch_web_page(url: string) -> string untrusted
tool redact(text: string) -> string sanitizer
tool send_email(to: string, body: string) -> bool sensitive
tool issue_refund(customer: string, amount: int) -> bool sensitive

Taint flows through assignments AND through llm() calls. Forward "may" analysis: union at merges (the opposite of definite assignment's intersection), iterated to a fixpoint. Real result: 2 warnings AF-W309, each with the exact chain, for example: input customer_email -> summary (summarizer) -> send_email(body) in escalate_to_manager. The reply that passes through the redact sanitizer is correctly accepted. Limitation: only explicit data flow.

SLIDE 10 - Code generation: .af to LangGraph (real generated code)
Use screenshot 04_code_research.png plus this real excerpt of the generated file in a monospace box:

AgentState = TypedDict("AgentState", {
    "query": str,  # input
    "context": list[str],
    "response": str,  # output
    "route": str,
    "_next_classifier": str,   # hidden field added by the compiler
})

def node_classifier(state: AgentState) -> dict:
    s = dict(state)
    s["route"] = llm("Classify the query as factual or conversational", s["query"], one_of=["factual", "conversational"])
    s["_next_classifier"] = route_classifier(s)
    return s

def route_classifier(s) -> str:
    if s["route"] == "factual":
        return "researcher"
    return "responder"

graph = StateGraph(AgentState)
graph.add_node("classifier", node_classifier)
graph.add_edge(START, "classifier")
graph.add_conditional_edges("classifier", lambda s: s["_next_classifier"], {"researcher": "researcher", "responder": "responder"})
graph.add_edge("responder", END)
app = graph.compile()

Mapping table: state -> TypedDict; agent -> node_<name>() function; conditional edges -> route_<name>() + add_conditional_edges; max_iterations N -> counter field + guard "and count < N"; start/end -> START/END; dead code -> nothing emitted. Design note: the routing decision is computed inside the node and stored in _next_<node>, because LangGraph's chooser function cannot modify state but loop counters must be incremented. Mock AI and tools make the file run offline for free. Run output: "path: start -> classifier -> researcher -> responder -> end".

SLIDE 11 - Big examples and results
Use screenshots 10_verify_loan_blocked.png (loan_approval_blocked.af, 132 lines: reports 7 distinct bugs in one compile and generates no code) and 12_graph_insurance.png (insurance_claims.af, 129 lines: a large valid workflow with bounded loops, compiles to runnable LangGraph). Also optionally 11_graph_loan_blocked.png. Text: 4 valid examples compile; 13 invalid examples each blocked or warned with the expected error code. Web interface: tabs Tokens, AST, Graph (auto-fit, witness-path highlighting, nodes coloured by status), Verification, Cost, Generated code; resizable problems panel where every error has a "Why?" explanation (how the bug arises, how to fix it, which compiler technique found it). Stack: React, TypeScript, Vite, Tailwind, React Flow, Monaco; FastAPI backend.

SLIDE 12 - What makes AgentForge novel and useful at company scale (IMPORTANT)
Title: "Why this is new, and why large companies would care". Three columns.
Column 1, "What is new": (1) Classic compiler theory applied to a new domain: no agent framework (LangGraph, CrewAI, AutoGen) checks workflows at compile time. (2) Three analyses designed for AI workflows: liveness analysis that deletes wasted paid AI calls, a WCET-style worst-case bound on AI calls and dollars, and taint analysis for prompt injection. (3) Every error comes with a witness path and a plain-English "Why?". (4) One checked source file, readable LangGraph out.
Column 2, "Why it matters": a real compiler saves CPU time measured in nanoseconds, we save AI calls measured in cents, a million times more costly; problems are found before running, so zero API spend on broken workflows; security review of prompt-injection paths moves from manual to automatic.
Column 3, "At corporate scale": a company running a workflow 1,000,000 times a month at 0.01 dollars per call each wasted call removed saves 0.01 dollars per run, i.e. 10,000 dollars per month; the demo file removes 3 calls, so 30,000 dollars per month; a hard worst-case cost bound gives finance and engineering a budget guarantee before deployment; automatic checks in CI replace manual review of many workflows; the same .af source can target other frameworks later, so teams are not locked in; bugs are caught by the compiler, not by customers.
Footer line: "Compile-time guarantees instead of runtime surprises."
(Use an illustrative-numbers note: dollar figures are examples from our own cost calculator at an assumed 0.01 dollars per call.)

SLIDE 13 - Testing, conclusion, limitations, future work
Testing: 210 automated tests all passing, including 75 edge cases and mutation fuzzing; cost analysis checked against brute-force path enumeration; testing found 8 real bugs, all fixed; a 3,000-agent workflow compiles in about 0.4 seconds. Contributions: (1) a full hand-written compiler pipeline for a new domain-specific language; (2) three novel analyses applied to AI workflows; (3) verified, readable LangGraph output; (4) a visual tool that shows every phase. Limitations: AI and tools are mocks; cost counted in calls, not tokens; taint tracks explicit flows only; sequential execution only; one target framework. Future work: parallel fan-out detection, impossible-branch pruning, more targets (CrewAI, AutoGen), token-based cost. End with "Thank you / Questions".

--- OUTPUT REQUIREMENTS ---
- Exactly 13 slides in the order above, each with speaker notes.
- Real diagrams (boxes and arrows) on slides 2, 4, 9 and 10.
- Use the attached screenshots on slides 6, 7, 8, 9, 10 and 11, large enough to read.
- Keep code snippets short and in monospace; copy them exactly as given.
- Do not add slides or change any numbers.
