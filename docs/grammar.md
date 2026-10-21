# AgentForge grammar (EBNF)

This is the grammar the hand written recursive descent parser follows.
Each rule below is one function in `backend/compiler/parser.py`.
The grammar is LL(1): at every choice point the next token alone decides which branch to take.

Notation: `{ x }` = zero or more, `[ x ]` = optional, `|` = choice, `"x"` = keyword or symbol.

```ebnf
program      = { declaration } EOF ;
declaration  = state_decl | tool_decl | agent_decl | flow_decl ;

(* state schema *)
state_decl   = "state" IDENT "{" { field_decl } "}" ;
field_decl   = [ "input" [ "untrusted" ] | "output" ] IDENT ":" type [ "=" default ] ;
default      = literal | "[" [ literal { "," literal } ] "]" ;
type         = "string" | "int" | "bool" | "list" "[" type "]" ;

(* tools *)
tool_decl    = "tool" IDENT "(" [ param { "," param } ] ")" "->" type { tool_tag } ;
tool_tag     = "untrusted" | "sensitive" | "sanitizer" ;
param        = IDENT ":" type ;

(* agents *)
agent_decl   = "agent" IDENT block ;
block        = "{" { statement } "}" ;
statement    = assign | if_stmt ;
assign       = state_ref "=" expr ;
if_stmt      = "if" "(" expr ")" block [ "else" ( block | if_stmt ) ] ;

(* flow *)
flow_decl    = "flow" IDENT "{" { edge_chain } "}" ;
edge_chain   = node arrow node { arrow node } ;
node         = IDENT | "start" | "end" ;
arrow        = "->"
             | "-[" edge_label "]" "->" ;
edge_label   = "else"
             | expr [ "," "max_iterations" INT ] ;

(* expressions, lowest precedence first: or < and < not < comparison *)
expr         = or_expr ;
or_expr      = and_expr { "or" and_expr } ;
and_expr     = not_expr { "and" not_expr } ;
not_expr     = "not" not_expr | comparison ;
comparison   = primary [ comp_op primary ] ;
comp_op      = "==" | "!=" | "<" | ">" | "<=" | ">=" ;
primary      = literal
             | state_ref
             | tool_call
             | llm_call
             | "(" expr ")" ;
state_ref    = "state" "." IDENT ;
tool_call    = IDENT "(" [ expr { "," expr } ] ")" ;
llm_call     = "llm" "(" STRING { "," expr } ")" [ "one_of" "[" literal { "," literal } "]" ] ;
literal      = STRING | INT | "true" | "false" ;
```

## Tokens

| Token | Pattern |
|---|---|
| IDENT | ASCII letter or `_`, then ASCII letters, digits, `_` (not a keyword) |
| INT | one or more ASCII digits |
| STRING | `"..."` on one line, escapes `\"` `\\` `\n` `\t` |
| keywords | `state tool agent flow start end input output llm one_of if else and or not true false max_iterations string int bool list untrusted sensitive sanitizer` |
| symbols | `{ } ( ) [ ] , : . = == != < > <= >= -> -[` |
| comment | `#` to end of line (skipped) |

## Notes and small decisions

- A state field may have a default value (`context: list[string] = []`). A field with a default counts as written at `start`. This was added because in the spec's example `responder` reads `state.context`, which is never written on the `else` path. Without a default, the compiler correctly reports that as AF-E302.
- `-[` is one token (the opening of a labelled edge). The closing is the two tokens `]` and `->`, so `]->` and `] ->` both work.
- Comparisons do not chain: `a == b == c` is a syntax error. This keeps the grammar simple and avoids confusing meaning.
- A bare name is never a value. The only thing an `IDENT` can start inside an expression is a tool call `name(...)`. State is always accessed as `state.field`.
- Edge chains are sugar: `a -> b -[c]-> d` becomes the two edges `a -> b` and `b -[c]-> d`.
- There is no minus operator and no negative number literal (not needed by the language).
- Panic mode recovery: on a syntax error the parser records it, skips tokens until the start of the next declaration (`tool`, `agent`, `flow`, or `state` followed by a name), and continues. That is how one run reports several syntax errors.
