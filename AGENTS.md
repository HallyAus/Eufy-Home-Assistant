## Canonical GitHub source and self-hosted Actions (2026-10-22)

GitHub `https://github.com/HallyAus/Eufy-Home-Assistant` is authoritative for source, commits and releases. Push to the verified GitHub remote and current project branch, not any retired internal forge or recovery mirror. Preserve local work and never force push. Use the configured self-hosted GitHub Actions runner where available, checking actual job labels, runner trust and workflow configuration. Keep existing release gates, production settings, credentials boundaries, rollback, stage/promotion and live validation. Neither a push nor green CI alone proves deployment. Historical source-control instructions below are superseded.

# Personal Eufy source and build route

The canonical source is GitHub HallyAus/Eufy-Home-Assistant. Preserve public Home Assistant/HACS download compatibility. Do not enable a release, public registry publication or production deployment merely by changing source-control instructions.

The separate personal runner preserves the original Python3.12/3.13/3.14
matrix, all tests including hardening regressions, runtime dependency imports,
Python compilation, shell syntax and Node24 JavaScript syntax. Its dependent
personal-oci job builds an amd64 candidate through the existing rootless mTLS
builder. Never add a Docker socket, privileged mode, public builder endpoint,
M4T access or credentials. Builder credentials remain repository-only; the
client certificate requires renewal before its90-day expiry.

The CI build context uses exact tracked GitHub bridge and add-on blobs. Its
temporary Dockerfile replaces only the reviewed source-fetch block with local
COPY; all other build steps remain identical. The shipped public Dockerfile
retains its immutable GitHub bridge commit for installed add-on compatibility.
At future releases, verify that immutable bridge source and its matching GitHub source objects before updating its pin. Never silently switch a
public user's build to a private LAN URL.

After complete existing gates and exact-source OCI checks, any private package publish still requires explicit verification of the current registry destination and repository-only credentials. Never move latest/public tags automatically. Preserve owner, source, blob-hash, repository-link and anonymous-access checks. Renew time-limited client certificates before their existing expiry. Never infer tokens from masked fields.

No Eufy login, camera stream, account write, Home Assistant installation/restart
or public registry/release publication is a CI test.
Never record real camera/account credentials, identifiers, media or household
configuration in source, tests, logs or artifacts. Existing public source and
release consumers must remain functional before retiring any working route.
