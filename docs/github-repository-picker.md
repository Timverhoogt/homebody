# GitHub repository picker

Use **Agent → Workspace** (the large mode button beside Everyday). Successful mode activation opens **Conversation & projects**; this lower selector only changes the view, not tool authority. The project card also has **Enable Workspace**, which invokes the same existing mode action. Then choose **Show conversation**, a GitHub repository, and **Open project** where a matching session is configured.

Repositories are listed through the bridge host's existing `gh` login for `github.com`, never a copied browser/robot token. Enable on the host only:

```sh
REACHY_GITHUB_OWNER=your-github-login
REACHY_GITHUB_PROJECT_MAP_JSON='{"your-github-login/repository":"registered-project-id"}'
```

The map is explicit owner-managed metadata. Matching native targets must already exist in `REACHY_NATIVE_TARGETS_JSON` and their project roots in `REACHY_AGENT_PROJECTS_JSON`. The picker cannot clone, create sessions, register roots, submit work, enable execution, or choose arbitrary paths. Unconfigured repositories remain selectable in the list, with a clear setup-needed explanation and disabled Open project/Review controls. Selecting another repository cannot submit a request to the previously bound project by mistake. Existing Stop control remains separate.

`repositories` is a paired-owner + CSRF native action requiring an adult Workspace profile, current runtime generation and active RAM display lease. Bridge-side authorization is checked even when no native targets exist, and rechecked after the GitHub query. No repository metadata is added to public status, Kids status, audit bodies or browser storage. Reset/owner loss/clear removes it and fences late responses.

Metadata includes exact repository name, private/archive markers, verified GitHub URL and matching configured target IDs, not descriptions or source contents. A fixed read-only `gh repo list OWNER --limit 500 --json nameWithOwner,isPrivate,isArchived` call uses the host's saved login and a 15-second deadline. Exported work-host/token overrides are excluded. The host caches for two minutes; the UI shows the actual checked time and warns at the 500-result cap. Reload may use that cache. Failures clear metadata and report unavailability, without subprocess output or old results masquerading as success.

Native discussion uses the worker's existing configured tools; phone approvals gate planned work, not every native discussion tool. Repository listing does not change that authority or make the working directory a sandbox.
