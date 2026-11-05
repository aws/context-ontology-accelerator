# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Document-to-table bridge for a namespace's induced ontology.

A namespace's induced ontology holds two disconnected halves: the table classes
(one per R2RML-mapped table, one datatype property per column) and the document
classes induced from the lexical graph. The bridge links the halves and moves
the business meaning that lives only in documents onto the table side, where the
NL→SQL path reads it:

1. align document classes to table classes (the shared grounding service, scoped
   to the namespace's own table classes) and to columns (embedding candidates,
   confirmed by an LLM per table);
2. synthesise a business definition for each table and column the documents
   support, from the linked document classes, their supporting facts, and
   document mentions of the table or column names;
3. write ``skos:closeMatch`` / ``skos:relatedMatch`` links and the definitions
   (``rdfs:comment`` + ``skos:definition``) into the ontology's named graph and
   re-embed the touched tables and columns.

Off unless requested: the ``bridge_to_tables`` induction strategy runs it as a
polled job on an already-accepted ontology.
"""

from coa_ontology.inducer.bridge.planner import BridgeOptions

__all__ = ["BridgeOptions"]
