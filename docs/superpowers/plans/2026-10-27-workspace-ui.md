# Clin: focused desktop workspace and interaction upgrades

Design: Quiet charcoal workspace matching the supplied Antigravity reference,
with a compact 244px sidebar, a readable central composer and optional right
inspector. Base #171819, sidebar #1d1f21, surface #25272a, border #36383c,
text #ececea, muted #a4a5aa; restrained periwinkle #b5b9ed for selection/focus.
Use the installed system sans stack at readable 13–15px and system monospace
for code only. No decorative gradients, marketing dashboard or invented metrics.
Keep typography, whitespace and clear information hierarchy as the main design.

Task flow: sidebar → focused composer → plan/run → readable report + actual
activity/plan/artifact inspector → workspace changes review. All advanced existing
views remain reachable in a collapsible workbench section and command palette.

- [x] Rebuild shell, grouped navigation, home composer, status and responsive tokens.
- [x] Add keyboard command palette and file-context selection from known files.
- [x] Add searchable/filterable pinned task inbox with real status counts.
- [x] Render reports as safe bounded Markdown with code copy and export.
- [x] Add run inspector with observed plans, activity, report artifacts and context.
- [x] Add explicit plan-to-implementation draft action and useful starter prompts.
- [x] Verify frontend tests/build/lint, local browser keyboard flows and screenshots.
- [x] Package, push and update installed apps with rollback.

Constraints: no new dependencies, paid model calls or fabricated background work;
no approval/scope/budget changes. Persist pinned IDs and display preferences only,
isolated per project; do not persist prompt drafts or credentials. Preserve other
checkout edits. No automatic generation when a starter or palette action is used.

Workflow references:
- https://code.claude.com/docs/en/common-workflows
- https://antigravity.google/docs/artifacts
- https://openai.com/index/introducing-the-codex-app/

Delivery: Clin implementation commit d24d3e4 pushed to PR #22. Installed the
Linux manager and IDE engine/extension, matching icons and display metadata;
retained rollback at updates/d24d3e4. Existing profile, application identities
and root-owned mode 4755 Chromium helpers remain compatible.

Validation: 1,464 Python tests passed (3 optional skips), 74 native Node tests,
26 frontend tests, frontend build/lint, PyInstaller, offline Linux Electron
packaging, packaged authenticated engine smoke and actual installed IDE
extension-host smoke passed. Live manager window_ready and authenticated
engine routes verified after launching both apps. Browser fixture exercised
keyboard palette/file attachments, inbox search/filter/pins/reload, code copy,
plan handoff with actual plan content, inspector tab keyboard controls,
compact sidebar and existing Doctor access. Preview uses synthetic tasks.

Follow-up limits: brand availability is unverified; Windows/macOS native
packaging was not run locally. No paid inference or external targets tested.
