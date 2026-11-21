import { createContext } from "react";

/**
 * Something another view asked to show: a tool call, a reply or reasoning by
 * its context item id (`<record>:text`, `<record>:reasoning`), or a message by
 * its outline id. When `contextId` is not on screen, `tocId` is the fallback.
 * `request` changes on every ask.
 */
export type RevealCall = { callId?: string; contextId?: string; tocId?: string; request: number };

export const RevealCallContext = createContext<RevealCall | null>(null);