"""Original local review playbooks informed by public methodology, never downloaded instructions."""
from local_coder.tools.base import Tool
from local_coder.types import ToolName, ToolResult
import json

# Curated topics from S1N6H's catalog/recon/proxy workflows, expressed as our own
# evidence/refutation checklists. No third-party prompt or executable is loaded.
_TOPICS = [
 ('authorization','Object and function authorization','Identify tenant/user-controlled identifiers and privileged operations.','Trace identity to the resource ownership or policy check at each entry point.','Compare two synthetic users against the same fixture resource.','Rule out equivalent upstream middleware checks and intentionally public data.'),
 ('injection','SQL and command injection','Map request parameters through interpolation, assignments and wrappers to execution sinks.','Distinguish SQL binding and argv arrays from dynamic query text or shell evaluation.','Use a local mocked sink or failing regression that proves attacker input crosses the boundary.','Do not label constant SQL or shell=False with a fixed argv array as injection.'),
 ('ssrf','Server-side request forgery','Find outbound requests whose URL or hostname derives from input.','Inspect address validation, redirect handling, DNS pinning and transport proxies.','Use a disposable loopback service to test denied destinations without contacting metadata services.','Rule out fixed allowlisted endpoints and validated, pinned transports.'),
 ('xss','Cross-site scripting','Trace untrusted text into HTML, DOM, template and script-context sinks.','Identify the exact output context and required encoding; sanitization must match that context.','Use a local DOM/unit fixture to demonstrate interpretation rather than merely reflected text.','textContent and correctly escaped template output are negative evidence; CSP alone is not proof of safety.'),
 ('deserialization','Unsafe deserialization','Inventory pickle, unsafe YAML and dynamic object loaders reachable from untrusted bytes.','Trace the trust boundary and loader configuration, including imported aliases.','Demonstrate rejection with a harmless local fixture and a stubbed effect.','SafeLoader, fixed trusted cache data and unreachable debug code need separate treatment.'),
 ('file_upload','File upload and archive extraction','Inspect extension, content, destination and archive-member validation.','Follow extracted paths and symlink handling to writes or subsequent execution.','Test traversal and type confusion using temporary directories and inert files.','A suspicious filename alone is not proof; verify resolved destination and serving behavior.'),
 ('path_traversal','Path traversal and file disclosure','Find user-controlled paths used for reads, writes, downloads and static routing.','Resolve normalization, symlink and directory-boundary behavior before the file operation.','Use a synthetic outside file and assert it cannot be read or modified.','String-prefix checks are not directory containment; consider platform-specific separators.'),
 ('csrf','Cross-site request forgery','Locate state-changing actions authenticated by ambient browser credentials.','Inspect CSRF token validation, Origin checks, SameSite behavior and content-type requirements.','Exercise a local request fixture with absent and invalid anti-CSRF proof.','Bearer-only endpoints without ambient credentials need a different threat model.'),
 ('business_logic','Business logic and workflow invariants','Write the invariant, state transitions, actor privileges and replay assumptions first.','Inspect validation order, idempotency, units, limits and cancellation paths.','Use deterministic local sequences that violate exactly one invariant.','Separate intended policy exceptions from an actually reachable invariant violation.'),
 ('race_conditions','Race conditions and double execution','Identify read-check-write sequences and operations that can be retried concurrently.','Inspect database transactions, unique constraints, locks and durable idempotency keys.','Use barriers in a synthetic concurrency test rather than timing-only sleeps.','One failed stress run or a missing application lock is not proof when database constraints enforce the invariant.'),
 ('graphql','GraphQL authorization and resource limits','Inventory resolver-level object access, field access, aliases and nested fan-out.','Inspect shared loaders, per-request identity, query depth and complexity limits.','Use a local resolver fixture for cross-user field access and bounded query cost.','Introspection by itself does not establish unauthorized access.'),
 ('cloud_identity','Cloud identity and configuration','Trace workload identity, role delegation and public resource permissions in local IaC.','Inspect action/resource/condition intersections and who can assume the role.','Use configuration evaluation or a policy simulator fixture; do not access cloud accounts.','Wildcard text without effective permissions and trust context is only a candidate.'),
 ('prompt_injection','AI prompt injection boundaries','Inventory untrusted document, retrieval and tool-output inputs reaching an agent.','Trace instruction/data separation, tool permissions, secret access and human approvals.','Use a local synthetic tool and inert canary to test whether external text can grant capabilities.','A model repeating adversarial text is not proof of unauthorized tool execution.'),
]
PLAYBOOKS = {row[0]: {'id':row[0], 'title':row[1], 'steps':list(row[2:]),
    'evidence_required':['Affected entry point and exact source location','Source-to-sink or state-transition trace','Controls inspected and ruled-out hypotheses','Local reproduction result, or explicitly untested status','Minimal remediation and verification plan'],
    'verification':'Unverified until a local reproduction establishes the claimed behavior',
    'reference':'https://owasp.org/www-project-web-security-testing-guide/'} for row in _TOPICS}


class SecurityPlaybookTool(Tool):
    name = ToolName.SECURITY_PLAYBOOK
    description = 'List or read local exploit-detection review playbooks with evidence requirements and false-positive checks. These are methodology, not findings or executable payloads.'
    parameters = {'type':'object','properties':{'id':{'type':'string','enum':['',*PLAYBOOKS]}},'additionalProperties':False}

    async def execute(self, id='', **kwargs):
        if id:
            if id not in PLAYBOOKS:
                return ToolResult(success=False,error='Unknown security playbook')
            return ToolResult(success=True,output=json.dumps(PLAYBOOKS[id]),data=PLAYBOOKS[id])
        result = {'playbooks':[{'id':value['id'],'title':value['title']} for value in PLAYBOOKS.values()]}
        return ToolResult(success=True,output=json.dumps(result),data=result)
