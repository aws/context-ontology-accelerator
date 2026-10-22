// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import "@cloudscape-design/global-styles/index.css";
import { RuntimeContextProvider } from "@components/RuntimeContext";
import { ControlPlaneClientProvider } from "@components/ControlPlaneClientProvider";
import { ApiClientProvider } from "@components/ApiClientProvider";
import { Auth } from "@components/Auth";
import { LocaleProvider } from "./i18n/LocaleProvider";
import App from "./App";

const queryClient = new QueryClient();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <LocaleProvider>
      <BrowserRouter>
        <RuntimeContextProvider>
          <ControlPlaneClientProvider>
            <ApiClientProvider>
              <QueryClientProvider client={queryClient}>
                <Auth applicationName="Context Ontology Accelerator">
                  <App />
                </Auth>
              </QueryClientProvider>
            </ApiClientProvider>
          </ControlPlaneClientProvider>
        </RuntimeContextProvider>
      </BrowserRouter>
    </LocaleProvider>
  </React.StrictMode>,
);
