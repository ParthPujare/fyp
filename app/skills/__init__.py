"""Skills — folder/markdown procedural memory registry (plan.md §9).

A skill = named, versioned procedure stored as ``skills/<scope>/<name>/skill.md``
with YAML frontmatter (name, trigger, scope, owner_role, version, args_schema,
steps[], tool_calls[]) and a markdown body for humans. Skills are resource-like:
visibility is enforced by Casbin ``skill_<name>`` rows, and every nested tool call
in a skill passes through the same intercept/HITL/sanitize chain as a direct call.
"""
