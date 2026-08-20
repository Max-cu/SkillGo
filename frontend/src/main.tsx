import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { AuthProvider } from "./auth";
import "./styles.css";
import "./skillgo-fluent.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode><AuthProvider><App /></AuthProvider></StrictMode>
);
