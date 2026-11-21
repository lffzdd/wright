import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "@fontsource-variable/inter";
import "@fontsource-variable/jetbrains-mono";
import "./styles.css";
import { cachedTheme, paintTheme, systemTheme } from "./theme";
import { cachedThoughtStyle, paintThoughtStyle } from "./thoughtStyle";

// Paint before the first render; the saved preference replaces this once it loads.
paintTheme(cachedTheme() ?? systemTheme());
paintThoughtStyle(cachedThoughtStyle() ?? "quiet");

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
