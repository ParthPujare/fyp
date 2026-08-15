---
name: launch_campaign
trigger: "Launch a marketing campaign for a customer"
scope: dept/marketing
owner_role: marketing_agent
version: 1
args_schema:
  customer:
    type: string
    description: "customer to search for first"
  campaign_name:
    type: string
    description: "campaign name"
  budget:
    type: number
    description: "campaign budget"
  status:
    type: string
    description: "campaign status"
  region:
    type: string
    description: "campaign region"
steps:
  - Search for the customer
  - Create the campaign
tool_calls:
  - tool: crm_search_customer
    args:
      query: "{{customer}}"
  - tool: crm_create_campaign
    args:
      name: "{{campaign_name}}"
      budget: "{{budget}}"
      status: "{{status}}"
      region: "{{region}}"
---

# Launch campaign

A deterministic two-step procedure: look up a customer, then create a marketing
campaign for them. Only the marketing_agent role has a Casbin row for this skill,
so other departments never see it in their tool schema.
