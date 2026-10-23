"""Code generation: optimized IR -> one runnable LangGraph Python file.

The generated file contains, in order:
  1. a TypedDict state class (user fields + hidden routing/loop fields),
  2. an `llm` helper and one stub function per tool (mock mode, no API keys needed),
  3. one node function per agent (its statements translated to Python),
  4. one routing function per branching node,
  5. the StateGraph wiring (add_node, add_edge, add_conditional_edges) and graph.compile(),
  6. when the program has `compare` blocks: the candidate agents, the test cases and the scoring code,
  7. a __main__ block that scores the candidates (if any), then runs the workflow once with sample inputs.

How routing works: a node with conditional edges computes its next step at the end
of its own function and stores it in a hidden field `_next_<node>`. The graph then
follows that field. Loop counters for max_iterations live in hidden `_count_<i>` fields.
"""

import json
import keyword
import pprint

from . import ast_nodes as ast
from .ir import IRGraph
from .necessity import classify, agents_to_test
from .semantic import SymbolTable

PY_TYPES = {"string": "str", "int": "int", "bool": "bool"}
PRECEDENCE = {"or": 1, "and": 2, "not": 3}   # comparisons are 4 (bind tightest)


def py_type(t: str) -> str:
    if t.startswith("list["):
        return f"list[{py_type(t[5:-1])}]"
    return PY_TYPES[t]


def empty_value(t: str) -> str:
    """Python source for an 'empty' value of a type."""
    if t.startswith("list["):
        return "[]"
    return {"string": '""', "int": "0", "bool": "False"}[t]


def sample_value(t: str, label: str) -> str:
    """Python source for a plausible placeholder value (used by mocks and sample inputs)."""
    if t.startswith("list["):
        inner = t[5:-1]
        return f"[{sample_value(inner, label + ' 1')}, {sample_value(inner, label + ' 2')}]"
    return {"string": py_str(f"[{label}]"), "int": "1", "bool": "True"}[t]


def py_str(text: str) -> str:
    """A double quoted Python string literal (JSON string syntax is valid Python)."""
    return json.dumps(text)


def safe_name(name: str) -> str:
    """A parameter called `from` or `class` is fine in .af but not in Python: add a trailing _."""
    return name + "_" if keyword.iskeyword(name) else name


# The generated llm(...) helper. Three behaviours:
#   - compare test case in mock mode: return the answer scripted for the candidate being tested,
#   - AGENTFORGE_LLM=real: ask a real model (OpenAI or Anthropic API, standard library only),
#   - otherwise: a placeholder answer, so the workflow runs offline.
LLM_HELPER = r'''
LLM_MODE = os.environ.get("AGENTFORGE_LLM", "mock")   # "mock" (default, offline) or "real" (needs an API key)
EVAL_SCRIPT = None   # set while a compare test case runs in mock mode: the scripted answer of the candidate


def real_llm(prompt, context, one_of):
    """Ask a real model. Set OPENAI_API_KEY or ANTHROPIC_API_KEY in your environment (never in this file).
    AGENTFORGE_PROVIDER ("openai" or "anthropic") picks one when both keys are set; AGENTFORGE_MODEL picks the model."""
    provider = os.environ.get("AGENTFORGE_PROVIDER") or ("openai" if os.environ.get("OPENAI_API_KEY") else "anthropic")
    key_name = "OPENAI_API_KEY" if provider == "openai" else "ANTHROPIC_API_KEY"
    key = os.environ.get(key_name)
    if not key:
        raise RuntimeError(f"AGENTFORGE_LLM=real needs the {key_name} environment variable (provider: {provider})")
    text = prompt
    if context:
        text += "\n\nContext:\n" + "\n".join(str(c) for c in context)
    if one_of:
        text += "\n\nAnswer with exactly one of these options and nothing else: " + ", ".join(str(o) for o in one_of)
    messages = [{"role": "user", "content": text}]
    if provider == "openai":
        url = "https://api.openai.com/v1/chat/completions"
        headers = {"content-type": "application/json", "authorization": "Bearer " + key}
        payload = {"model": os.environ.get("AGENTFORGE_MODEL", "gpt-4o-mini"), "messages": messages}
    else:
        url = "https://api.anthropic.com/v1/messages"
        headers = {"content-type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01"}
        payload = {"model": os.environ.get("AGENTFORGE_MODEL", "claude-haiku-5-5"), "max_tokens": 512,
                   "messages": messages}
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST", headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        body = json.load(response)
    reply = (body["choices"][0]["message"]["content"] if provider == "openai" else body["content"][0]["text"]).strip()
    if not one_of:
        return reply
    cleaned = reply.strip(" .\n\"").lower()
    for option in one_of:
        if str(option).lower() == cleaned:
            return option
    for option in one_of:
        if str(option).lower() in cleaned:
            return option
    raise RuntimeError(f"the model answered {reply!r}, which is not one of {one_of}")


def llm(prompt, *context, one_of=None):
    """Ask a language model. By default this is a mock that returns placeholder text, so the workflow runs offline."""
    if LLM_MODE == "real":
        return real_llm(prompt, context, one_of)
    if EVAL_SCRIPT is not None:   # a compare test case: the answer the .af file scripts for this candidate
        if one_of:
            return EVAL_SCRIPT if EVAL_SCRIPT in one_of else one_of[0]
        return EVAL_SCRIPT
    if one_of:
        for option in one_of:
            if str(option) == MOCK_CHOICE:
                return option
        return one_of[0]
    return f"[mock LLM answer to: {prompt}]"
'''


# Reads KEY=VALUE lines from a .env file in the folder you run the file from; real environment variables win.
DOTENV_HELPER = r'''
def load_dotenv(path=".env"):
    """Read KEY=VALUE lines from a .env file in the current folder. Variables already set in the environment win."""
    if os.environ.get("AGENTFORGE_NO_DOTENV") or not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_dotenv()
'''

COMMON_HELPERS = r'''
def judge(op, output, expected):
    """Did the output pass? `contains` ignores upper/lower case; `==` must match exactly."""
    if op == "contains":
        return str(expected).lower() in str(output).lower()
    return output == expected
'''

NECESSITY_HELPERS = r'''
def score_workflow(group, skip=()):
    """Run the whole workflow on every test case with the agents in `skip` left out; how many cases passed?"""
    run_app = build_app(skip=skip)
    passed = 0
    for case in group["cases"]:
        start = base_state()
        start.update(deepcopy(case["inputs"]))
        try:
            final = run_app.invoke({**start, **HIDDEN_STATE}, {"recursion_limit": FLOW_LIMIT})
            passed += judge(case["expect"]["op"], final[case["expect"]["field"]], case["expect"]["value"])
        except RuntimeError as problem:   # the workflow itself stopped; any other error (a bad key, no network) is real
            if not str(problem).startswith("no edge condition matched"):
                raise
    return passed


def run_necessity(group):
    """Is every agent needed? Leave each testable agent out in turn (the most expensive first) and keep it out
    when the final output got no worse. Later agents are tested without the ones already left out."""
    skip, tested = set(), []
    baseline = current = score_workflow(group)
    if baseline == 0:   # the workflow as written passes nothing, so removing an agent proves nothing: test nothing
        tested = [{"agent": name, "score_without": None, "needed": True} for name in group["order"]]
        return {"name": group["name"], "total": len(group["cases"]), "baseline": 0, "agents": tested, "after": 0}
    for name in group["order"]:
        without = score_workflow(group, skip | {name})
        needed = without < current
        tested.append({"agent": name, "score_without": without, "needed": needed})
        if not needed:
            skip.add(name)
            current = without
    return {"name": group["name"], "total": len(group["cases"]), "baseline": baseline, "agents": tested, "after": current}


def necessity_json():
    """Print the measurements as JSON for the compiler (python -m compiler file.af --profile real)."""
    print("AGENTFORGE_NECESSITY=" + json.dumps([run_necessity(group) for group in NECESSITIES]))


def print_necessity():
    if LLM_MODE != "real":
        print("mock mode: the model's answers are placeholders, so these scores mean nothing. Run with AGENTFORGE_LLM=real.")
    for group in NECESSITIES:
        report = run_necessity(group)
        print(f"necessity {report['name']}: the workflow as written passes {report['baseline']}/{report['total']} cases")
        if report["baseline"] == 0:
            print("  it fails every case, so nothing was tested: fix that first (agents are only removed on evidence)")
            continue
        for t in report["agents"]:
            print(f"  without '{t['agent']}': {t['score_without']}/{report['total']}  ->  "
                  f"{'needed' if t['needed'] else 'not needed'}")
'''

COMPARE_HELPERS = r'''
def run_compare(group):
    """Run every candidate on every test case and score them. Returns a report dict."""
    global EVAL_SCRIPT
    passed = {name: 0 for name in group["candidates"]}
    cases = []
    for case in group["cases"]:
        results = []
        for name in group["candidates"]:
            start = base_state()
            start.update(deepcopy(case["inputs"]))
            script = case["mock"].get(name)
            stopped = False
            try:
                if group.get("end_to_end"):
                    # run the WHOLE workflow with this candidate in the wired place and judge the final output
                    slot = group["wired"][0]
                    run_app = build_app({slot: in_slot(slot, scripted(EVAL_AGENTS[name], script))})
                    final = run_app.invoke({**start, **HIDDEN_STATE}, {"recursion_limit": FLOW_LIMIT})
                else:
                    EVAL_SCRIPT = script
                    try:
                        final = EVAL_AGENTS[name](start)
                    finally:
                        EVAL_SCRIPT = None
                output = final[case["expect"]["field"]]
            except RuntimeError as problem:   # the workflow itself stopped; any other error (a bad key, no network) is real
                if not str(problem).startswith("no edge condition matched"):
                    raise
                output, stopped = f"[workflow stopped: {problem}]", True
            ok = not stopped and judge(case["expect"]["op"], output, case["expect"]["value"])
            passed[name] += ok
            results.append({"agent": name, "output": output, "passed": ok})
        cases.append({"expect": case["expect"], "inputs": case["inputs"], "results": results})
    top = max(passed.values())
    leaders = [name for name in group["candidates"] if passed[name] == top]
    # on a tie keep the agent that is already in the flow, so nothing is swapped without a reason
    best = next((name for name in leaders if name in group["wired"]), leaders[0])
    return {"name": group["name"], "scope": "end_to_end" if group.get("end_to_end") else "agent",
            "wired": group["wired"], "candidates": group["candidates"], "cases": cases,
            "scores": passed, "total": len(cases), "best": best, "tie": len(leaders) > 1}


def print_comparison(report):
    mode = "real model" if LLM_MODE == "real" else "mock mode: the answers are scripted in the .af file"
    print(f"compare {report['name']} ({mode})")
    if report["scope"] == "end_to_end":
        print("  each case runs the whole workflow with that candidate in place; the final output is judged")
    for number, case in enumerate(report["cases"], start=1):
        exp = case["expect"]
        print(f"  case {number}: expect state.{exp['field']} {exp['op']} {exp['value']!r}")
        for r in case["results"]:
            print(f"    {'PASS' if r['passed'] else 'FAIL'}  {r['agent']}: {r['output']}")
    print("  scores:")
    for name in report["candidates"]:
        tags = (" (in the flow)" if name in report["wired"] else "") + ("  <- best" if name == report["best"] else "")
        print(f"    {name}: {report['scores'][name]}/{report['total']}{tags}")
    if report["tie"]:
        print("  tie: the agent already in the flow is kept")
    for slot in report["wired"]:
        if slot != report["best"] and os.environ.get("AGENTFORGE_USE_BEST") != "1":
            print(f"  '{slot}' is in the flow but '{report['best']}' scored higher; "
                  f"run with AGENTFORGE_USE_BEST=1 to use '{report['best']}'")


def scores_json():
    """Print the scores of every compare block as JSON for the compiler (python -m compiler file.af --profile real)."""
    print("AGENTFORGE_SCORES=" + json.dumps([run_compare(group) for group in COMPARES]))


def scripted(fn, text):
    """Run `fn` with the mock LLM answering `text` (mock mode only), so just this agent is scripted, not the others."""
    def node(state):
        global EVAL_SCRIPT
        EVAL_SCRIPT = text
        try:
            return fn(state)
        finally:
            EVAL_SCRIPT = None
    return node


def guarded(group_name, slot, plain):
    """The wired agent with the compare block's `ensure` check. Only a real model can give a bad answer, so the
    check runs with AGENTFORGE_LLM=real. If the answer fails it, the other candidates are tried in order, and if
    none passes the run stops with an error instead of passing a bad value to the agents after it."""
    check = ENSURES[group_name]
    order = [slot] + [c for c in BY_NAME[group_name]["candidates"] if c != slot]

    def node(state):
        if LLM_MODE != "real":
            return plain(state)
        failed = []
        for name in order:
            s = plain(state) if name == slot else in_slot(slot, EVAL_AGENTS[name])(state)
            if check(s):
                if failed:
                    print(f"ensure failed for {', '.join(repr(f) for f in failed)}; used '{name}' instead", file=sys.stderr)
                return s
            failed.append(name)
        raise RuntimeError(f"ensure failed for every candidate of compare '{group_name}' ({', '.join(order)})")
    return node


def slot_node(group, slot, fn):
    """`fn` as the code of `slot`, with the run-time check if the compare block has one."""
    return guarded(group["name"], slot, fn) if group["name"] in ENSURES else fn


def in_slot(slot, fn):
    """Return a node function that runs `fn` where the agent `slot` stood in the flow."""
    def node(state):
        s = fn(state)
        router = SLOT_ROUTERS.get(slot)
        if router:
            s["_next_" + slot] = router(s)
        return s
    return node
'''


def literal_py(lit: ast.Literal) -> str:
    return py_str(lit.value) if lit.value_type == "string" else repr(lit.value)


class CodeGenerator:
    def __init__(self, graph: IRGraph, symbols: SymbolTable, source_name: str = "workflow.af"):
        self.graph = graph
        self.symbols = symbols
        # the file name ends up inside docstrings, so keep quotes and backslashes out of it
        self.source_name = source_name.replace("\\", "/").replace("\"", "'")
        self.lines: list[str] = []
        self.has_compares = bool(symbols.compares)
        self.has_necessity = bool(symbols.necessities)
        self.dynamic = self.has_compares or self.has_necessity   # build_app is a function: it can swap or skip agents
        # compare blocks with an `ensure` check and one wired candidate: wired agent -> block
        wired_now = {n for n, node in graph.nodes.items() if node.kind == "agent"}
        self.ensures = {}
        for info in symbols.compares.values():
            wired = [c for c in info.candidates if c in wired_now]
            if info.decl.ensure is not None and len(wired) == 1:
                self.ensures[wired[0]] = info
        # give every bounded edge its own loop counter
        self.counters = {id(e): f"_count_{i}" for i, e in enumerate(
            e for e in graph.edges if e.max_iterations is not None)}

    def emit(self, text: str = "", indent: int = 0):
        self.lines.append("    " * indent + text if text else "")

    # ---------- helpers about the graph ----------

    def branching_nodes(self) -> list:
        """Nodes whose next step depends on a condition."""
        return [n for n in self.graph.nodes
                if any(e.condition is not None for e in self.graph.out_edges(n))]

    @staticmethod
    def graph_name(node_id: str) -> str:
        return {"start": "START", "end": "END"}.get(node_id, py_str(node_id))

    # ---------- expressions ----------

    def expr(self, e) -> str:
        if isinstance(e, ast.Literal):
            return literal_py(e)
        if isinstance(e, ast.StateRef):
            return f's["{e.field}"]'
        if isinstance(e, ast.Call):
            return f"tool_{e.name}({', '.join(self.expr(a) for a in e.args)})"
        if isinstance(e, ast.LlmCall):
            parts = [py_str(e.prompt)] + [self.expr(a) for a in e.args]
            if e.one_of is not None:
                parts.append("one_of=[" + ", ".join(literal_py(o) for o in e.one_of) + "]")
            return f"llm({', '.join(parts)})"
        if isinstance(e, ast.UnaryOp):
            return f"not {self.wrap(e.operand, 'not')}"
        if isinstance(e, ast.BinaryOp):
            return f"{self.wrap(e.left, e.op)} {e.op} {self.wrap(e.right, e.op)}"
        raise ValueError(f"unknown expression {e!r}")

    def wrap(self, child, parent_op) -> str:
        """Parenthesize a child expression only when Python would group it differently."""
        text = self.expr(child)
        parent = PRECEDENCE.get(parent_op, 4)
        if isinstance(child, ast.BinaryOp):
            prec = PRECEDENCE.get(child.op, 4)
            if prec < parent or (prec == 4 and parent == 4):
                return f"({text})"
        if isinstance(child, ast.UnaryOp) and parent == 4:
            return f"({text})"
        return text

    # ---------- statements ----------

    def statements(self, body: list, indent: int):
        if not body:
            self.emit("pass", indent)
        for stmt in body:
            if isinstance(stmt, ast.Assign):
                self.emit(f's["{stmt.target.field}"] = {self.expr(stmt.value)}', indent)
            elif isinstance(stmt, ast.If):
                self.emit(f"if {self.expr(stmt.condition)}:", indent)
                self.statements(stmt.then_body, indent + 1)
                if stmt.else_body:
                    self.emit("else:", indent)
                    self.statements(stmt.else_body, indent + 1)

    # ---------- file sections ----------

    def generate(self) -> str:
        self.header()
        self.state_class()
        self.llm_helper()
        self.tools()
        self.agents()
        self.routers()
        self.wiring()
        if self.dynamic:
            self.harness_common()
        if self.has_compares:
            self.compare_section()
        if self.has_necessity:
            self.necessity_section()
        self.main_block()
        return "\n".join(self.lines) + "\n"

    def header(self):
        self.emit(f'"""Generated by AgentForge from {self.source_name}. Do not edit by hand; edit the .af file and recompile.')
        self.emit()
        self.emit("Run it with:  python workflow.py")
        self.emit("Tools and the LLM run in MOCK mode by default, so no API keys are needed.")
        self.emit("Set AGENTFORGE_CHOICE to choose which one_of option the mock LLM returns (default: the first).")
        self.emit("Set AGENTFORGE_LLM=real and OPENAI_API_KEY (or ANTHROPIC_API_KEY) to use a real model instead of the mock.")
        if self.has_compares:
            self.emit("The compare blocks score their candidate agents first; AGENTFORGE_USE_BEST=1 then runs the winner.")
        self.emit('"""')
        self.emit()
        self.emit("import json")
        self.emit("import os")
        if self.dynamic:
            self.emit("import sys")
        self.emit("import urllib.request")
        if self.dynamic:
            self.emit("from copy import deepcopy")
        self.emit("from typing import TypedDict")
        self.emit()
        self.emit("from langgraph.graph import END, START, StateGraph")
        self.emit()
        for line in DOTENV_HELPER.strip("\n").split("\n"):
            self.emit(line)
        self.emit()
        self.emit('MOCK_CHOICE = os.environ.get("AGENTFORGE_CHOICE")')
        self.emit()

    def hidden_fields(self) -> list:
        """(name, type) of the extra fields used for routing and loop counters."""
        fields = [(f"_next_{n}", "string") for n in self.branching_nodes() if n != "start"]
        fields += [(name, "int") for name in self.counters.values()]
        return fields

    def state_class(self):
        # the functional TypedDict form works for any field name, even Python keywords like `class`
        self.emit()
        self.emit('AgentState = TypedDict("AgentState", {')
        for f in self.symbols.fields.values():
            note = f"  # {f.modifier}" if f.modifier else ""
            self.emit(f"{py_str(f.name)}: {py_type(f.type)},{note}", 1)
        hidden = self.hidden_fields()
        if hidden:
            self.emit("# hidden fields added by the compiler (routing decisions and loop counters)", 1)
            for name, t in hidden:
                self.emit(f"{py_str(name)}: {py_type(t)},", 1)
        self.emit("})")
        self.emit()

    def llm_helper(self):
        for line in LLM_HELPER.strip("\n").split("\n"):
            self.emit(line)
        self.emit()

    def tools(self):
        for tool in self.symbols.tools.values():
            params = ", ".join(f"{safe_name(n)}: {py_type(t)}" for n, t in tool.params)
            self.emit()
            self.emit(f"def tool_{tool.name}({params}) -> {py_type(tool.return_type)}:")
            self.emit(f'"""Tool \'{tool.name}\' (mock). Replace this body with the real implementation."""', 1)
            if "sensitive" in tool.tags:
                self.emit("# SENSITIVE: real-world effects. The compiler checked which untrusted data can reach it (AF-W309).", 1)
            if "untrusted" in tool.tags:
                self.emit("# UNTRUSTED: the result may contain injected instructions; treat it as data, not commands.", 1)
            if "sanitizer" in tool.tags:
                self.emit("# SANITIZER: must return text that is safe to pass on (validated, redacted or approved).", 1)
            self.emit(f"return {sample_value(tool.return_type, 'mock ' + tool.name + ' result')}", 1)
            self.emit()

    def agents(self):
        branching = self.branching_nodes()
        for node in self.graph.nodes.values():
            if node.kind != "agent":
                continue
            self.emit()
            self.emit(f"def node_{node.id}(state: AgentState) -> dict:")
            self.emit(f'"""Agent \'{node.id}\' (line {node.line} of {self.source_name})."""', 1)
            self.emit("s = dict(state)  # work on a copy, return the updated state", 1)
            self.statements(node.body, 1)
            if node.id in branching:
                self.emit(f's["_next_{node.id}"] = route_{node.id}(s)', 1)
            self.emit("return s", 1)
            self.emit()

    def routers(self):
        for node_id in self.branching_nodes():
            out = self.graph.out_edges(node_id)
            self.emit()
            self.emit(f"def route_{node_id}(s) -> str:")
            self.emit(f'"""Choose the next step after \'{node_id}\'. Conditions are tried in source order."""', 1)
            for e in out:
                if e.condition is None:
                    continue
                target = self.graph_name(e.target)
                cond = self.expr(e.condition)
                if e.max_iterations is not None:
                    counter = self.counters[id(e)]
                    self.emit(f"# loop edge: taken at most {e.max_iterations} times", 1)
                    self.emit(f'if ({cond}) and s["{counter}"] < {e.max_iterations}:', 1)
                    self.emit(f's["{counter}"] += 1', 2)
                else:
                    self.emit(f"if {cond}:", 1)
                self.emit(f"return {target}", 2)
            default = next((e for e in out if e.is_default), None)
            if default:
                self.emit(f"return {self.graph_name(default.target)}", 1)
            else:
                self.emit(f'raise RuntimeError("no edge condition matched after \'{node_id}\' and there is no else edge")', 1)
            self.emit()

    def wiring(self):
        branching = self.branching_nodes()
        ind = 1 if self.dynamic else 0   # with compare/necessity blocks the wiring is a function, so agents can be swapped or skipped
        skipping = self.has_necessity
        self.emit()
        self.emit("# ---------- graph wiring ----------")
        if skipping:
            nexts = {n: self.graph.out_edges(n)[0].target for n, node in self.graph.nodes.items()
                     if node.kind == "agent" and n not in branching and self.graph.out_edges(n)}
            self.emit("# the agent after each agent that has one plain edge: build_app skips an agent by jumping to it")
            self.emit("NEXT_OF = {" + ", ".join(f"{py_str(a)}: {py_str(b)}" for a, b in nexts.items()) + "}")
            self.emit()
        if self.dynamic:
            self.emit("def build_app(swap=None, skip=()):")
            note = "; `skip` names agents to leave out (their edges go to the agent that followed them)" if skipping else ""
            self.emit(f'"""Build the graph. `swap` maps an agent name to the function that should run in its place{note}."""', 1)
            self.emit("swap = swap or {}", 1)
            if skipping:
                self.emit("skip = set(skip)", 1)
                self.emit("def jump(name):", 1)
                self.emit("while name in skip:", 2)
                self.emit("name = NEXT_OF[name]", 3)
                self.emit('return END if name == "end" else name', 2)
        self.emit("graph = StateGraph(AgentState)", ind)

        def target(t):
            return f'jump("{t}")' if skipping and self.graph.nodes[t].kind == "agent" else self.graph_name(t)

        for node in self.graph.nodes.values():
            if node.kind == "agent":
                fn = f'swap.get("{node.id}", node_{node.id})' if self.dynamic else f"node_{node.id}"
                if node.id in self.ensures:
                    fn = (f'swap.get("{node.id}", guarded({py_str(self.ensures[node.id].name)}, '
                          f'"{node.id}", node_{node.id}))')
                if skipping:
                    self.emit(f'if "{node.id}" not in skip:', ind)
                    self.emit(f'graph.add_node("{node.id}", {fn})', ind + 1)
                else:
                    self.emit(f'graph.add_node("{node.id}", {fn})', ind)
        for node_id in self.graph.nodes:
            out = self.graph.out_edges(node_id)
            if not out:
                continue
            level = ind
            if skipping and self.graph.nodes[node_id].kind == "agent":
                self.emit(f'if "{node_id}" not in skip:', ind)
                level = ind + 1
            if node_id in branching:
                targets = dict.fromkeys(e.target for e in out)
                mapping = ", ".join(f"{self.graph_name(t)}: {target(t)}" for t in targets)
                if node_id == "start":
                    chooser = "lambda state: route_start(dict(state))"
                else:
                    chooser = f'lambda s: s["_next_{node_id}"]'
                self.emit(f"graph.add_conditional_edges({self.graph_name(node_id)}, {chooser}, {{{mapping}}})", level)
            else:
                # one plain edge (or a lone else edge)
                self.emit(f"graph.add_edge({self.graph_name(node_id)}, {target(out[0].target)})", level)
        if self.dynamic:
            self.emit("return graph.compile()", 1)
            self.emit()
            if self.has_compares:
                self.emit("# (`app = build_app()` comes after the compare section below, which defines what build_app uses)")
            else:
                self.emit()
                self.emit("app = build_app()")
        else:
            self.emit("app = graph.compile()")
        self.emit()

    # ---------- compare blocks: candidates, test cases, scoring ----------

    def initial_items(self) -> list:
        """(field, python value, note) for the state a run starts from; shared by __main__ and the compare harness."""
        items = []
        for f in self.symbols.fields.values():
            if f.modifier == "input":
                value, note = sample_value(f.type, "sample " + f.name), "  # input: change this"
            elif f.has_default:
                value, note = self.default_py(f.default), "  # default from the .af file"
            else:
                value, note = empty_value(f.type), ""
            items.append((f.name, value, note))
        return items

    def compare_section(self):
        wired_nodes = {n for n, node in self.graph.nodes.items() if node.kind == "agent"}
        groups, candidates = [], []
        for info in self.symbols.compares.values():
            wired = [c for c in info.candidates if c in wired_nodes]
            groups.append({
                "name": info.name, "candidates": info.candidates, "wired": wired,
                "end_to_end": info.decl.end_to_end and len(wired) == 1,
                "cases": [{
                    "inputs": {st.target.field: self.plain(st.value) for st in case.inputs},
                    "expect": {"field": case.expects[0].target.field, "op": case.expects[0].op,
                               "value": case.expects[0].value.value},
                    "mock": {m.agent: m.text for m in case.mocks},
                } for case in info.decl.cases],
            })
            candidates += [c for c in info.candidates if c not in candidates]

        self.emit()
        self.emit("# ---------- compare: which candidate agent gives the better output? ----------")
        self.emit("# Each candidate runs as written in the .af file on every test case. In mock mode the answer of each")
        self.emit("# candidate is the one scripted in the case (`mock agent \"...\"`); with AGENTFORGE_LLM=real a real model answers.")
        for name in candidates:
            self.emit()
            self.emit(f"def eval_{name}(state: AgentState) -> dict:")
            self.emit(f'"""Candidate \'{name}\' exactly as written (line {self.symbols.agents[name].decl.line} of {self.source_name})."""', 1)
            self.emit("s = dict(state)", 1)
            self.statements(self.symbols.agents[name].decl.body, 1)
            self.emit("return s", 1)
            self.emit()
        self.emit()
        self.emit("EVAL_AGENTS = {" + ", ".join(f"{py_str(n)}: eval_{n}" for n in candidates) + "}")
        self.emit("# agents that branch: after a swap, the winner must also choose the next step")
        routers = [n for n in self.branching_nodes() if n in candidates]
        self.emit("SLOT_ROUTERS = {" + ", ".join(f"{py_str(n)}: route_{n}" for n in routers) + "}")
        self.emit("COMPARES = " + pprint.pformat(groups, width=110, sort_dicts=False))
        self.emit('BY_NAME = {group["name"]: group for group in COMPARES}')
        self.emit("# run-time checks (`ensure` in a compare block); they apply to the real model's answers")
        self.emit("ENSURES = {")
        for info in self.ensures.values():
            self.emit(f"{py_str(info.name)}: lambda s: {self.expr(info.decl.ensure)},", 1)
        self.emit("}")
        self.emit()
        self.emit()
        for line in COMPARE_HELPERS.strip("\n").split("\n"):
            self.emit(line)
        self.emit()
        self.emit()
        self.emit("app = build_app()")
        self.emit()

    def harness_common(self):
        """What both kinds of test run share: the starting state, a step limit and the judge."""
        self.emit()
        self.emit("# ---------- test harness (compare and necessity blocks) ----------")
        self.emit("# the starting value of the hidden routing fields, and the step limit for a whole-workflow test run")
        self.emit("HIDDEN_STATE = {" + ", ".join(f"{py_str(n)}: {empty_value(t)}" for n, t in self.hidden_fields()) + "}")
        self.emit(f"FLOW_LIMIT = {self.recursion_limit()}")
        self.emit()
        self.emit()
        self.emit("def base_state() -> dict:")
        self.emit('"""The state every test case starts from (before the case sets its own inputs)."""', 1)
        self.emit("return {", 1)
        for name, value, note in self.initial_items():
            self.emit(f"{py_str(name)}: {value},{note}", 2)
        self.emit("}", 1)
        self.emit()
        for line in COMMON_HELPERS.strip("\n").split("\n"):
            self.emit(line)
        self.emit()

    def necessity_section(self):
        rows = classify(self.graph, self.symbols)
        order = agents_to_test(rows)
        groups = [{
            "name": info.name, "order": order,
            "cases": [{
                "inputs": {st.target.field: self.plain(st.value) for st in case.inputs},
                "expect": {"field": case.expects[0].target.field, "op": case.expects[0].op,
                           "value": case.expects[0].value.value},
            } for case in info.decl.cases],
        } for info in self.symbols.necessities.values()]
        self.emit()
        self.emit("# ---------- necessity: which agents does the workflow need? ----------")
        self.emit("# Run `python workflow.py --necessity` (AGENTFORGE_LLM=real) to leave each testable agent out in turn and")
        self.emit("# see whether the final output gets worse. The compiler does this for you with --profile real.")
        self.emit("NECESSITIES = " + pprint.pformat(groups, width=110, sort_dicts=False))
        self.emit()
        for line in NECESSITY_HELPERS.strip("\n").split("\n"):
            self.emit(line)
        self.emit()

    @staticmethod
    def plain(value):
        """A Literal (or list of Literals) as a plain Python value."""
        return [v.value for v in value] if isinstance(value, list) else value.value

    def recursion_limit(self) -> int:
        bounds = sum(e.max_iterations for e in self.graph.edges if e.max_iterations is not None)
        return 50 + 2 * len(self.graph.nodes) * (bounds + 1)

    def main_block(self):
        limit = self.recursion_limit()
        self.emit()
        self.emit('if __name__ == "__main__":')
        if self.has_necessity:
            self.emit('if "--necessity-json" in sys.argv:', 1)
            self.emit("necessity_json()", 2)
            self.emit("raise SystemExit(0)", 2)
            self.emit('if "--necessity" in sys.argv:', 1)
            self.emit("print_necessity()", 2)
            self.emit("raise SystemExit(0)", 2)
        if self.has_compares:
            self.emit("if \"--scores-json\" in sys.argv:", 1)
            self.emit("scores_json()", 2)
            self.emit("raise SystemExit(0)", 2)
            self.emit("# score the candidate agents of every compare block (the winner can replace the one in the flow)", 1)
            self.emit("swap, swapped = {}, {}", 1)
            self.emit("for group in COMPARES:", 1)
            self.emit("report = run_compare(group)", 2)
            self.emit("print_comparison(report)", 2)
            self.emit("slot = group[\"wired\"][0] if len(group[\"wired\"]) == 1 else None", 2)
            self.emit("if slot and report[\"best\"] != slot and os.environ.get(\"AGENTFORGE_USE_BEST\") == \"1\":", 2)
            self.emit("swap[slot], swapped[slot] = slot_node(group, slot, in_slot(slot, EVAL_AGENTS[report[\"best\"]])), report[\"best\"]", 3)
            self.emit("print(f\"  using '{report['best']}' in place of '{slot}' for this run\")", 3)
            self.emit("if swap:", 1)
            self.emit("app = build_app(swap)", 2)
            self.emit("print()", 1)
        self.emit("# every field starts with a value; the compiler already checked that agents only", 1)
        self.emit("# read fields that are written on every path before them", 1)
        self.emit("initial_state = {", 1)
        for name, value, note in self.initial_items():
            self.emit(f'"{name}": {value},{note}', 2)
        for name, t in self.hidden_fields():
            self.emit(f'"{name}": {empty_value(t)},', 2)
        self.emit("}", 1)
        self.emit("# run the graph step by step so we can print the path it takes", 1)
        self.emit("final_state, visited = initial_state, []", 1)
        self.emit(f'for step in app.stream(initial_state, {{"recursion_limit": {limit}}}, stream_mode="updates"):', 1)
        self.emit("for node_name, update in step.items():", 2)
        self.emit("visited.append(node_name)", 3)
        self.emit("final_state = {**final_state, **(update or {})}  # update is None when a node changes nothing", 3)
        self.emit('print("path:", " -> ".join(["start"] + visited + ["end"]))', 1)
        if self.has_compares:
            self.emit("for slot, winner in swapped.items():", 1)
            self.emit("print(f\"  (node '{slot}' ran the code of '{winner}')\")", 2)
        outputs = [f.name for f in self.symbols.fields.values() if f.modifier == "output"]
        shown = outputs or [f.name for f in self.symbols.fields.values()]
        for name in shown:
            self.emit(f'print("{name}:", final_state["{name}"])', 1)

    @staticmethod
    def default_py(default) -> str:
        if isinstance(default, list):
            return "[" + ", ".join(literal_py(x) for x in default) + "]"
        return literal_py(default)


def generate(graph: IRGraph, symbols: SymbolTable, source_name="workflow.af") -> str:
    return CodeGenerator(graph, symbols, source_name).generate()
