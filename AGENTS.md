# Personal Eufy source and build route

Origin is private VM300 hallyaus/Eufy-Home-Assistant, Git SSH2222. Keep the
public HallyAus GitHub copy as identical recovery and Home Assistant/HACS
download compatibility. GitHub Actions remain disabled. Push completed source
to VM300 first; routine recovery pushes must not build or deploy.

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

After the complete original gates and exact-source OCI checks, CI may publish
only immutable source-SHA candidates to the private VM300 package registry.
Its dedicated PERSONAL_PACKAGE_TOKEN is repository-only (package write/user
read), and the publisher must verify the private owner, source, all blob hashes,
repository link and anonymous denial. It never moves latest or public tags.
The server token has no automatic expiry; rotate/revoke it before the CI use
deadline16January2027UTC. Never infer its value from a masked secret field.

No Eufy login, camera stream, account write, Home Assistant installation/restart
or public registry/release publication is a CI test.
Never record real camera/account credentials, identifiers, media or household
configuration in source, tests, logs or artifacts. Existing public source and
release consumers must remain functional before retiring any working route.
