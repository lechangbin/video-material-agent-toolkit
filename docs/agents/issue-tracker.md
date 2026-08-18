# Issue tracker

This repository uses GitHub Issues for implementation tickets and specifications.
Use the GitHub CLI (`gh`) for issue operations.

Pull requests are not used as the primary request surface. Work should begin from
an issue that records the expected outcome and acceptance criteria.

## Commands

View an issue:

```powershell
gh issue view <number>
```

List open issues:

```powershell
gh issue list --state open
```

Create an issue:

```powershell
gh issue create --title "<title>" --body-file "<path>"
```

Add a comment:

```powershell
gh issue comment <number> --body-file "<path>"
```

Apply or remove labels:

```powershell
gh issue edit <number> --add-label "<label>"
gh issue edit <number> --remove-label "<label>"
```

Close an issue:

```powershell
gh issue close <number> --comment "<reason>"
```

## Conventions

- “Publish this spec” means create a GitHub Issue.
- “Fetch ticket” means read it with `gh issue view`.
- Keep the issue body authoritative for scope, decisions, and acceptance criteria.
- Use comments for progress, investigation evidence, and changes that do not yet
  justify rewriting the specification.
- Update the issue body when an accepted decision changes the contract.
- Record blockers explicitly instead of silently broadening scope.
- Link related issues and use GitHub sub-issues or dependencies when one issue
  is decomposed into independently deliverable work.
- Close an issue only after its acceptance criteria have been verified, or with
  a documented reason explaining why it will not be implemented.
