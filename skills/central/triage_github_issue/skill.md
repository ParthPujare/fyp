---
name: triage_github_issue
trigger: "File a GitHub issue for an incident"
scope: central
owner_role: devops_agent
version: 1
args_schema:
  repo:
    type: string
    description: "owner/repo, e.g. acme/web"
  title:
    type: string
    description: "issue title"
  body:
    type: string
    description: "issue body"
steps:
  - List repos to confirm visibility
  - Create the issue
tool_calls:
  - tool: github_list_repos
    args: {}
  - tool: github_create_issue
    args:
      repo: "{{repo}}"
      title: "{{title}}"
      body: "{{body}}"
---

# Triage GitHub issue

Central skill wrapping the connected GitHub account. Nested calls are each
intercepted: a marketing caller may see the skill only if granted, and the
nested `github_create_issue` is DENIED for roles without `github:create`.
