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

export const emptyDraft = (): Draft => ({
  prompt: "", attachments: [], documents: [], references: [], command: null, mentions: [], commandId: null,
  clientRequestId: null, boundSessionId: null, environment: "local", execution_mode: "sandbox", localFiles: [], phase: "idle", reason: "",
});
