"""Fuzz and differential tests (fixed random seeds, so every run is reproducible).

1. Mutation fuzzing: thousands of randomly corrupted versions of the example programs.
   Whatever the input, the compiler must never crash, every diagnostic must be well formed,
   and every program that compiles must produce valid Python.

2. Differential testing of the verifier: hundreds of random (but well typed) workflows.
   Their expected verification results are computed by a deliberately different, brute
   force method (enumerate every path, compute reachability by plain DFS from every node)
   and compared with the compiler's fixpoint / Tarjan / BFS based passes.
"""

import random
import re

import pytest

from compiler.lexer import tokenize
from compiler.pipeline import PHASES, compile_source
from compiler.tokens import KEYWORDS as KEYWORD_TABLE

from helpers import EXAMPLES_DIR

CONTRACT = (EXAMPLES_DIR.parent / "docs" / "contract.md").read_text(encoding="utf-8")
DOCUMENTED_CODES = set(re.findall(r"\| (AF-[EW]\d{3}) \|", CONTRACT))
SEEDS_SOURCES = [p.read_text(encoding="utf-8") for p in sorted(EXAMPLES_DIR.glob("*/*.af"))]
KEYWORDS = set(KEYWORD_TABLE)


# =============================================================== 1. mutation fuzzing

def mutate(source: str, rng: random.Random) -> str:
    """Apply 1-4 random edits: at the character level or the token level."""
    for _ in range(rng.randint(1, 4)):
        choice = rng.randrange(10)
        lines = source.split("\n")
        if choice >= 7:                                 # edits that usually keep the syntax valid
            i = rng.randrange(len(lines))
            if choice == 7:                             # delete a whole line
                del lines[i]
            elif choice == 8:                           # duplicate a line somewhere else
                lines.insert(rng.randrange(len(lines) + 1), lines[i])
            else:                                       # swap one name for another name of the program
                names = sorted(set(re.findall(r"\b[a-z_][a-z0-9_]*\b", source)) - KEYWORDS)
                if names:
                    old, new = rng.choice(names), rng.choice(names)
                    lines[i] = re.sub(rf"\b{old}\b", new, lines[i])
            source = "\n".join(lines)
        elif choice == 0 and source:                    # delete a span of characters
            i = rng.randrange(len(source))
            source = source[:i] + source[i + rng.randint(1, 8):]
        elif choice == 1:                               # insert a random character
            i = rng.randrange(len(source) + 1)
            source = source[:i] + rng.choice('{}()[],:.=<>-!"#@$ \n\tab_09²é') + source[i:]
        elif choice == 2:                               # truncate
            source = source[: rng.randrange(len(source) + 1)]
        else:                                           # token level edits
            tokens, _ = tokenize(source)
            words = [source_text(t) for t in tokens[:-1]] or ["agent"]
            i = rng.randrange(len(words))
            if choice == 3:
                del words[i]
            elif choice == 4:
                words.insert(i, rng.choice(words + ["state", "->", "-[", "]", "else", "if", "{", "}", "max_iterations"]))
            elif choice == 5:
                j = rng.randrange(len(words))
                words[i], words[j] = words[j], words[i]
            else:
                words[i] = rng.choice(words)
            source = " ".join(words)
    return source


def source_text(token) -> str:
    """The token as it was written (strings get their quotes back)."""
    if token.type.name == "STRING":
        return '"' + token.lexeme.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    return token.lexeme


def check_invariants(source: str):
    result = compile_source(source)   # 1. must not raise

    # 2. every diagnostic is well formed and documented
    for d in result["diagnostics"]:
        assert d["severity"] in ("error", "warning")
        assert d["code"] in DOCUMENTED_CODES, d["code"]
        assert d["phase"] in PHASES
        assert d["line"] >= 1 and d["col"] >= 1
        assert d["message"]
        assert (d["code"][3] == "E") == (d["severity"] == "error")

    # 3. ok exactly when there is no error
    errors = [d for d in result["diagnostics"] if d["severity"] == "error"]
    assert result["ok"] == (not errors)

    # 4. phase statuses: after the first failed phase, everything is skipped
    statuses = [result["phases"][p]["status"] for p in PHASES]
    if "error" in statuses:
        first = statuses.index("error")
        later = [s for p, s in zip(PHASES[first + 1:], statuses[first + 1:])]
        assert all(s == "skipped" for s in later), statuses
    else:
        assert all(s == "ok" for s in statuses) or statuses[0] == "ok"

    # 5. if it compiled, the generated program is valid Python
    if result["ok"]:
        compile(result["phases"]["codegen"]["code"], "workflow.py", "exec")
    return result


@pytest.mark.parametrize("seed", range(10))
def test_mutation_fuzzing(seed):
    rng = random.Random(seed)
    compiled = 0
    for _ in range(250):
        source = mutate(rng.choice(SEEDS_SOURCES), rng)
        compiled += check_invariants(source)["ok"]
    # sanity: the fuzzer produces a mix of broken and still-valid programs
    assert 0 < compiled < 250


def test_random_garbage_never_crashes():
    rng = random.Random(1234)
    alphabet = list('{}()[],:.=<>-!"# \n') + ["state", "agent", "flow", "tool", "->", "-[", "]->", "if", "else",
                                               "start", "end", "llm", "one_of", "x", "y", "1", '"s"', "true"]
    for _ in range(1500):
        check_invariants(" ".join(rng.choice(alphabet) for _ in range(rng.randint(0, 60))))


# =============================================================== 2. differential testing

def random_workflow(rng: random.Random):
    """Build a random, well typed workflow.

    Returns (source, model) where the model describes the graph independently of the compiler.
    All fields are strings; the input field q is the only thing conditions test, so conditions
    are always safe and only the agents' reads/writes matter for state completeness.
    """
    n_agents = rng.randint(1, 6)
    agents = [f"a{i}" for i in range(n_agents)]
    fields = [f"f{i}" for i in range(rng.randint(1, 5))]
    defaults = {f for f in fields if rng.random() < 0.25}
    outputs = {f for f in fields if rng.random() < 0.3}

    must, reads, body = {}, {}, {}
    for a in agents:
        r = [f for f in fields if rng.random() < 0.3]
        lines = []
        if r:   # read first, so these are reads of values from earlier agents
            lines.append("if (" + " or ".join(f'state.{f} == "x"' for f in r) + ") { }")
        must_writes = set()
        for f in fields:
            roll = rng.random()
            if roll < 0.3:      # plain write: definite
                lines.append(f'state.{f} = llm("w", state.q)')
                must_writes.add(f)
            elif roll < 0.4:    # write in one branch only: not definite
                lines.append(f'if (state.q == "z") {{ state.{f} = "v" }}')
            elif roll < 0.5:    # write in both branches: definite
                lines.append(f'if (state.q == "z") {{ state.{f} = "v" }} else {{ state.{f} = "w" }}')
                must_writes.add(f)
        must[a], reads[a], body[a] = must_writes, r, lines

    # edges: every node gets one plain edge, or conditional edges (+ maybe an else), or nothing
    edges = []   # (source, target, conditional, bounded, is_else)
    targets = agents + ["end"]
    for node in ["start"] + agents:
        roll = rng.random()
        if roll < 0.08 and node != "start":
            continue                                          # dead end
        if roll < 0.55:
            edges.append((node, rng.choice(targets), False, False, False))
        else:
            for k in range(rng.randint(1, 2)):
                edges.append((node, rng.choice(targets), True, rng.random() < 0.3, False))
            if rng.random() < 0.75:
                edges.append((node, rng.choice(targets), False, False, True))

    lines = ["state S {", "  input q: string"]
    for f in fields:
        lines.append(f"  {'output ' if f in outputs else ''}{f}: string{' = \"\"' if f in defaults else ''}")
    lines.append("}")
    for a in agents:
        lines.append(f"agent {a} {{")
        lines += ["  " + line for line in body[a]]
        lines.append("}")
    lines.append("flow f {")
    for k, (s, t, cond, bounded, is_else) in enumerate(edges):
        if is_else:
            lines.append(f"  {s} -[else]-> {t}")
        elif cond:
            bound = f", max_iterations {rng.randint(1, 4)}" if bounded else ""
            lines.append(f'  {s} -[state.q == "v{k}"{bound}]-> {t}')
        else:
            lines.append(f"  {s} -> {t}")
    lines.append("}")

    model = {"agents": agents, "fields": fields, "initial": defaults | {"q"}, "outputs": outputs,
             "must": must, "reads": reads, "edges": edges}
    return "\n".join(lines), model


def brute_force_expectations(model):
    """Expected verification results, computed WITHOUT the compiler's algorithms."""
    nodes = ["start"] + model["agents"] + ["end"]
    succ = {n: [t for s, t, *_ in model["edges"] if s == n] for n in nodes}

    def reach_from(n, include_self):
        seen, stack = set(), list(succ[n])
        while stack:
            m = stack.pop()
            if m not in seen:
                seen.add(m)
                stack.extend(succ[m])
        return seen | ({n} if include_self else set())

    reach0 = {n: reach_from(n, True) for n in nodes}     # reachable in 0+ steps
    reach1 = {n: reach_from(n, False) for n in nodes}    # reachable in 1+ steps
    reachable = reach0["start"]

    def all_simple_paths(target):
        paths, stack = [], [["start"]]
        while stack:
            path = stack.pop()
            if path[-1] == target:
                paths.append(path)
                continue
            for m in succ[path[-1]]:
                if m not in path:
                    stack.append(path + [m])
        return paths

    def written_on_every_path(target):
        # writes only ever add, so a looping path writes at least what its loop-free version writes:
        # the intersection over simple paths equals the intersection over all paths
        result = None
        for path in all_simple_paths(target):
            written = set(model["initial"])
            for node in path[:-1]:
                written |= model["must"].get(node, set())
            result = written if result is None else result & written
        return result

    expected = {"W301": {a for a in model["agents"] if a not in reachable}, "E302": set(), "E304": set()}
    for a in model["agents"]:
        if a in reachable:
            have = written_on_every_path(a)
            expected["E302"] |= {(a, f) for f in model["reads"][a] if f not in have}
    if "end" in reachable:
        have = written_on_every_path("end")
        expected["E304"] = {f for f in model["outputs"] if f not in have}

    # cycles: mutual reachability instead of Tarjan
    bad_sccs = set()
    for n in reachable:
        if n in reach1[n]:
            scc = frozenset({m for m in reach1[n] if n in reach1[m]})
            inner = [e for e in model["edges"] if e[0] in scc and e[1] in scc]
            leaves = any(e[0] in scc and e[1] not in scc for e in model["edges"])
            if not any(e[3] for e in inner) and not leaves:
                bad_sccs.add(scc)
    expected["E303"] = len(bad_sccs)
    stuck = set().union(*bad_sccs) if bad_sccs else set()
    skip = {n for n in nodes if reach0[n] & stuck}
    expected["E305"] = {n for n in reachable if "end" not in reach0[n] and n not in skip}
    expected["W306"] = {n for n in reachable
                        if any(e[2] for e in model["edges"] if e[0] == n)
                        and not any(e[4] for e in model["edges"] if e[0] == n)}
    return expected


def actual_results(result):
    diags = result["diagnostics"]

    def matching(code, pattern):
        return [re.search(pattern, d["message"]) for d in diags if d["code"] == code]

    return {
        "W301": {m.group(1) for m in matching("AF-W301", r"agent '(\w+)'")},
        "E302": {(m.group(1), m.group(2)) for m in matching("AF-E302", r"agent '(\w+)' reads state\.(\w+)")},
        "E304": {m.group(1) for m in matching("AF-E304", r"output state\.(\w+)")},
        "E303": sum(d["code"] == "AF-E303" for d in diags),
        "E305": {m.group(1) or "start" for m in matching("AF-E305", r"^(?:agent '(\w+)'|start)")},
        "W306": {m.group(1) for m in matching("AF-W306", r"'(\w+)'")},
    }


def check_witness_paths(result, model):
    """Every AF-E302 / AF-E304 witness path must be a real path on which nobody writes the field."""
    edge_set = {(s, t) for s, t, *_ in model["edges"]}
    for d in result["diagnostics"]:
        if d["code"] not in ("AF-E302", "AF-E304"):
            continue
        path = d["path"]
        field = re.search(r"state\.(\w+)", d["message"]).group(1)
        assert path[0] == "start"
        assert all((a, b) in edge_set for a, b in zip(path, path[1:])), path
        assert field not in model["initial"]
        assert not any(field in model["must"].get(n, set()) for n in path[:-1]), (field, path)


@pytest.mark.parametrize("seed", range(8))
def test_verifier_matches_brute_force(seed):
    rng = random.Random(1000 + seed)
    for _ in range(60):
        source, model = random_workflow(rng)
        result = compile_source(source)
        assert result["phases"]["semantic"]["status"] == "ok", (source, result["diagnostics"])
        if result["phases"]["verify"]["status"] == "skipped":
            continue
        expected = brute_force_expectations(model)
        actual = actual_results(result)
        assert actual == expected, f"\n{source}\nexpected {expected}\nactual   {actual}"
        check_witness_paths(result, model)
        if result["ok"]:
            compile(result["phases"]["codegen"]["code"], "workflow.py", "exec")
