import type { CompileResult, Example } from "./types";

export async function compileSource(source: string): Promise<CompileResult> {
  const response = await fetch("/api/compile", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ source }),
  });
  if (!response.ok) throw new Error(`server returned ${response.status}`);
  return response.json();
}

export async function fetchExamples(): Promise<Example[]> {
  const response = await fetch("/api/examples");
  if (!response.ok) throw new Error(`server returned ${response.status}`);
  return response.json();
}
