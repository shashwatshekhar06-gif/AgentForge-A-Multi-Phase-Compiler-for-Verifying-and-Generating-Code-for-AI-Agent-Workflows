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

export interface RealProfileStatus {
  available: boolean;   // a model key is available to the server (never sent to the browser)
  max_calls: number;    // the most model calls one measurement may make
}

export async function fetchRealProfile(): Promise<RealProfileStatus> {
  const response = await fetch("/api/real-profile");
  if (!response.ok) throw new Error(`server returned ${response.status}`);
  return response.json();
}

// Scores the compare blocks with a real model (paid API calls), then compiles again with the winner.
export async function compileReal(source: string): Promise<CompileResult> {
  const response = await fetch("/api/compile-real", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ source }),
  });
  if (!response.ok) {
    let detail = `server returned ${response.status}`;
    try {
      const body = await response.json();
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      // keep the status text
    }
    throw new Error(detail);
  }
  return response.json();
}
