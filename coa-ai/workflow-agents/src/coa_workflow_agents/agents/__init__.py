# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Strands agents: the supervisor, and the sub-agents it calls as tools."""

from coa_workflow_agents.agents.supervisor import build_supervisor_agent

__all__ = ["build_supervisor_agent"]
