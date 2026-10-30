# Changelog

## 0.3.0

### Claude subscription login
- Runs on your Claude Pro/Max subscription: with no credential variable set, the Claude Code login on the machine is used; in a pipeline, set `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`. `ANTHROPIC_API_KEY` still works and is required for shared or company pipelines (see README, "Claude credentials").
- `doctor` reports which credential a run will use, accepts either one in `--ci`, and warns when both are set (the API key wins).
- Undefined pipeline secrets (the literal `$(NAME)` Azure passes through) are removed at start-up instead of being tried as credentials.
- The pipeline template passes `CLAUDE_CODE_OAUTH_TOKEN` as well as `ANTHROPIC_API_KEY`.

### Security
- Tool policy: allowed `git`/`dotnet` commands could escape it through their arguments. Refused now: `git --output`, `--no-index`, `--ext-diff`, `--textconv`, `-O`; any path argument outside the repository (`dotnet build -o /tmp/x`, `--logger "trx;LogFilePath=/…"`, `../other.csproj`); any argument naming a NEVER_READ file (`git show HEAD:.env`, `git diff -- secrets.json`).
- Grep over a directory can no longer print matching lines (it could show the contents of NEVER_READ files); file names and counts are still allowed, line output only for a single checked file.
- `VSS_NUGET_ACCESSTOKEN` is now blanked in the agents' own shell, like the other pipeline secrets. `CLAUDE_CODE_OAUTH_TOKEN` and `ANTHROPIC_AUTH_TOKEN` are never passed to build, test, Stryker or Sonar.
- PR title, PR summary, the evidence-pack PR thread and escalation comments are PHI-scanned like the commit message; flagged text is withheld (the full text stays in the run record).

### Other
- No more pytest collection warning for `dotnet.TestResult`.
