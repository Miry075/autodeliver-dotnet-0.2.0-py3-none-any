# Azure DevOps setup (once per organisation, then once per repository)

## Once: the tool repository
1. Push the autodeliver source to an Azure Repos repository, for example `<Project>/autodeliver-dotnet`.
2. Create its CI pipeline from `azure-pipelines/ci.yml`. It tests on Windows and Linux and builds the wheel, `.skill` and `.plugin`.
3. Tag releases `v<version>` matching `src/autodeliver/__init__.py`. Consumers pin these tags.
4. Grant consuming pipelines read access to this repository: Project settings → Repositories → autodeliver-dotnet → Security → `<Project> Build Service` → Read. If the consumer is in another project, also disable "Limit job authorization scope to current project" for that project, or keep the tool repo in the same project.

## Per repository
1. `autodeliver init` and commit `.autodeliver/config.yml` and `.azuredevops/autodeliver.yml`.
2. Pipelines → New pipeline → Existing YAML → `.azuredevops/autodeliver.yml`. On the first run, authorise access to the `autodeliver` repository resource.
3. Library → variable group `autodeliver-secrets`: **one** Claude credential as a secret. `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`, uses your Claude subscription) only for your own repositories and tickets; `ANTHROPIC_API_KEY` (Claude Console) for any shared or company pipeline. If both exist, the API key is used. Add `SONAR_TOKEN` (secret) if `sonar.enabled`. Authorise the pipeline to use the group.
4. Build identity (`<Project> Build Service (<org>)`) on the target repository: allow **Contribute**, **Create branch**, **Contribute to pull requests**. Deny **Bypass policies when pushing**, **Bypass policies when completing pull requests** and **Force push**. On the area path: **Edit work items in this node**.
5. Branch policy on the target branch: minimum 1 reviewer, with "Prohibit the most recent pusher from approving" and "Allow requestors to approve their own changes" off. Add build validation and "Check for linked work items".
6. Agent network egress: allow Anthropic API, NuGet feeds, GitHub releases (gitleaks download) and SonarCloud. Agent-written tests run on this machine. Pipeline secrets are stripped from their environment, but the Claude credential the CLI needs (API key or subscription token) cannot be.
7. Private NuGet feeds: add a `NuGetAuthenticate@1` step before the template job if restore needs credentials.
8. Tag a small, well-specified ticket `auto-deliver` and queue the pipeline with `dryRun: true` first.

## Local use
- Local live dry run: set `AZURE_DEVOPS_PAT` (Work Items: Read scope is enough for a dry run). Claude: with neither `ANTHROPIC_API_KEY` nor `CLAUDE_CODE_OAUTH_TOKEN` set, the run uses the Claude Code login on your machine (run `claude` once and `/login` with your Pro/Max account). `autodeliver doctor` shows which credential will be used.
- Windows: run from PowerShell or Windows Terminal. Claude Code's Bash tool uses Git for Windows, so Git must be installed (it is anyway).
