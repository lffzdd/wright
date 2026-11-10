import { createContext, useContext } from "react";

/** Opens a file in the inspector's Files tab, optionally at a line. Null where no inspector exists. */
export type OpenFile = (path: string, line?: number) => void;

export const OpenFileContext = createContext<OpenFile | null>(null);

export function useOpenFile(): OpenFile | null {
  return useContext(OpenFileContext);
}
