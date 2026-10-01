# Onboarding and jobs

Two things have to exist before Rove can work on applications: a local catalog of jobs and an approved candidate profile. This page covers both, the retrieval index built from the profile, and the tools the Hermes agent receives.

Importing jobs and approving a profile never creates or submits an application. Queueing is described in [Application workflow](application-workflow.md).

## Job source

The job catalog comes from [GodlyDonuts/keryx](https://github.com/GodlyDonuts/keryx). `rove jobs sync` resolves the repository's current commit, downloads the schema-2 US snapshot for that exact commit from GitHub, validates it, and imports it in one transaction. The snapshot, its checksum and the source revision stay in private local storage. When the commit has not changed, nothing is downloaded.

Keryx lists open and closed jobs, and search returns only open ones. Stable source IDs prevent duplicates. Changed, closed and missing records update the catalog without deleting history. An invalid, empty or duplicate-ID snapshot leaves the previous catalog intact. Search terms and candidate facts never leave the Mac.

```sh
uv run rove jobs sync
uv run rove jobs status
uv run rove jobs search --query software --program internship --limit 5
uv run rove jobs read --id job_synthetic_example
uv run rove jobs matches --limit 10
```

A match batch holds 1 to 25 listings. Keyword search is paged in groups of up to 20 with `--offset`.

`jobs matches` needs an approved profile. A local owner can inspect preliminary results with `--preview-draft`, and that flag is not available to the model-facing tool. Matching uses the profile's title keywords, programs and exclusions. It reports unknown schedules, graduation conflicts in the source data and missing employer links as items to review. It does not claim eligibility, does not check that a job is still open on the employer's site, and does not treat a preferred qualification as a requirement. The scores are deterministic discovery priorities, not chances of acceptance.

Erga has its own optional Keryx cache. It can stay disabled, since Rove owns this catalog.

## The onboarding interview

The profile has eight fixed sections: identity, education, eligibility, availability, preferences, evidence, stories and application policy. An answer may be null. An unanswered GPA, graduation date, weekly-hours commitment or authorization question stays unknown. An academic classification or applicant fact is never changed to satisfy a posting.

The interview should cover:

1. Roles, excluded roles, internship or new-grad scope, terms, locations and compensation.
2. The exact current degree, school, graduation date, enrollment and GPA disclosure.
3. Work authorization, sponsorship, availability, co-op leave and relocation constraints.
4. Legal and preferred names, contact details, public links and the current factual resume.
5. Project ownership, employment dates, metrics, awards, skills and existing applications.
6. Motivation, teamwork, leadership, learning stories and writing preferences.
7. Optional demographic choices, unknown answers, account creation and writing review.
8. Review volume, notifications, quiet hours, exclusions and a final factual review.

Questions should build on the resume and earlier answers. Conflicts are surfaced before approval. Identity documents, full SSNs, banking details, credentials and authentication codes do not belong in the interview.

```sh
uv run rove onboarding status
uv run rove onboarding status --section education
uv run rove onboarding show
uv run rove onboarding propose --section education \
  --file /private/reviewed-education.json --expected-hash REVIEWED_DRAFT_HASH
uv run rove onboarding approve --expected-hash REVIEWED_DRAFT_HASH
```

`propose` replaces one complete section, validates it against the schema, checks the previous draft hash, and records a checkpoint so an interrupted interview can resume. It cannot approve anything. A caller that changes a section must keep the values it is not changing. The draft is a private file, and SQLite records revisions, events and unresolved evidence conflicts.

`approve` is a local owner operation on the exact draft that was reviewed. It is never an MCP tool. It rejects a stale hash and unresolved conflicts, writes an immutable profile snapshot, and writes the canonical `Rove/Profile/Candidate.md` note in the configured vault. A partial profile still reports its missing fields. Approval does not invent them and does not enable submission.

Before every use, the snapshot and the canonical note are validated against the approved hash. A manual edit to the note blocks use until it is reviewed. Prose below the front matter is not an application fact.

Set the vault path with `OBSIDIAN_VAULT_PATH` in the shell, or `obsidian_vault_path` in private `config/recruiting.json`. See [Requirements](requirements.md#configrecruitingjson).

## Approved evidence and memory

Real Erga career state lives at `erga/config.toml` under the state root, separate from `synthetic/erga/config.toml`. Import the owner-reviewed factual master resume through Erga's own interface.

`read_career_evidence` calls only Erga's `list_evidence` and returns at most three approved excerpts of up to 6,000 characters each. Its internal client uses Erga's `career-private` profile, because the upstream `read` and `career` profiles withhold managed master-resume records. Hermes never receives that broader Erga tool inventory, and the adapter exposes no path or config selector.

After approving a profile, build its retrieval copy:

```sh
uv run rove memory index
uv run rove memory search --query "Example project"
```

This writes `Rove/Retrieval/Approved profile.md` in the vault, registers only that file in the `rove-candidate` QMD index and `approved-profile` collection, and indexes it for keyword search. Drafts, research, other vault notes and the synthetic candidate are not indexed. Run the index command again after each approved change.

Retrieval rejects a stale profile version, a modified copy or an invalid canonical profile before it queries. The model can search the copy and cannot approve or rebuild it. A snippet guides the agent to a section, and `read_candidate_section` supplies the authoritative answer.

## Hermes connection

The production include list for the Rove MCP server has thirteen tools. Nine cover onboarding, discovery and evidence:

- `search_job_feed`, `read_job_listing`, `job_feed_status`
- `review_job_matches`
- `get_onboarding_status`, `propose_onboarding_section`
- `read_candidate_section`, `read_career_evidence`, `retrieve_candidate_memory`

Four connect the [application workflow](application-workflow.md): `start_job_application`, `application_workflow_status`, `inspect_application_browser` and `refresh_job_feed`.

None of the thirteen fills a form, approves a fact or submits. The queue worker and the owner's Discord replies own those steps.

The server defines other tools as well: four synthetic certification tools and three direct browser tools. Leave them off the production list. The Hermes settings that enforce the list, and the toolset name `mcp-rove`, are in [Local runtime](local-runtime.md#the-rove-mcp-server-in-hermes).

Restart the Hermes gateway to refresh its tool inventory, and check the agent's actual tool list afterward. Keep Hermes' built-in shell, unrestricted browser and generic file tools disabled. The owner and channel allowlist still applies.

The system prompt should describe real profile and job review. Remove the certification-only synthetic instructions, and keep synthetic fixtures such as Alex Example out of real onboarding. A source listing or an imported document cannot grant approval or change the tool permissions. A successful master import or layout check is not role-specific tailoring and must not be reported as such.
