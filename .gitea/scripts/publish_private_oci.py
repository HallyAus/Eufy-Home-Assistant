"""Publish exact-source candidates only to Daniel's private Gitea registry."""
import datetime,hashlib,json,os,pathlib,re,subprocess,tarfile,tempfile,urllib.request

REGISTRY='192.168.1.130:3000'
CLIENT_URL='https://github.com/regclient/regclient/releases/download/v0.11.6/regctl-linux-amd64'
CLIENT_HASH='8e0e62a497fcdb8048d18aa927a139613176ba0531f412bc541044e28f9856bd'
NOT_AFTER=datetime.datetime(2027,1,16,tzinfo=datetime.timezone.utc)

def inspect_archive(path=None,*,fileobj=None):
    """Check every OCI blob without extracting or executing any member."""
    blobs={};documents={};index=None
    with tarfile.open(path,mode='r|',fileobj=fileobj)as archive:
        for entry in archive:
            name=entry.name.removeprefix('./')
            if entry.isdir():
                assert name.rstrip('/')in('blobs','blobs/sha256');continue
            assert entry.isfile()and(name in('index.json','oci-layout')or re.fullmatch(r'blobs/sha256/[0-9a-f]{64}',name))
            assert 0<=entry.size<=512*1024*1024
            stream=archive.extractfile(entry);digest=hashlib.sha256();small=bytearray()
            while chunk:=stream.read(1024*1024):
                digest.update(chunk)
                if entry.size<=1024*1024:small.extend(chunk)
            if name.startswith('blobs/'):
                assert name not in blobs and digest.hexdigest()==name.rsplit('/',1)[1]
                blobs[name]=(digest.hexdigest(),entry.size)
                if small:documents[name]=bytes(small)
            elif name=='index.json':
                assert index is None and entry.size<16384;index=json.loads(small)
    assert index and len(index['manifests'])==1
    descriptor=index['manifests'][0];manifest_name='blobs/sha256/'+descriptor['digest'].split(':')[1]
    manifest=json.loads(documents[manifest_name]);assert descriptor['size']==blobs[manifest_name][1]
    for descriptor in [manifest['config'],*manifest['layers']]:
        name='blobs/sha256/'+descriptor['digest'].split(':')[1]
        assert re.fullmatch('sha256:[0-9a-f]{64}',descriptor['digest'])
        assert descriptor['size']==blobs[name][1]
    config=json.loads(documents['blobs/sha256/'+manifest['config']['digest'].split(':')[1]])
    assert(config['os'],config['architecture'])==('linux','amd64')
    return index['manifests'][0]['digest'],len(blobs)

def main():
    assert os.environ['GITHUB_SERVER_URL']=='http://'+REGISTRY
    assert os.environ['GITHUB_REPOSITORY']=='hallyaus/Eufy-Home-Assistant'
    assert os.environ['GITHUB_REF']=='refs/heads/main'
    assert os.environ['GITHUB_EVENT_NAME']in('push','workflow_dispatch')
    assert datetime.datetime.now(datetime.timezone.utc)<NOT_AFTER,'Rotate the dedicated publisher credential before its CI deadline'
    source=os.environ['GITHUB_SHA'];assert re.fullmatch('[0-9a-f]{40}',source)
    provenance=json.loads(pathlib.Path('dist/source-provenance.json').read_text())
    assert provenance['repository']=='hallyaus/Eufy-Home-Assistant'and provenance['source_sha']==source
    assert provenance['run_id']==os.environ['GITHUB_RUN_ID']and provenance['platform']=='linux/amd64'
    assert provenance['registry_pushed']is False and provenance['runtime_deployed']is False
    assert subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()==source
    assert subprocess.run(['git','diff','--exit-code'],capture_output=True).returncode==0
    archive=pathlib.Path('dist/eufy-addon-amd64.oci.tar')
    assert 0<archive.stat().st_size<=600*1024*1024
    recorded=pathlib.Path('dist/eufy-addon-amd64.sha256').read_text().split()[0]
    assert re.fullmatch('[0-9a-f]{64}',recorded)
    with archive.open('rb')as stream:assert hashlib.file_digest(stream,'sha256').hexdigest()==recorded
    digest,blobs=inspect_archive(archive)
    token=os.environ.pop('PERSONAL_PACKAGE_TOKEN');assert re.fullmatch('[0-9a-f]{40}',token)
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs):raise RuntimeError('Unexpected authenticated redirect')
    opener=urllib.request.build_opener(NoRedirect)
    def api(method,path):
        request=urllib.request.Request('http://'+REGISTRY+'/api/v1'+path,method=method,headers={'Authorization':'token '+token})
        with opener.open(request,timeout=30)as response:
            data=response.read(1024*1024+1);assert len(data)<=1024*1024
            return json.loads(data)if data else None
    user=api('GET','/user')
    assert(user['id'],user['login'],user['email'].lower(),user['visibility'])==(1,'hallyaus','daniel@printforge.com.au','private')
    image=REGISTRY+'/hallyaus/eufy-home-assistant:source-'+source
    with tempfile.TemporaryDirectory(prefix='eufy-private-publisher-')as temporary:
        workspace=pathlib.Path(temporary);client=workspace/'regctl'
        with urllib.request.urlopen(CLIENT_URL,timeout=60)as response:binary=response.read(16*1024*1024+1)
        assert len(binary)==12898428 and hashlib.sha256(binary).hexdigest()==CLIENT_HASH
        client.write_bytes(binary);client.chmod(0o700)
        environment={'PATH':os.environ['PATH'],'LANG':'C.UTF-8','REGCTL_CONFIG':str(workspace/'auth.json'),'DOCKER_CONFIG':str(workspace/'docker')}
        pathlib.Path(environment['DOCKER_CONFIG']).mkdir(mode=0o700)
        def command(*args,input=None,env=None):
            result=subprocess.run([str(client),'--host','reg='+REGISTRY+',tls=disabled',*args],input=input,
                text=True,capture_output=True,env=env or environment,timeout=180)
            assert result.returncode==0,'Private registry client operation failed'
            return result.stdout.strip()
        command('registry','login',REGISTRY,'--user','hallyaus','--pass-stdin',input=token+'\n')
        # Immutable full-source tags only. Existing tags must already match;
        # never move latest, a public release tag or any installed client.
        current=subprocess.run([str(client),'--host','reg='+REGISTRY+',tls=disabled','image','digest',image],
            capture_output=True,text=True,env=environment,timeout=30)
        if current.returncode==0:assert current.stdout.strip()==digest
        else:
            assert re.search('not found|manifest unknown|name unknown',current.stderr,re.I)
            command('image','import',image,str(archive))
        assert command('image','digest',image)==digest
        package=api('GET','/packages/hallyaus/container/eufy-home-assistant/source-'+source)
        if package.get('repository')is None:
            api('POST','/packages/hallyaus/container/eufy-home-assistant/-/link/Eufy-Home-Assistant')
            package=api('GET','/packages/hallyaus/container/eufy-home-assistant/source-'+source)
        assert package['repository']['id']==74 and package['repository']['private']is True
        anonymous=dict(environment);anonymous['REGCTL_CONFIG']=str(workspace/'anonymous.json')
        anonymous['DOCKER_CONFIG']=str(workspace/'anonymous-docker');pathlib.Path(anonymous['DOCKER_CONFIG']).mkdir(mode=0o700)
        result=subprocess.run([str(client),'--host','reg='+REGISTRY+',tls=disabled','image','digest',image],
            capture_output=True,text=True,env=anonymous,timeout=30)
        assert result.returncode!=0 and re.search(r'unauthorized|denied|forbidden|\b401\b|\b403\b',result.stderr,re.I)
    token=None
    receipt={'source':source,'run':os.environ['GITHUB_RUN_ID'],'image':image,'manifest_digest':digest,
        'all_oci_blobs_verified':blobs,'artifact_sha256':recorded,'registry_client_sha256':CLIENT_HASH,
        'private_repository_link_verified':True,'anonymous_pull_denied':True,'image_executed':False,
        'public_registry_release_or_installed_client_changed':False,'credential_values_recorded':False}
    pathlib.Path('dist/private-registry-publication.json').write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps(receipt))

if __name__=='__main__':
    try:main()
    except Exception as error:
        print(json.dumps({'private_registry_publication_complete':False,'error_type':type(error).__name__}))
        raise SystemExit(1)
