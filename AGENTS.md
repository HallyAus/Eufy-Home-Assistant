## GitHub source control and self-hosted runner (current policy, 2026-10-22)

- **Canonical repository:** `https://github.com/HallyAus/Eufy-Home-Assistant`. GitHub is the source of truth. Commit, push, open pull requests and publish tags/releases to GitHub; do not publish to former Git servers or maintain a secondary recovery mirror.
- **Branch and remote:** Use the repository's current GitHub default/production branch, verify `git remote -v` before pushing, preserve uncommitted work and never force-push over newer commits.
- **CI/CD:** A self-hosted GitHub Actions runner is available. Prefer it for eligible jobs when the repository has a configured workflow; use the exact runner labels verified in repository settings or existing workflows. Do not assume the runner's OS, permissions, online status or available secrets, and do not expose credentials to untrusted contributions.
- **Validation and deployment:** Keep the project's existing tests, security checks, branch protections, release approvals, staging/acceptance and production verification. A GitHub push or green build is not proof of production deployment. Do not silently enable or bypass deployment triggers, and never infer that Vercel, Proxmox or another target deploys automatically.
- **Precedence:** This section overrides older hosting, mirror, disabled-Actions and publishing instructions elsewhere in this file. Preserve non-hosting project-specific safety rules.

# Personal Eufy source and build route

The separate personal runner preserves the original Python3.12/3.13/3.14
matrix, all tests including hardening regressions, runtime dependency imports,
Python compilation, shell syntax and Node24 JavaScript syntax. Its dependent
personal-oci job builds an amd64 candidate through the existing rootless mTLS
builder. Never add a Docker socket, privileged mode, public builder endpoint,
M4T access or credentials. Builder credentials remain repository-only; the
client certificate requires renewal before its90-day expiry.

The CI build context uses exact tracked VM300 bridge and add-on blobs. Its
temporary Dockerfile replaces only the reviewed source-fetch block with local
COPY; all other build steps remain identical. The shipped public Dockerfile
retains its immutable GitHub bridge commit for installed add-on compatibility.
At future releases, verify that immutable bridge source and its matching
primary/recovery objects before updating its pin. Never silently switch a
public user's build to a private LAN URL.

No Eufy login, camera stream, account write, Home Assistant installation/restart
or public registry/release publication is a CI test.
Never record real camera/account credentials, identifiers, media or household
configuration in source, tests, logs or artifacts. Existing public source and
release consumers must remain functional before retiring any working route.
