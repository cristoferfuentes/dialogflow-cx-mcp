name: Pull request
description: Propose a change to the MCP
body:
  - type: markdown
    attributes:
      value: |
        Thanks! Before sending:

        - `pytest` passes (no network needed)
        - `ruff check cxp server.py tests` is clean
        - `smoke_test.py` still works if you touched a tool
        - No credentials, project ids or agent ids in the diff
        - If you claim something about the API, it comes from a **real execution**

  - type: textarea
    id: what
    attributes:
      label: What does this change?
    validations:
      required: true

  - type: textarea
    id: measurement
    attributes:
      label: How did you measure it?
      description: |
        For any claim about the API's behaviour: the probe you ran, the exact error,
        the exact response. If it is not a measurement, say so.
    validations:
      required: false

  - type: checkboxes
    id: checklist
    attributes:
      label: Checklist
      options:
        - label: `pytest` passes
        - label: `ruff check cxp server.py tests` is clean
        - label: `smoke_test.py` passes (if a tool was touched)
        - label: The unit tests do not touch the network
        - label: No credentials, real project ids or real agent ids in the diff
        - label: The README and the docstrings are updated if the behaviour changed
