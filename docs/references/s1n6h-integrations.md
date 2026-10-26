# Reviewed repositories and native integrations

Reviewed October 25, 2026 using the author's public GitHub repository catalog:
https://github.com/S1N6H?tab=repositories

- **pentest-harness:** engagement methodology, durable tasks and evidence-led
  reporting. Native implementation in our existing engine, with explicit scope.
- **Bug-Bounty-Skills:** topic discovery for our 13 original local review
  playbooks, including authorization, XSS, SSRF, injection, business logic,
  concurrency and GraphQL. Its aggregated content has mixed upstream licenses;
  we do not download or execute its prompt collection.
- **Pentest-AI-BurpSuite / DeepSeek-Pentest-AI:** the proxy-history workflow
  informed bounded HAR and Burp XML metadata imports. Request credentials,
  query values, cookies and response bodies are discarded. No JAR is loaded.
- **S1N6H-Recon:** staged asset inventory informed scope-first reconnaissance
  and static entry-point review. No bulk scanner shell script is executed.
- **Security.txt-Checker:** disclosure metadata checking is available inside
  the existing authorized HTTP tool. It checks Contact/Expires metadata against
  RFC 9116; it does not treat a disclosure file as permission to test a target.

These are original integrations of relevant workflow ideas, not a claim that
all repositories or their executable tooling have been embedded. References:

https://github.com/S1N6H/Bug-Bounty-Skills
https://github.com/S1N6H/Pentest-AI-BurpSuite
https://github.com/S1N6H/DeepSeek-Pentest-AI
https://github.com/S1N6H/S1N6H-Recon
https://github.com/S1N6H/Security.txt-Checker
https://www.rfc-editor.org/rfc/rfc9116
https://owasp.org/projects/web-security-testing-guide
