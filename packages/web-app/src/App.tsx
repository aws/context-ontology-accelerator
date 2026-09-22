// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React, { useEffect, useMemo, useState } from "react";
import { Routes, Route, useNavigate, useLocation } from "react-router-dom";
import AppLayout from "@cloudscape-design/components/app-layout-toolbar";
import Flashbar from "@cloudscape-design/components/flashbar";
import SideNavigation from "@cloudscape-design/components/side-navigation";
import TopNavigation, {
  type TopNavigationProps,
} from "@cloudscape-design/components/top-navigation";
import SplitPanel from "@cloudscape-design/components/split-panel";
import {
  applyMode,
  applyDensity,
  Mode,
  Density,
} from "@cloudscape-design/global-styles";
import { NamespaceList } from "./pages/NamespaceList";
import { CreateNamespace } from "./pages/CreateNamespace";
import { HomePage } from "./pages/HomePage";
import { GetNamespace } from "./pages/GetNamespace";
import { NamespacePermissions } from "./pages/NamespacePermissions";
import { TableDetail } from "./pages/TableDetail";
import { ProfilePage } from "./pages/ProfilePage";
import { Playground } from "./pages/Playground";
import { TraceDetail } from "./pages/TraceDetail";
import { SourceList } from "./pages/SourceList";
import { ConnectSource } from "./pages/ConnectSource";
import { SourceDetail } from "./pages/SourceDetail";
import { OntologyPage } from "./pages/OntologyPage";
import { ProposalDetailPage } from "./pages/ontology/induce/ProposalDetail";
import { GraphSearchPage } from "./pages/ontology/GraphSearch";
import { InducedOntologyDetailPage } from "./pages/ontology/InducedOntologyDetail";
import { OntologyClassDetailPage } from "./pages/ontology/OntologyClassDetail";
import { MetricList } from "./pages/MetricList";
import { IdentityPage } from "./pages/IdentityPage";
import { SystemHealthPage } from "./pages/SystemHealthPage";
import { MetricDetail } from "./pages/MetricDetail";
import { MetricForm } from "./pages/MetricForm";
import { useUser } from "./auth";
import {
  NamespaceProvider,
  useCurrentNamespace,
} from "./components/NamespaceProvider";
import {
  SplitPanelProvider,
  useSplitPanel,
} from "@components/SplitPanelProvider";
import {
  ToolsPanelProvider,
  useToolsPanel,
} from "@components/ToolsPanelProvider";
import type { DrawerDefinition } from "@components/ToolsPanelProvider";
import {
  BreadcrumbProvider,
  useBreadcrumbs,
  type BreadcrumbItem,
} from "@components/BreadcrumbProvider";
import BreadcrumbGroup from "@cloudscape-design/components/breadcrumb-group";
import { PlaygroundHelpPanel } from "./pages/PlaygroundHelpPanel";
import { PageErrorBoundary } from "@components/PageErrorBoundary";
import { VersionBadge } from "@components/VersionBadge";

const DARK_MODE_KEY = "scl.darkMode";
const DENSITY_KEY = "scl.densityCompact";

function readDarkMode(): boolean {
  if (typeof window === "undefined") return true;
  try {
    const stored = window.localStorage.getItem(DARK_MODE_KEY);
    return stored === null ? true : stored === "1";
  } catch {
    return true;
  }
}

function readDensity(): boolean {
  if (typeof window === "undefined") return false;
  try {
    return window.localStorage.getItem(DENSITY_KEY) === "1";
  } catch {
    return false;
  }
}

const initialDark = readDarkMode();
const initialCompact = readDensity();
if (typeof document !== "undefined") {
  applyMode(initialDark ? Mode.Dark : Mode.Light);
  applyDensity(initialCompact ? Density.Compact : Density.Comfortable);
}

export default function App() {
  return (
    <NamespaceProvider>
      <SplitPanelProvider>
        <ToolsPanelProvider>
          <BreadcrumbProvider>
            <AppShell />
          </BreadcrumbProvider>
        </ToolsPanelProvider>
      </SplitPanelProvider>
    </NamespaceProvider>
  );
}

function AppShell() {
  const navigate = useNavigate();
  const location = useLocation();
  const { user, logout } = useUser();
  const [navOpen, setNavOpen] = useState(true);
  const [darkMode, setDarkMode] = useState(initialDark);
  const [densityCompact, setDensityCompact] = useState(initialCompact);
  const { current, namespaces, setNamespace, namespaceId, isLoading } =
    useCurrentNamespace();
  const { state: splitPanelState, closePanel } = useSplitPanel();
  const { items: breadcrumbItems } = useBreadcrumbs();
  const {
    state: toolsPanelState,
    openDrawer,
    closeDrawer,
    registerDrawer,
    unregisterDrawer,
  } = useToolsPanel();

  useEffect(() => {
    registerDrawer({
      id: "help",
      content: <PlaygroundHelpPanel />,
      trigger: {
        iconSvg: (
          <svg
            viewBox="0 0 16 16"
            xmlns="http://www.w3.org/2000/svg"
            focusable="false"
            aria-hidden="true"
          >
            <circle cx="8" cy="8" r="7" fill="none" stroke="currentColor" />
            <path d="M8 12V7M8 6V4" stroke="currentColor" />
          </svg>
        ),
      },
      ariaLabels: { drawerName: "Info", closeButton: "Close info" },
    });
    return () => unregisterDrawer("help");
  }, [registerDrawer, unregisterDrawer]);
  const [splitPanelPreferences, setSplitPanelPreferences] = useState<{
    position: "side" | "bottom";
  }>({ position: "side" });
  const [namespaceWarning, setNamespaceWarning] = useState(false);

  // The namespace list and create routes have no "current namespace" and
  // are deliberately rendered without the namespace-scoped side-nav. Mirror
  // that in the top nav by hiding the namespace switcher utility there.
  const isNamespaceListRoute = location.pathname === "/namespaces/create";

  useEffect(() => {
    window.localStorage.setItem(DARK_MODE_KEY, darkMode ? "1" : "0");
    applyMode(darkMode ? Mode.Dark : Mode.Light);
  }, [darkMode]);

  useEffect(() => {
    window.localStorage.setItem(DENSITY_KEY, densityCompact ? "1" : "0");
    applyDensity(densityCompact ? Density.Compact : Density.Comfortable);
  }, [densityCompact]);

  const nsPath = namespaceId ? `/namespaces/${namespaceId}` : "#";
  const namespaceScopedNavItems = [
    {
      type: "section" as const,
      text: "Scan",
      items: [
        {
          type: "link" as const,
          text: "Sources",
          href: `${nsPath}/sources`,
        },
      ],
    },
    {
      type: "section" as const,
      text: "Ontology",
      items: [
        {
          type: "link" as const,
          text: "Induction",
          href: `${nsPath}/ontology`,
        },
        {
          type: "link" as const,
          text: "Explorer",
          href: `${nsPath}/ontology/graph`,
        },
        {
          type: "link" as const,
          text: "Metrics",
          href: `${nsPath}/metrics`,
        },
      ],
    },
  ];

  // Flat [section, label, href] index of every left-nav link, used to derive a
  // default breadcrumb (COA › Section › Page) for pages that don't publish
  // their own richer trail via useBreadcrumbs.
  const navIndex: Array<{
    section: string | null;
    label: string;
    href: string;
  }> = [
    { section: null, label: "Home", href: "/" },
    ...namespaceScopedNavItems.flatMap((s) =>
      s.items.map((i) => ({ section: s.text, label: i.text, href: i.href })),
    ),
    {
      section: "Serve",
      label: "Playground",
      href: "/playground",
    },
    { section: "Administration", label: "Namespaces", href: "/namespaces" },
    { section: "Administration", label: "Identity", href: "/identity" },
    {
      section: "Administration",
      label: "System Health",
      href: "/system-health",
    },
  ];

  // Longest-prefix match of the current path against known nav hrefs, so detail
  // routes (e.g. /namespaces/:id/metrics/:id) still resolve to their nav page.
  const matchedNav = navIndex
    .filter((n) => n.href !== "#" && location.pathname.startsWith(n.href))
    .sort((a, b) => b.href.length - a.href.length)[0];

  const routeDefaultCrumbs: BreadcrumbItem[] = matchedNav
    ? [
        { text: "COA", onClick: () => navigate("/") },
        ...(matchedNav.section ? [{ text: matchedNav.section }] : []),
        {
          text: matchedNav.label,
          onClick: () => navigate(matchedNav.href),
        },
      ]
    : [{ text: "COA", onClick: () => navigate("/") }];

  // A page's own published trail wins; otherwise fall back to the route default.
  const effectiveCrumbs =
    breadcrumbItems.length > 0 ? breadcrumbItems : routeDefaultCrumbs;

  // Cloudscape's TopNavigation re-measures itself (and can loop into React's
  // "maximum update depth" guard) whenever the `utilities` prop changes
  // identity. Keep the array stable across AppShell re-renders.
  const topNavUtilities: TopNavigationProps["utilities"] = useMemo(
    () => [
      // The namespace switcher only makes sense once the user has
      // navigated _into_ a namespace-scoped section. On the namespace
      // list and create routes there is no "current" namespace, and
      // surfacing the dropdown there confuses users into thinking
      // they are viewing a namespace nested inside another. Hide it
      // there to match the side-nav, which is also empty on those
      // routes.
      ...(isNamespaceListRoute
        ? []
        : [
            {
              type: "menu-dropdown" as const,
              text: `Namespace: ${isLoading ? "Loading…" : (current?.displayName ?? current?.name ?? "—")}`,
              ariaLabel: "Namespace switcher",
              title: "Namespace",
              onItemClick: ({ detail }: { detail: { id: string } }) =>
                setNamespace(detail.id),
              items:
                namespaces.length > 0
                  ? [...namespaces]
                      .sort((a, b) =>
                        (a.displayName ?? a.name ?? "").localeCompare(
                          b.displayName ?? b.name ?? "",
                          undefined,
                          { sensitivity: "base" },
                        ),
                      )
                      .map((n) => ({
                        id: n.namespaceId!,
                        text: n.displayName ?? n.name ?? "",
                      }))
                  : [
                      {
                        id: "none",
                        text: "No namespaces — create one first",
                        disabled: true,
                      },
                    ],
            },
          ]),
      {
        type: "button",
        iconSvg: React.createElement(
          "svg",
          { viewBox: "0 0 16 16", xmlns: "http://www.w3.org/2000/svg" },
          React.createElement("circle", {
            cx: 8,
            cy: 8,
            r: 5.5,
            fill: "none",
            stroke: "currentColor",
            strokeWidth: 1.5,
          }),
          React.createElement("path", {
            d: "M8 2.5v11A5.5 5.5 0 0 1 8 2.5z",
            fill: "currentColor",
          }),
        ),
        ariaLabel: darkMode ? "Switch to light mode" : "Switch to dark mode",
        title: darkMode ? "Light mode" : "Dark mode",
        onClick: () => {
          const next = !darkMode;
          setDarkMode(next);
          applyMode(next ? Mode.Dark : Mode.Light);
        },
      },
      {
        type: "menu-dropdown",
        iconName: "settings",
        ariaLabel: "Settings",
        title: "Settings",
        onItemClick: ({ detail }) => {
          if (detail.id === "theme") {
            setDarkMode((prev) => !prev);
          } else if (detail.id === "density") {
            setDensityCompact((prev) => !prev);
          }
        },
        items: [
          {
            id: "density",
            text: densityCompact
              ? "Switch to comfortable density"
              : "Switch to compact density",
          },
          {
            id: "theme",
            text: darkMode ? "Switch to light mode" : "Switch to dark mode",
          },
        ],
      },
      {
        type: "menu-dropdown",
        text: user?.email || "",
        iconName: "user-profile",
        ariaLabel: "User menu",
        items: [
          { id: "profile", text: "Profile" },
          { id: "signout", text: "Sign out" },
        ],
        onItemClick: ({ detail }) => {
          if (detail.id === "profile") {
            navigate("/profile");
          } else if (detail.id === "signout") {
            logout().catch((err) => console.error("Logout failed:", err));
          }
        },
      },
    ],
    [
      isNamespaceListRoute,
      isLoading,
      current?.displayName,
      current?.name,
      namespaces,
      setNamespace,
      darkMode,
      densityCompact,
      user?.email,
      navigate,
      logout,
    ],
  );

  return (
    <>
      <div id="top-nav" style={{ position: "sticky", top: 0, zIndex: 1001 }}>
        <TopNavigation
          identity={{
            href: "/",
            title: "Context Ontology Accelerator",
            logo: { src: "/aws-logo-white.svg", alt: "COA" },
            onFollow: (e) => {
              e.preventDefault();
              navigate("/");
            },
          }}
          utilities={topNavUtilities}
        />
      </div>
      <AppLayout
        headerSelector="#top-nav"
        breadcrumbs={
          <BreadcrumbGroup
            ariaLabel="Breadcrumbs"
            // Encode each crumb's index in its href so the click handler can
            // resolve back to the source item and run its onClick action.
            items={effectiveCrumbs.map((b, i) => ({
              text: b.text,
              href: `#${i}`,
            }))}
            onClick={(e) => {
              e.preventDefault();
              const idx = Number(e.detail.href.replace("#", ""));
              effectiveCrumbs[idx]?.onClick?.();
            }}
          />
        }
        notifications={
          namespaceWarning ? (
            <Flashbar
              items={[
                {
                  type: "warning",
                  content: "Please select or create a namespace first.",
                  dismissible: true,
                  onDismiss: () => setNamespaceWarning(false),
                  id: "ns-warning",
                },
              ]}
            />
          ) : undefined
        }
        splitPanel={
          splitPanelState.isOpen && splitPanelState.content ? (
            <SplitPanel
              header={splitPanelState.header}
              i18nStrings={{
                closeButtonAriaLabel: "Close panel",
                openButtonAriaLabel: "Open panel",
                preferencesTitle: "Split panel preferences",
                preferencesPositionLabel: "Position",
                preferencesPositionDescription:
                  "Choose the position of the panel",
                preferencesPositionSide: "Side",
                preferencesPositionBottom: "Bottom",
                preferencesConfirm: "Confirm",
                preferencesCancel: "Cancel",
                resizeHandleAriaLabel: "Resize split panel",
              }}
            >
              {splitPanelState.content}
            </SplitPanel>
          ) : undefined
        }
        splitPanelOpen={splitPanelState.isOpen}
        onSplitPanelToggle={() => closePanel()}
        splitPanelPreferences={splitPanelPreferences}
        onSplitPanelPreferencesChange={({ detail }) =>
          setSplitPanelPreferences(detail)
        }
        navigation={
          <SideNavigation
            activeHref={location.pathname}
            header={{ href: "/", text: "Context Ontology Accelerator" }}
            items={[
              { type: "link", text: "Home", href: "/" },
              ...namespaceScopedNavItems,
              {
                type: "section",
                text: "Serve",
                items: [
                  {
                    type: "link",
                    text: "Playground",
                    href: "/playground",
                  },
                ],
              },
              { type: "divider" },
              {
                type: "section",
                text: "Administration",
                items: [
                  { type: "link", text: "Namespaces", href: "/namespaces" },
                  { type: "link", text: "Identity", href: "/identity" },
                  {
                    type: "link",
                    text: "System Health",
                    href: "/system-health",
                  },
                ],
              },
            ]}
            onFollow={(e) => {
              e.preventDefault();
              if (e.detail.href.startsWith("#")) {
                setNamespaceWarning(true);
                navigate("/namespaces");
              } else {
                setNamespaceWarning(false);
                navigate(e.detail.href);
              }
            }}
          />
        }
        navigationOpen={navOpen}
        onNavigationChange={({ detail }) => setNavOpen(detail.open)}
        toolsHide
        drawers={toolsPanelState.drawers.map((d: DrawerDefinition) => ({
          id: d.id,
          content: d.content,
          trigger: d.trigger,
          ariaLabels: d.ariaLabels,
        }))}
        activeDrawerId={toolsPanelState.activeDrawerId}
        onDrawerChange={({ detail }) => {
          if (detail.activeDrawerId) openDrawer(detail.activeDrawerId);
          else closeDrawer();
        }}
        content={
          <PageErrorBoundary>
            <Routes>
              <Route path="/" element={<HomePage />} />
              <Route path="/namespaces" element={<NamespaceList />} />
              <Route path="/namespaces/create" element={<CreateNamespace />} />
              <Route
                path="/namespaces/:namespaceId"
                element={<GetNamespace />}
              />
              <Route
                path="/namespaces/:namespaceId/permissions"
                element={<NamespacePermissions />}
              />
              <Route
                path="/namespaces/:namespaceId/sources"
                element={<SourceList />}
              />
              <Route
                path="/namespaces/:namespaceId/sources/connect"
                element={<ConnectSource />}
              />
              <Route
                path="/namespaces/:namespaceId/sources/:sourceId"
                element={<SourceDetail />}
              />
              <Route
                path="/namespaces/:namespaceId/sources/:dataSourceId/tables/:tableId"
                element={<TableDetail />}
              />
              <Route
                path="/namespaces/:namespaceId/ontology"
                element={<OntologyPage />}
              />
              <Route
                path="/namespaces/:namespaceId/ontology/proposals/:proposalId"
                element={<ProposalDetailPage />}
              />
              <Route
                path="/namespaces/:namespaceId/ontology/graph"
                element={<GraphSearchPage />}
              />
              <Route
                path="/namespaces/:namespaceId/ontology/induced"
                element={<InducedOntologyDetailPage />}
              />
              <Route
                path="/namespaces/:namespaceId/ontology/induced/class"
                element={<OntologyClassDetailPage />}
              />
              <Route
                path="/namespaces/:namespaceId/metrics"
                element={<MetricList />}
              />
              <Route
                path="/namespaces/:namespaceId/metrics/create"
                element={<MetricForm />}
              />
              <Route
                path="/namespaces/:namespaceId/metrics/:metricName"
                element={<MetricDetail />}
              />
              <Route
                path="/namespaces/:namespaceId/metrics/:metricName/edit"
                element={<MetricForm />}
              />
              <Route path="/identity" element={<IdentityPage />} />
              <Route path="/system-health" element={<SystemHealthPage />} />
              <Route path="/profile" element={<ProfilePage />} />
              <Route path="/playground" element={<Playground />} />
              <Route
                path="/playground/trace/:sessionId/:requestId"
                element={<TraceDetail />}
              />
            </Routes>
          </PageErrorBoundary>
        }
      />
      <VersionBadge />
    </>
  );
}
