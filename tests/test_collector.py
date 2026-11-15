import importlib.util
from pathlib import Path
import datetime as dt
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('collector',Path(__file__).parents[1]/'integrations/weekly_hn_digest.py')
assert spec is not None and spec.loader is not None
c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)

class CollectorTests(unittest.TestCase):
 def test_explicit_half_open_bounds(self):
  start=dt.datetime(2026,11,6,8,tzinfo=dt.timezone.utc);end=start+dt.timedelta(days=7)
  seen=[]
  def fetch(bounds):
   seen.append(bounds)
   return [{'objectID':str(t),'created_at_i':t,'title':'Story'} for t in [int(start.timestamp()),int(end.timestamp())-1,int(end.timestamp())]]
  with patch.object(c,'fetch_shard',fetch):
   items,s,e=c.collect(7,start=start,end=end)
  self.assertEqual((s,e),(start,end))
  self.assertEqual(max(b for a,b in seen),int(end.timestamp()))
  self.assertEqual(len(items),2)
 def test_reject_partial_or_naive_bounds(self):
  with self.assertRaises(ValueError):c.collect(7,start=dt.datetime.now(dt.timezone.utc))
  with self.assertRaises(ValueError):c.collect(7,start=dt.datetime(2026,1,1),end=dt.datetime(2026,1,8))
 def test_independent_rankings(self):
  data=[{'id':str(i),'score':100-i,'comments':100-i,'tags':[]} for i in range(15)]
  votes,comments,ask,show=c.sections(data,10,50)
  self.assertEqual(votes,comments);self.assertEqual(len(votes),10)

if __name__=='__main__':unittest.main()
