import type { Attachment, ExecutionMode, FileReference } from "../../types";

export type Draft = {
  prompt: string;
  attachments: Attachment[];
  documents?: Array<{ id: string; filename: string }>;
  references?: FileReference[];
  command?: string | null;
  mentions?: string[];
  commandId: string | null;
  clientRequestId?: string | null;
  boundSessionId?: string | null;
  environment?: "local" | "worktree";
  execution_mode?: ExecutionMode;
  localFiles?: Array<{ name: string; file: File; image: boolean }>;
  phase: "idle" | "awaiting" | "accepted" | "rejected" | "unknown";
  reason: string;
};

/** A new draft; ``execution_mode`` starts from the user's default (sandbox.default_execution). */
export const emptyDraft = (execution_mode: ExecutionMode = "sandbox"): Draft => ({
  prompt: "", attachments: [], documents: [], references: [], command: null, mentions: [], commandId: null,
  clientRequestId: null, boundSessionId: null, environment: "local", execution_mode, localFiles: [], phase: "idle", reason: "",
});
