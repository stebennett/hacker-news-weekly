import copy,importlib.util,unittest
from pathlib import Path
spec=importlib.util.spec_from_file_location('renderer',Path(__file__).parents[1]/'integrations/render_email.py')
assert spec and spec.loader
r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)

def fixture():
 def item(i):return {'id':str(i),'title':'<script>example</script>','score':100,'comments':20,'created':'2026-11-10T12:00:00+00:00','url':'https://example.com/','story':'Fixture synopsis.','discussion':'Fixture discussion.'}
 return {'start':'2026-11-06T08:00:00+00:00','end':'2026-11-13T08:00:00+00:00','sections':[{'heading':h,'items':[item(i) for i in ids]} for h,ids in zip(r.HEADINGS,[range(1,11),range(11,16),range(21,31),[]])]}
class EmailTests(unittest.TestCase):
 def build(self,data,**kw):return r.build(data,recipient='reader@example.com',sender='sender@example.com',**kw)
 def test_disjoint_rankings_and_safe_link(self):
  m=self.build(fixture(),archive_url='https://twihn.com/issues/2026-11-13/',message_id='<2026-11-13@twihn.com>')
  self.assertEqual(m['Message-ID'],'<2026-11-13@twihn.com>')
  self.assertIn('Read on the web',m.get_body(preferencelist=('plain',)).get_content())
  self.assertIn('&lt;script&gt;',m.get_body(preferencelist=('html',)).get_content())
 def test_duplicate_within_section_rejected(self):
  d=fixture();d['sections'][0]['items'][1]=copy.deepcopy(d['sections'][0]['items'][0])
  with self.assertRaises(ValueError):self.build(d)
 def test_overlap_between_rankings_rejected(self):
  d=fixture();d['sections'][1]['items'][0]=copy.deepcopy(d['sections'][0]['items'][0])
  with self.assertRaises(ValueError):self.build(d)
 def test_link_cannot_point_elsewhere(self):
  with self.assertRaises(ValueError):self.build(fixture(),archive_url='https://example.com/')
 def test_email_injection_rejected(self):
  with self.assertRaises(ValueError):r.build(fixture(),recipient='a@example.com\nBcc: b@example.com',sender='s@example.com')
if __name__=='__main__':unittest.main()
