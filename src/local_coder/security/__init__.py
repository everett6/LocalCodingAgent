"""Security review state that outlives one conversation.

- lessons: SECURITY_LESSONS.md, the reviewed, human-edited notes every review
  loads (false positives to suppress, confirmed findings, project patterns),
  plus the queue of lessons the agent proposed but nobody accepted yet.
- triage: stable fingerprints, cross-scanner dedupe, suppression and the
  new-since-last-review baseline for scanner findings.
- ledger: the structured findings a review records as it goes, kept outside
  the chat history so context compaction cannot lose them.

Everything here reads and writes files inside the project workspace only.
"""
