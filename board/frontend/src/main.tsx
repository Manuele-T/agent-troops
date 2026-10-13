import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { HttpDataSource } from "./adapters/httpDataSource";
import "./styles.css";

// Composition root: pick the concrete adapter here and inject it. Swapping
// adapters (synthetic, etc.) is a one-line change and needs no UI edits.
const source = new HttpDataSource();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App source={source} />
  </React.StrictMode>,
);
