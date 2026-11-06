// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { OntologyGraph } from "./OntologyGraph";
import { ResolutionTraceSafe } from "./ResolutionTraceSafe";

export function Hero(): React.JSX.Element {
  return (
    <section className="relative overflow-hidden">
      {/* Ontology watermark: static, faint, editorial. Hidden on mobile. */}
      <div
        className="pointer-events-none absolute inset-0 hidden items-center justify-center md:flex"
        aria-hidden="true"
      >
        <div className="absolute -right-48 -top-12 h-[880px] w-[1500px] opacity-[0.7] lg:-right-56 lg:h-[960px] lg:w-[1680px]">
          <OntologyGraph variant="hero" />
        </div>
        <div className="absolute inset-y-0 left-0 w-[62%] bg-gradient-to-r from-paper via-paper/95 to-transparent" />
        <div className="absolute inset-y-0 right-0 w-40 bg-gradient-to-l from-paper via-paper/70 to-transparent" />
        <div className="absolute inset-x-0 bottom-0 h-48 bg-gradient-to-b from-transparent to-paper" />
        <div className="absolute inset-x-0 top-0 h-24 bg-gradient-to-b from-paper to-transparent" />
      </div>

      <div className="relative mx-auto w-full max-w-7xl px-6 pb-24 pt-16 sm:px-10 sm:pt-20 lg:px-14 lg:pb-32 lg:pt-24">
        <div className="grid items-start gap-16 lg:grid-cols-[1.05fr_0.95fr] lg:gap-20">
          {/* Left column */}
          <div className="min-w-0 max-w-[36rem]">
            <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
              <span className="display-eyebrow">
                W3C · OWL · SHACL · MCP · Apache 2.0
              </span>
            </div>

            <h1 className="display-h1 mt-7 text-[clamp(2.4rem,5.8vw,4.75rem)] text-ink text-balance">
              Context Ontology Accelerator
            </h1>

            <p className="mt-7 max-w-[32rem] text-[17px] leading-[1.55] text-ink-soft">
              Context Ontology Accelerator is a self-managed, open source
              accelerator (Apache 2.0) that helps customers build user-defined
              ontologies to make agent decisions more accurate, explainable, and
              auditable. It uses AI-assisted induction (with human review) to
              accelerate customers' creation of ontologies that are specific to
              their entities, relationships, rules, and constraints. Customers
              review and edit each proposed ontology before they accept it, and
              use the result across their systems, built on open W3C standards
              (OWL, RDF, SHACL). The
              ontology is stored in a customer-owned knowledge graph that any
              agent can consume through Context Ontology Accelerator MCP.
            </p>

            <div className="mt-10 flex flex-wrap items-center gap-3">
              <a
                href="#/deploying"
                className="group inline-flex items-center gap-2.5 rounded-full bg-ink px-5 py-3 text-[14px] font-medium text-paper transition-all hover:bg-terracotta-deep"
              >
                <span>Get Started</span>
              </a>
              <a
                href="#/how-it-works"
                className="group inline-flex items-center gap-2 rounded-full border border-ink/15 px-5 py-3 text-[14px] font-medium text-ink transition-all hover:border-ink/35 hover:bg-ink/[0.04]"
              >
                <span>How it works</span>
                <span className="font-mono text-[12px] text-ink-muted transition-colors group-hover:text-ink">
                  →
                </span>
              </a>
            </div>

            <dl className="mt-12 grid grid-cols-1 gap-y-5 border-t border-rule pt-6 sm:grid-cols-[auto_1fr] sm:gap-x-10">
              <dt className="font-mono text-[11px] uppercase tracking-[0.18em] text-ink-muted">
                Standards
              </dt>
              <dd className="font-mono text-[12.5px] text-ink-soft">
                RDF · OWL 2 · SHACL · R2RML · SPARQL 1.1
              </dd>
              <dt className="font-mono text-[11px] uppercase tracking-[0.18em] text-ink-muted">
                Interfaces
              </dt>
              <dd className="font-mono text-[12.5px] text-ink-soft">
                MCP server · REST · SPARQL endpoint
              </dd>
            </dl>
          </div>

          {/* Right column */}
          <div className="relative min-w-0">
            <ResolutionTraceSafe />
            <p className="mt-3 max-w-[32rem] text-[11px] leading-[1.5] text-ink-faint">
              Scenarios are illustrative and do not constitute regulatory
              advice; outputs should be independently validated before use in
              regulatory filings or compliance reporting. Latency and LLM-call
              figures shown reflect a pre-compiled governed metric — actual
              performance varies by query complexity, data volume, and
              infrastructure configuration, and not all query types are eligible
              for pre-compiled resolution.
            </p>
          </div>
        </div>
      </div>
    </section>
  );
}
