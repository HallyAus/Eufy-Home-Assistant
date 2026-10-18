# Personal Gitea add-on build

Prepared19October2026Sydney; the first actual runner execution is pending.

The primary is private VM300. The public GitHub recovery remains available to
existing Home Assistant add-on/HACS clients, with Actions disabled. This
change does not publish a version, install an integration, change the public
add-on Dockerfile or open a camera stream.

The original three-version Python matrix and full tests/import/compile/shell
checks are preserved. The full test inventory includes the separate original
hardening workflow's four files. Node24 runs the original JavaScript syntax
checks. Only after those gates pass does the personal-oci job build the image.

The existing VM301 rootless BuildKit0.34.0 service uses its authenticated
Docker-bridge mTLS endpoint and isolated user/network/process sandbox. It has
one builder slot,2CPU/6GiB service limits,12GiB cache cap and5GiB free-space
floor. The personal runner has capacity one. No Docker socket, privileged
container, host bind mount, registry push or runtime installation is added.

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
ARM/multiarchitecture, a real installed HA runtime, public publication and
automatic updates are not proven by an amd64 build and remain separate
compatibility/release requirements.
