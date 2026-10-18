# Personal Gitea add-on build

Original exact-source runner454 passed:163 tests in each original Python
environment, static checks, rootless amd64 build and independent artifact/blob
verification. Its immutable candidate is now published privately at
`192.168.1.130:3000/hallyaus/eufy-home-assistant:source-a2be4020cb54eeab01471826fd4e23581a4b63e0`,
manifest `sha256:cffad2de5f53454306d4525a4c383f3dba7c5affa5bf948334b3328fcba4579f`.
The package is linked to the private repository; anonymous pulling was denied.
The temporary import credential was revoked and subsequently returned401.
This does not certify an installed device or a public release.

The primary is private VM300. The public GitHub recovery remains available to
existing Home Assistant add-on/HACS clients, with Actions disabled. This
change does not publish a public version, install an integration, change the public
add-on Dockerfile or open a camera stream.

The original three-version Python matrix and full tests/import/compile/shell
checks are preserved. The full test inventory includes the separate original
hardening workflow's four files. Node24 runs the original JavaScript syntax
checks. Only after those gates pass does the personal-oci job build the image.

The existing VM301 rootless BuildKit0.34.0 service uses its authenticated
Docker-bridge mTLS endpoint and isolated user/network/process sandbox. It has
one builder slot,2CPU/6GiB service limits,12GiB cache cap and5GiB free-space
floor. The personal runner has capacity one. No Docker socket, privileged
container, host bind mount, public registry push or runtime installation is added.

The repository-only BUILDKIT_CA/BUILDKIT_CLIENT_CERT/BUILDKIT_CLIENT_KEY hold
one dedicated90-day client for this repository. They transferred directly
through SSH processes without local credential files or displayed values.
The temporary setup PAT was revoked immediately; metadata verification
confirmed zero setup/dispatcher/builder setup PATs. The main branch permits
only hallyaus, with force pushes, deploy-key writers and bypass disabled.

Build contexts contain only regular tracked VM300 add-on/bridge files. A
hash-guarded transformation replaces the reviewed public Git fetch block with
local COPY, retaining all other Dockerfile steps. The public Dockerfile still
pins bridge commit2d34ded6425779853a91baf8ae8b5861647e5dea; its bridge tree
matches the current pre-migration main. Future source-fetch changes fail
closed until the transformation is reviewed.

The image is exported as a private linux/amd64 OCI candidate. Gitea retains
its checksum, exact source/tree/context receipt, BuildKit metadata and image
for3days. No camera/account credentials enter the build context or artifact.
The dependent build now publishes a verified immutable candidate to VM300 after
the unchanged original gates. It uses repository-only PERSONAL_PACKAGE_TOKEN,
with package-write/user-read scope, private owner and exact source checks,
checksum-pinned regctl0.11.6, all OCI blob verification and anonymous-denial
proof. Registry settings are per-command; no global Docker configuration or
Docker socket is added. The token is passed by stdin, and temporary client
authentication files are removed without being read or retained as artifacts.
The private publication receipt is retained with the existing candidate.

The Gitea token itself has no automatic expiry. The CI publisher refuses use
after16January2027UTC; rotate the dedicated service token and review that
deadline before then. Temporary setup access is revoked immediately. Token
values and authentication files remain unavailable to the agent.

The first automatic publication from the new workflow remains pending until
the complete runner job and retained receipt pass. ARM/multiarchitecture, a
real installed HA runtime, public publication and automatic installed-client
updates remain separate compatibility/release requirements. Preserve the
existing public Dockerfile and all installed client source URLs.
