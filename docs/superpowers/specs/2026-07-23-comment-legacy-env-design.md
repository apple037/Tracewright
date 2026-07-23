# Comment Legacy Environment Variables

## Goal

Make the current `.env` unambiguous for Agent Flow without deleting potentially
useful legacy values.

## Change

- Comment every currently active legacy environment assignment.
- Preserve each key and value exactly.
- Add a short header explaining that the block belongs to the previous runtime
  and is not consumed by Agent Flow.
- Do not add, rename, copy, or activate any Agent Flow setting in this change.

## Safety and Verification

- `.env` remains ignored and must not be committed or printed with secrets.
- After editing, verify that the file contains no active assignments.
- Verify that all 30 original keys remain present as comments.
- No application tests are required because this change intentionally leaves
  Agent Flow without active `.env` configuration.
