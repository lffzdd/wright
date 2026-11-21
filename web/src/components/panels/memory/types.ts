/** Shapes returned by GET /workspaces/{id}/memory (application/workspace/memory_view.py). */

export type FactKind = "preference" | "constraint" | "fact" | "feedback" | "reference";
export type Tier = "pinned" | "indexed" | "archived";
export type FactStatus = "active" | "superseded" | "quarantined" | "deleted";
export type Origin = "user_stated" | "user_confirmed" | "agent_inferred" | "external";
export type Sensitivity = "none" | "personal" | "sensitive";

export type Fact = {
  id: string;
  space_id: string;
  kind: FactKind;
  subject: string;
  content: string;
  why: string;
  origin: Origin;
  sensitivity: Sensitivity;
  tier: Tier;
  status: FactStatus;
  markers: string[];
  sources: string[];
  created_at: string;
  updated_at: string;
  valid_to: string;
  superseded_by: string;
  last_used_at: string;
  use_count: number;
  revision: number;
  shareable: boolean;
};

export type Episode = {
  id: string;
  goal: string;
  status: string;
  outcome: string;
  created_at: string;
  links: { commits?: string[]; prs?: string[] };
  compacted: boolean;
};

export type Commitment = {
  id: string;
  space_id: string;
  content: string;
  status: "open" | "done" | "dismissed" | "expired";
  due: string;
  created_at: string;
  updated_at: string;
};

export type ChangeField = { name: string; before: unknown; after: unknown };

export type Change = {
  id: number;
  at: string;
  actor: "agent" | "user" | "extractor" | "consolidation";
  action: string;
  target_id: string;
  target_type: "fact" | "episode" | "commitment" | "other";
  scope: string;
  summary: string;
  fields: ChangeField[];
  note: string;
  reverted: boolean;
  forgotten: boolean;
  undoable: boolean;
};

export type Space = { id: string; type: "global" | "repo" | "personal"; name: string };

export type MemorySnapshot = {
  project_id: string;
  current_space: string;
  spaces: Space[];
  facts: Fact[];
  episodes: Episode[];
  commitments: Commitment[];
  changes: Change[];
};

/** Runs one console action, shows progress or the error, then reloads. */
export type RunAction = (action: () => Promise<unknown>) => Promise<boolean>;
