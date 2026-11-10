import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "@fontsource-variable/geist";
import "@fontsource-variable/geist-mono";
import "./styles.css";
import { cachedTheme, paintTheme, systemTheme } from "./theme";

// Paint before the first render; the saved preference replaces this once it loads.
paintTheme(cachedTheme() ?? systemTheme());

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
