import { createContext } from "react";

/** A tool call another view asked to show; `request` changes on every ask. */
export type RevealCall = { callId: string; request: number };

export const RevealCallContext = createContext<RevealCall | null>(null);