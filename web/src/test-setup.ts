import { configure } from "@testing-library/react";

// Under a full parallel run, the default 1s wait for findBy* is too tight for
// the debounced refresh chains in App; keep assertions the same, widen the wait.
configure({ asyncUtilTimeout: 5000 });
