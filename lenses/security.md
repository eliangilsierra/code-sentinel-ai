# Lens: security

Find weaknesses that an attacker can exploit through the changed code. Trace the data or the
control flow from an attacker-controlled source to the dangerous operation before you report.

## What to look for

- Injection: user input reaching a query, shell command, file path, template, expression
  language, log format or deserializer without parameterisation, escaping or validation.
- Authentication and authorization: an endpoint, handler or action that skips the checks its
  siblings apply, a check on the wrong object, a client-supplied identifier trusted without an
  ownership or tenant check, privilege granted by a request field.
- Secrets: credentials, tokens or keys added to code or configuration; secrets written to logs,
  errors or responses.
- Cross-site and request forgery: unescaped output in a browser context, raw HTML insertion,
  state-changing requests without the framework's forgery protection.
- Server-side request forgery and redirects: a URL from the request fetched or redirected to
  without an allowlist.
- Cryptography: weak or home-made algorithms, fixed or predictable keys, IVs or salts, missing
  verification of signatures or certificates, non-constant-time comparison of secrets.
- Exposure: sensitive data returned or logged, permissive cross-origin or file permissions,
  debug features enabled, security headers or cookie flags removed.
- Unsafe resource use driven by input: unbounded loops, allocations, regular expressions or
  uploads that let one request exhaust the service.

## Before you report

1. Identify the source the attacker controls and the sink that becomes dangerous.
2. Search for the framework guarantee that already protects the sink: parameterised queries in
   the ORM, automatic output escaping, default forgery protection, a global filter or security
   configuration, validation earlier in the call chain.
3. If the protection applies, the finding is void. If you cannot show the unprotected path, drop it.
4. State the trigger as `attacker input -> impact`, for example `id of another tenant -> read of
   its orders`.

## Severity

- `important`: exploitable by a remote or authenticated attacker with real impact.
- `nit`: hardening with a limited or unlikely impact.
- `pre_existing`: an old weakness that the change now exposes to new callers.

## Do not report

- Generic advice such as "validate input" or "use HTTPS" without a concrete path.
- Findings that a dependency scanner, secret scanner or static analysis already reports.
- Test fixtures, examples and local development settings unless they ship to production.
