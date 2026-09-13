---
name: send_campaign_email
trigger: "Send a campaign email via Gmail"
scope: dept/marketing
owner_role: marketing_agent
version: 1
args_schema:
  to:
    type: string
    description: "recipient email"
  subject:
    type: string
    description: "email subject"
  body:
    type: string
    description: "email body"
steps:
  - Send the campaign email via the connected Gmail account
tool_calls:
  - tool: gmail_send
    args:
      to: "{{to}}"
      subject: "{{subject}}"
      body: "{{body}}"
---

# Send campaign email

Marketing-only wrapper over the connected Gmail account. The nested `gmail_send`
call runs through the same intercept/HITL/sanitize chain as a direct call —
`risk=critical` so it suspends for admin approval.
