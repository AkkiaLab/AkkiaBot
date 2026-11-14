import { vi } from "vitest";

export function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** A `fetch` stub that answers with the given responses in order and records requests. */
export function fetchSequence(...responses: Array<Response | Error>) {
  const queue = [...responses];
  return vi.fn(async (_input: Request) => {
    const next = queue.shift();
    if (!next) throw new Error("unexpected extra fetch call");
    if (next instanceof Error) throw next;
    return next;
  });
}
