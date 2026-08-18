# Triage labels

Use these labels as the repository's standard triage workflow.

| Label | Meaning |
| --- | --- |
| `needs-triage` | The request exists but its validity, scope, or owner has not been established. |
| `needs-info` | Progress requires information or a decision not currently available. |
| `ready-for-agent` | Scope and acceptance criteria are clear enough for autonomous implementation. |
| `ready-for-human` | Work requires a human-only action, review, credential, login, or product decision. |
| `wontfix` | The request was deliberately declined or superseded; the reason must be documented. |

## Transitions

- New issues normally begin with `needs-triage`.
- Use `needs-info` only when the missing information materially changes the work.
- Move to `ready-for-agent` after the specification and acceptance criteria are actionable.
- Use `ready-for-human` for interactive login and other human-only boundaries.
- Apply `wontfix` only with a closing explanation.
- Remove obsolete workflow labels when applying the next state.
