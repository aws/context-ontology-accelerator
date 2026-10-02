// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { type PageId } from '../pages'

type NavSection = {
  stage: string
  items: { id: PageId; label: string }[]
}

const SECTIONS: NavSection[] = [
  { stage: 'Administrate', items: [
    { id: 'namespaces', label: 'Namespaces' },
    { id: 'authentication-setup', label: 'Authentication' },
    { id: 'role-permissions', label: 'Roles & Permissions' },
    { id: 'agent-access', label: 'Agent Access' },
    { id: 'cedar-policies', label: 'Cedar Policies' },
  ]},
  { stage: 'Scan', items: [
    { id: 'sources', label: 'Sources' },
    { id: 'cross-account-sources', label: 'Cross-Account Sources' },
    { id: 'custom-connector-sources', label: 'Custom Connectors' },
    { id: 'databricks-sources', label: 'Databricks Sources' },
  ]},
  { stage: 'Model', items: [
    { id: 'ontologies', label: 'Ontologies' },
    { id: 'metrics', label: 'Metrics' },
  ]},
  { stage: 'Serve', items: [{ id: 'serve', label: 'Serve' }] },
  { stage: 'Developer', items: [
    { id: 'getting-started', label: 'Getting Started' },
    { id: 'package-guide', label: 'Package Guide' },
    { id: 'smithy-codegen', label: 'Smithy Codegen' },
  ]},
]

interface SidebarProps {
  currentPage: PageId
}

export function Sidebar({ currentPage }: SidebarProps): React.JSX.Element {
  return (
    <aside className="hidden w-56 shrink-0 border-r border-rule py-8 pr-6 md:block">
      {SECTIONS.map((section) => (
        <div key={section.stage} className="mb-6">
          <p className="mb-2 text-[11px] font-medium tracking-[0.18em] uppercase text-terracotta">
            {section.stage}
          </p>
          <ul className="space-y-0.5">
            {section.items.map((item) => (
              <li key={item.id}>
                <a
                  href={`#/${item.id}`}
                  className={`block rounded-md px-3 py-2 text-[13.5px] font-medium transition-colors ${
                    currentPage === item.id
                      ? 'bg-terracotta/8 text-terracotta-deep'
                      : 'text-ink-muted hover:text-ink hover:bg-ink/[0.03]'
                  }`}
                >
                  {item.label}
                </a>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </aside>
  )
}
