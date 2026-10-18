"""Build an exact tracked-source context without the public recovery fetch."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess


def git(*args):
    return subprocess.check_output(['git', *args])


def local_dockerfile(source):
    start = source.index('ARG REPO_URL=')
    end = source.index('\nWORKDIR /opt/eufy/bridge', start)
    block = source[start:end]
    if hashlib.sha256(block.encode()).hexdigest() != '52b40f22dad3b72c7cc1e1a4819d338812d2cfbdc93f81add2364e912383b666':
        raise ValueError('Review the changed source-fetch contract before transforming it')
    return source[:start] + 'COPY bridge /opt/eufy/bridge\n' + source[end:]


def main():
    sha = git('rev-parse', 'HEAD').decode().strip()
    if not re.fullmatch(r'[0-9a-f]{40}', sha) or os.getenv('GITHUB_SHA') != sha:
        raise SystemExit('Exact workflow source required')
    if git('status', '--porcelain', '--untracked-files=no'):
        raise SystemExit('Clean tracked source required')
    destination = Path('dist/eufy-build-context')
    destination.mkdir(parents=True, exist_ok=False)
    entries = git('ls-tree', '-rz', sha, '--', 'eufy_nvr/', 'bridge/')
    members = {}
    for entry in entries.split(b'\0'):
        if not entry:
            continue
        metadata, raw_name = entry.split(b'\t', 1)
        mode, kind, oid = metadata.decode().split()
        name = raw_name.decode()
        path = PurePosixPath(name)
        if mode not in ('100644', '100755') or kind != 'blob' or '..' in path.parts or '\\' in name:
            raise SystemExit('Only regular tracked context files are allowed')
        if any(part.startswith('.env') or part in ('__pycache__', 'node_modules') for part in path.parts):
            raise SystemExit('Configuration/generated context member refused')
        target_name = name.removeprefix('eufy_nvr/') if name.startswith('eufy_nvr/') else name
        data = git('cat-file', 'blob', oid)
        if name == 'eufy_nvr/Dockerfile':
            data = local_dockerfile(data.decode()).encode()
        target = destination / target_name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        members[target_name] = hashlib.sha256(data).hexdigest()
    for required in ('Dockerfile', 'run.sh', 'bridge/requirements.txt', 'bridge/eufy_stream.py'):
        if required not in members:
            raise SystemExit('Incomplete exact-source build context')
    receipt = {'repository': 'hallyaus/Eufy-Home-Assistant', 'source_sha': sha,
               'source_tree': git('rev-parse', 'HEAD^{tree}').decode().strip(),
               'bridge_tree': git('rev-parse', 'HEAD:bridge').decode().strip(),
               'run_id': os.getenv('GITHUB_RUN_ID'), 'context_members_sha256': members,
               'source_transport': 'tracked VM300 blobs copied locally; no recovery fetch',
               'platform': 'linux/amd64', 'registry_pushed': False, 'runtime_deployed': False}
    Path('dist/source-provenance.json').write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: receipt[key] for key in ('repository', 'source_sha', 'bridge_tree', 'platform', 'registry_pushed', 'runtime_deployed')}))


if __name__ == '__main__':
    main()
