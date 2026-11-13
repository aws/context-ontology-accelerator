// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { Construct } from "constructs";
import { prefixed } from "../context";

/**
 * Base construct for all SemanticContext resources.
 *
 * Provides a consistent naming helper that produces names in the form:
 *   `{prefix}-{env}-{name}`
 */
export class SCLConstruct extends Construct {
  /** Return a consistently prefixed resource name. */
  protected prefixed(name: string): string {
    return prefixed(this.node, name);
  }
}
