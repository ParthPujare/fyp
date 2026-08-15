---
name: triage_incident
trigger: Triage an infrastructure incident by registering a deployment
scope: central
owner_role: devops_agent
version: 1
args_schema:
  service:
    type: string
    description: service name
  version:
    type: string
    description: version
  environment:
    type: string
    description: environment
  status:
    type: string
    description: deployment status
  deployed_by:
    type: string
    description: who deployed
steps:
- Register the deployment
tool_calls:
- tool: devops_create_deployment
  args:
    service: '{{service}}'
    version: '{{version}}'
    environment: '{{environment}}'
    status: '{{status}}'
    deployed_by: '{{deployed_by}}'
---

# Triage incident

Central skill visible to every department (every role has a Casbin row). It calls
the `devops_create_deployment` tool — so a Marketing/HR/Finance user who invokes it
is **blocked at the nested-tool intercept** (attack A4): the skill is visible, but
the tool it calls is denied by role.
