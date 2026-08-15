---
name: new_leave_request
trigger: "Create a leave request for an employee"
scope: dept/hr
owner_role: hr_agent
version: 1
args_schema:
  employee_id:
    type: integer
    description: "employee id"
  start_date:
    type: string
    description: "leave start date (YYYY-MM-DD)"
  end_date:
    type: string
    description: "leave end date (YYYY-MM-DD)"
  status:
    type: string
    description: "request status"
  type:
    type: string
    description: "leave type"
steps:
  - Create the leave request
tool_calls:
  - tool: hr_create_leave_request
    args:
      employee_id: "{{employee_id}}"
      start_date: "{{start_date}}"
      end_date: "{{end_date}}"
      status: "{{status}}"
      type: "{{type}}"
---

# New leave request

One-step HR procedure that registers a leave request. HR-only skill.
