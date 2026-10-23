// Use the locally installed Monaco (no CDN, so the demo works offline)
// and register syntax highlighting for the .af language.

// only the editor core, its features (hover, find, ...) and Python highlighting, not all ~80 languages
import * as monaco from "monaco-editor/esm/vs/editor/editor.api";
import "monaco-editor/esm/vs/editor/editor.all.js";
import "monaco-editor/esm/vs/basic-languages/python/python.contribution";
import editorWorker from "monaco-editor/esm/vs/editor/editor.worker?worker";
import { loader } from "@monaco-editor/react";

self.MonacoEnvironment = {
  getWorker: () => new editorWorker(),
};

loader.config({ monaco });

monaco.languages.register({ id: "agentforge" });

monaco.languages.setMonarchTokensProvider("agentforge", {
  keywords: ["state", "tool", "agent", "flow", "start", "end", "input", "output", "llm", "one_of",
             "if", "else", "and", "or", "not", "max_iterations", "untrusted", "sensitive", "sanitizer",
             "compare", "candidates", "case", "expect", "contains", "mock", "end_to_end", "ensure", "necessity"],
  typeKeywords: ["string", "int", "bool", "list"],
  constants: ["true", "false"],
  tokenizer: {
    root: [
      [/#.*$/, "comment"],
      [/"([^"\\]|\\.)*"/, "string"],
      [/"([^"\\]|\\.)*$/, "string.invalid"],
      [/\d+/, "number"],
      [/[a-zA-Z_]\w*/, {
        cases: {
          "@keywords": "keyword",
          "@typeKeywords": "type",
          "@constants": "number",
          "@default": "identifier",
        },
      }],
      [/-\[|\]->|->/, "operator.arrow"],
      [/==|!=|<=|>=|<|>|=/, "operator"],
    ],
  },
});

monaco.languages.setLanguageConfiguration("agentforge", {
  comments: { lineComment: "#" },
  brackets: [["{", "}"], ["[", "]"], ["(", ")"]],
  autoClosingPairs: [
    { open: "{", close: "}" },
    { open: "[", close: "]" },
    { open: "(", close: ")" },
    { open: '"', close: '"' },
  ],
});

monaco.editor.defineTheme("agentforge-light", {
  base: "vs",
  inherit: true,
  rules: [
    { token: "keyword", foreground: "7c3aed", fontStyle: "bold" },
    { token: "type", foreground: "0891b2" },
    { token: "operator.arrow", foreground: "db2777", fontStyle: "bold" },
    { token: "comment", foreground: "94a3b8", fontStyle: "italic" },
  ],
  colors: {},
});
