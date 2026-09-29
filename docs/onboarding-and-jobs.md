# Candidate onboarding and Keryx intake

The runtime can now import real Keryx listings, search them locally, collect a real
candidate draft, and rank jobs against an approved profile. The synthetic browser
fixture remains a separate test. Discovery never creates an application or submits one.

## Job source

The owner explicitly selects [GodlyDonuts/keryx](https://github.com/GodlyDonuts/keryx).
`autopilot jobs sync` resolves one immutable upstream commit, downloads its schema-2
US snapshot from the fixed GitHub source, validates it, and imports it transactionally.
The snapshot, checksum and source revision remain in private local storage.

Keryx includes both open and closed jobs. Search returns only current open records.
Stable source IDs prevent duplicates; changed, closed and missing records update the
local catalog without deleting the history. Invalid, empty or duplicate-ID snapshots
leave the previous catalog intact. Search terms and candidate facts never leave the Mac.

```sh
uv run autopilot jobs sync
uv run autopilot jobs status
uv run autopilot jobs search --query software --program internship --limit 5
uv run autopilot jobs read --id job_synthetic_example
uv run autopilot jobs matches --limit 10
```

Match batches support 1–25 listings. General keyword search is paged in groups of
up to 20 using `--offset`.

The last command requires a reviewed profile. A local owner may inspect preliminary
results with `--preview-draft`; that flag is unavailable to the model-facing matching
tool. Matching uses explicit title/program preferences and exclusions. It reports
unknown schedules, source graduation conflicts and missing employer links as review
items. It does not claim eligibility, verify a job is still open on the employer site,
or silently equate a preferred qualification with a requirement. Scores are deterministic
discovery priorities, not probabilities of acceptance. Unimplemented criteria such as
full compensation, industry and work-style analysis still require posting review.

Importing the source alone does not enable notifications or applications. The explicit
[application workflow](application-workflow.md) configuration adds scheduled refresh,
notification delivery, and queued preparation. Erga's own optional Keryx cache
can remain disabled: Autopilot owns this intake catalog and avoids a second download.

## Resumable interview

Eight fixed sections cover identity, education, eligibility, availability, preferences,
career evidence, stories and application policies. Answers may be null. An unanswered
GPA, graduation date, weekly-hours commitment or authorization question stays unknown.
Never change an academic classification or applicant fact to satisfy a posting.

The interview should cover:

1. Roles, excluded roles, internship/new-grad scope, terms, locations and compensation.
2. Exact current degree, school, graduation date, enrollment and GPA disclosure.
3. Work authorization, sponsorship, availability, co-op leave and relocation constraints.
4. Legal/preferred names, contact details, public links and the current factual resume.
5. Project ownership, employment dates, metrics, awards, skills and existing applications.
6. Motivation, teamwork, leadership, learning stories and writing preferences.
7. Optional demographic choices, unknown answers, account creation and writing review.
8. Review volume, notifications, quiet hours, exclusions and final factual review.

Questions should build on the resume and previous answers, not ask the same thing
repeatedly. Conflicts must be surfaced before approval. Identity documents, full SSNs,
banking details, credentials and authentication codes do not belong in this interview.

```sh
uv run autopilot onboarding status
uv run autopilot onboarding status --section education
uv run autopilot onboarding show
uv run autopilot onboarding propose --section education \
  --file /private/reviewed-education.json --expected-hash REVIEWED_DRAFT_HASH
uv run autopilot onboarding approve --expected-hash REVIEWED_DRAFT_HASH
```

`propose` replaces one complete section, validates its schema, checks the previous
draft hash, and records a resumable checkpoint. It cannot approve any facts. Its
callers must preserve existing values when changing a section. The private draft is
outside Git; SQLite records revisions/events and unresolved evidence conflicts.

`approve` is a local owner operation after review of that exact draft. It is never
an MCP tool. It rejects stale hashes and unresolved conflicts, creates an immutable
profile snapshot, and writes the structured canonical `Erga Autopilot/Profile/Candidate.md`
note in the configured Obsidian vault. A partial reviewed profile still reports its
missing fields; approval does not invent them or enable submission. Before use, both
snapshot and canonical note are validated against the approved hash. Manual edits
to the note block use until reviewed. Free-form prose below the frontmatter is not
an application fact.

Configure `OBSIDIAN_VAULT_PATH` in the process environment or `obsidian_vault_path` in
the private `config/recruiting.json`. No real path, answer, resume or source transcript
belongs in Git. QMD remains a derived retrieval index, never the approval authority.

## Approved evidence and memory

Keep real Erga career state at `erga/config.toml` under the private runtime root,
separate from `synthetic/erga/config.toml`. Import the owner-reviewed factual master
through Erga's supported interface. `read_career_evidence` calls only `list_evidence`
and returns at most three approved excerpts, each capped at 6,000 characters. Its
internal client uses Erga's `career-private` profile because the upstream `read` and
`career` profiles deliberately withhold managed master-resume records. Hermes never
receives that broader Erga tool inventory. No generic path or config selector is exposed.

After approving a profile, build its local retrieval copy:

```sh
uv run autopilot memory index
uv run autopilot memory search --query "Example project"
```

This writes `Erga Autopilot/Retrieval/Approved profile.md` in the vault, registers only
that file in the `erga-candidate` QMD index / `approved-profile` collection, and indexes
it for keyword search. It does not index drafts, research, unrelated vault notes or
the synthetic candidate. The canonical profile remains `Profile/Candidate.md`.
After each approved change, rerun the index command. Retrieval rejects a stale
profile version, modified projection or invalid canonical profile before querying.
The model can search this copy but cannot approve or rebuild it. Snippets guide
retrieval; `read_candidate_section` supplies authoritative structured answers.

## Hermes connection

Use these nine narrow tools as the production Autopilot MCP include list:

- `search_job_feed`, `read_job_listing`, `job_feed_status`
- `review_job_matches`
- `get_onboarding_status`, `propose_onboarding_section`
- `read_candidate_section`, `read_career_evidence`, `retrieve_candidate_memory`

Restart the Hermes gateway to refresh its tool inventory. Keep built-in shell,
unrestricted browser and generic file tools disabled. The owner/channel allowlist
still applies. Real onboarding and feed data must not be confused with Alex Example
or any other synthetic fixture. Source listings and imported documents cannot grant
approval or alter the tool permissions.

Remove synthetic tools from the production include list. Set `tools.resources: false`
and `tools.prompts: false` inside this MCP server's config: excluding their names alone
does not disable Hermes' generated resource/prompt wrappers. Check the actual agent
tool list, not only the configured include list. Update the system prompt to describe
real profile/job review; do not retain the certification-only synthetic instructions.

The nine tools above support onboarding, discovery and evidence retrieval. Additional
workflow tools are documented in [Application workflow](application-workflow.md);
installations must explicitly include them and refresh the Hermes session. A successful
imported master or layout check must not be reported as successful role-specific tailoring.
