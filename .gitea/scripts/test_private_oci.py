"""Exercise malformed/private-image boundaries without registry or device calls."""
import hashlib,io,json,tarfile,unittest
from publish_private_oci import inspect_archive

def archive(*,tamper=False,traversal=False,architecture='amd64',duplicate=False):
    entries=[]
    def blob(value):
        body=json.dumps(value,separators=(',',':')).encode()if isinstance(value,dict)else value
        digest='sha256:'+hashlib.sha256(body).hexdigest()
        entries.append(('blobs/sha256/'+digest.split(':')[1],body))
        return {'digest':digest,'size':len(body)}
    config=blob({'os':'linux','architecture':architecture})
    layer=blob(b'offline synthetic layer; never executable')
    manifest=blob({'schemaVersion':2,'config':config,'layers':[layer]})
    expected=manifest['digest']
    entries.extend([('index.json',json.dumps({'schemaVersion':2,'manifests':[manifest]}).encode()),
                    ('oci-layout',b'{"imageLayoutVersion":"1.0.0"}')])
    if tamper:entries[0]=(entries[0][0],b'changed')
    if traversal:entries.append(('../outside',b'no extraction permitted'))
    if duplicate:entries.append(entries[0])
    stream=io.BytesIO()
    with tarfile.open(fileobj=stream,mode='w')as tar:
        for name,body in entries:
            item=tarfile.TarInfo(name);item.size=len(body)
            tar.addfile(item,io.BytesIO(body))
    stream.seek(0)
    return stream,expected

class PrivateCandidateTests(unittest.TestCase):
    def test_exact_image_graph(self):
        stream,expected=archive()
        self.assertEqual(inspect_archive(fileobj=stream),(expected,3))
    def test_corrupt_blob_refused(self):
        with self.assertRaises(AssertionError):inspect_archive(fileobj=archive(tamper=True)[0])
    def test_traversal_refused(self):
        with self.assertRaises(AssertionError):inspect_archive(fileobj=archive(traversal=True)[0])
    def test_wrong_platform_refused(self):
        with self.assertRaises(AssertionError):inspect_archive(fileobj=archive(architecture='arm64')[0])
    def test_duplicate_blob_refused(self):
        with self.assertRaises(AssertionError):inspect_archive(fileobj=archive(duplicate=True)[0])

if __name__=='__main__':unittest.main()
