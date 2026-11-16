// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Lets a page publish breadcrumb items into the AppLayout `breadcrumbs` slot
 * via `setBreadcrumbs`/`clearBreadcrumbs`.
 */
import React, { createContext, useCallback, useContext, useState } from "react";

/** A single breadcrumb crumb. `onClick` runs instead of navigation when set. */
export interface BreadcrumbItem {
  text: string;
  onClick?: () => void;
}

interface BreadcrumbContextValue {
  items: BreadcrumbItem[];
  setBreadcrumbs: (items: BreadcrumbItem[]) => void;
  clearBreadcrumbs: () => void;
}

const BreadcrumbContext = createContext<BreadcrumbContextValue>({
  items: [],
  setBreadcrumbs: () => {},
  clearBreadcrumbs: () => {},
});

export function BreadcrumbProvider({
  children,
}: {
  children: React.ReactNode;
}) {
  const [items, setItems] = useState<BreadcrumbItem[]>([]);

  const setBreadcrumbs = useCallback((next: BreadcrumbItem[]) => {
    setItems(next);
  }, []);

  const clearBreadcrumbs = useCallback(() => {
    setItems([]);
  }, []);

  return (
    <BreadcrumbContext.Provider
      value={{ items, setBreadcrumbs, clearBreadcrumbs }}
    >
      {children}
    </BreadcrumbContext.Provider>
  );
}

export function useBreadcrumbs() {
  return useContext(BreadcrumbContext);
}
