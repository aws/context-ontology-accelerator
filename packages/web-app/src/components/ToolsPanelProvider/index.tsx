// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React, { createContext, useCallback, useContext, useState } from "react";
import type { IconProps } from "@cloudscape-design/components/icon";

export interface DrawerDefinition {
  id: string;
  content: React.ReactNode;
  trigger: {
    iconName?: IconProps.Name;
    iconSvg?: React.ReactNode;
  };
  ariaLabels: {
    drawerName: string;
    closeButton?: string;
    triggerButton?: string;
  };
}

interface ToolsPanelState {
  drawers: DrawerDefinition[];
  activeDrawerId: string | null;
}

interface ToolsPanelContextValue {
  state: ToolsPanelState;
  registerDrawer: (drawer: DrawerDefinition) => void;
  unregisterDrawer: (id: string) => void;
  openDrawer: (id: string) => void;
  closeDrawer: () => void;
}

const ToolsPanelContext = createContext<ToolsPanelContextValue>({
  state: { drawers: [], activeDrawerId: null },
  registerDrawer: () => {},
  unregisterDrawer: () => {},
  openDrawer: () => {},
  closeDrawer: () => {},
});

export function ToolsPanelProvider({
  children,
}: {
  children: React.ReactNode;
}) {
  const [state, setState] = useState<ToolsPanelState>({
    drawers: [],
    activeDrawerId: null,
  });

  const registerDrawer = useCallback((drawer: DrawerDefinition) => {
    setState((prev) => {
      const existing = prev.drawers.findIndex((d) => d.id === drawer.id);
      if (existing >= 0) {
        const updated = [...prev.drawers];
        updated[existing] = drawer;
        return { ...prev, drawers: updated };
      }
      return { ...prev, drawers: [...prev.drawers, drawer] };
    });
  }, []);

  const unregisterDrawer = useCallback((id: string) => {
    setState((prev) => ({
      drawers: prev.drawers.filter((d) => d.id !== id),
      activeDrawerId: prev.activeDrawerId === id ? null : prev.activeDrawerId,
    }));
  }, []);

  const openDrawer = useCallback((id: string) => {
    setState((prev) => ({ ...prev, activeDrawerId: id }));
  }, []);

  const closeDrawer = useCallback(() => {
    setState((prev) => ({ ...prev, activeDrawerId: null }));
  }, []);

  return (
    <ToolsPanelContext.Provider
      value={{
        state,
        registerDrawer,
        unregisterDrawer,
        openDrawer,
        closeDrawer,
      }}
    >
      {children}
    </ToolsPanelContext.Provider>
  );
}

export function useToolsPanel() {
  return useContext(ToolsPanelContext);
}
