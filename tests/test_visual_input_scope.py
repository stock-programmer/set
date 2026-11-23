import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pianchang.asset_review import ReferenceObserver, _expectations, _observe_images
from pianchang.bailian_text import TextConfig
from pianchang.reference_subject import portrait_observation
from tests.test_reference_assets import offline_png
from trace_system import Tracer, use_context


class VisualInputTests(unittest.TestCase):
    def test_serialized_image_scope_preserves_real_comparison_failures(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(use_context(None,tracer=Tracer(root/"traces")))
        roles = {"candidate":{"subject_id":"role-a", "purpose":"portrait", "name":"演员甲"},
                 "anchor_0":{"subject_id":"role-b","purpose":"portrait","name":"演员乙","required":True},
                 "anchor_1":{"subject_id":"role-a","purpose":"portrait","name":"演员甲","required":True}}
        brief = {"purpose":"portrait","subject_id":"role-a","canonical":{},"source_refs":[],
                 "frame_roles":roles,"shared_style":"胶片摄影","human_feedback":"",
                 "portrait_lines":[{"id":"B004","kind":"beat","text":"演员甲留着短发。"}]}
        images = {k:offline_png((i+1,80,100)) for i,k in enumerate(roles)}
        files = {}
        for k, data in images.items():
            files[k] = root / (k+".png")
            files[k].write_bytes(data)
        criteria = _expectations(brief, [roles["anchor_0"],roles["anchor_1"]])
        payloads = []
        def respond(req):
            content = json.loads(req.data)["messages"][1]["content"]
            items = json.loads(content[0]["text"].split("\n",1)[1])
            frames = {json.loads(content[i]["text"])["frame_id"]:base64.b64decode(
                content[i+1]["image_url"]["url"].split(",",1)[1]) for i in range(1,len(content),2)}
            payloads.append((items,frames))
            rows = [{"criterion_id":i["criterion_id"], "reason":"离线媒体证据", "frame_ids":list(frames),
                     "verdict":"FAIL" if (i["criterion_id"]=="shared_style" and "other_subject_style_sample" in frames)
                     or (i["criterion_id"]=="comparison" and "same_subject_reference" in frames) else "PASS"} for i in items]
            for row in rows:
                if row["criterion_id"] == "portrait_sources_check":
                    row.update(visible="候选画面可见一名短发成年男子的清楚正面。",
                               citations=[{"source_id":"B004","quote":"留着短发"}])
            return {"id":"stub-"+str(len(payloads)),"choices":[{"finish_reason":"stop",
                    "message":{"content":json.dumps({"observations":rows})}}]}, None
        with patch.object(ReferenceObserver,"_post",side_effect=respond):
            rows, provider = _observe_images(SimpleNamespace(vision_config=TextConfig(api_key="offline-vision")),
                                            brief,criteria,files)
        self.assertEqual([p[1] for p in payloads], [
            {"candidate":images["candidate"]},
            {"candidate":images["candidate"],"other_subject_style_sample":images["anchor_0"]},
            {"candidate":images["candidate"],"same_subject_reference":images["anchor_1"]}])
        self.assertEqual(set(payloads[0][0][0]["brief"]["frame_roles"]),{"candidate"})
        self.assertNotIn("可比图像",next(i["expected"] for i in payloads[0][0] if i["criterion_id"]=="style"))
        self.assertEqual(payloads[1][0][0]["brief"]["frame_roles"]["other_subject_style_sample"]["name"],"演员乙")
        result = {r["criterion_id"]:r for r in rows}
        self.assertEqual((result["subject"]["verdict"],result["anchor_0"]["verdict"]),("PASS","PASS"))
        self.assertEqual(payloads[0][0][0]["brief"]["portrait_sources"],{"B004":"演员甲留着短发。"})
        self.assertEqual(result["subject"]["source_evidence"]["citations"][0],{"source_id":"B004","quote":"留着短发"})
        self.assertEqual(result["anchor_1"]["verdict"],"FAIL")
        self.assertEqual(result["style"]["verdict"],"FAIL")
        self.assertEqual(set(result["style"]["frame_ids"]),{"candidate","anchor_0"})
        self.assertEqual(len(provider["requests"]),3)
        roles["candidate"].update(subject_id="prop-cup",purpose="prop",name="杯子")
        roles["anchor_0"].update(subject_id="prop-screen",purpose="prop",name="屏幕")
        roles["anchor_1"].update(subject_id="prop-cup",purpose="prop",name="杯子")
        brief.update(subject_id="prop-cup",purpose="prop")
        criteria = _expectations(brief,[roles["anchor_0"],roles["anchor_1"]])
        with patch.object(ReferenceObserver,"_post",side_effect=respond):
            _observe_images(SimpleNamespace(vision_config=TextConfig(api_key="offline-vision")),brief,criteria,files)
        prop_comparison = payloads[4][0][0]
        self.assertIn("道具各有自己的形制", prop_comparison["expected"])
        self.assertEqual(prop_comparison["brief"]["frame_roles"]["other_subject_style_sample"]["name"],"屏幕")

    def test_portrait_subject_requires_both_pixels_and_bound_source(self):
        person = {"verdict":"PASS", "reason":"清楚可见一名短发男子及其脸部。", "frame_ids":["candidate"]}
        original = {"B004":"张先生站起身来。"}
        observed = {**person, "visible":"正面短发男子，脸部轮廓清楚。", "citations":[]}
        self.assertEqual(portrait_observation(person,observed,original,"张先生")["verdict"],"PASS")
        self.assertEqual(portrait_observation(person,observed,original,"张先生",missing_sources=["B004"])["verdict"],"UNKNOWN")
        fabricated = {**observed, "verdict":"FAIL", "reason":"无法证明这个人就是张先生，必须有一颗痣。"}
        self.assertEqual(portrait_observation(person,fabricated,original,"张先生")["verdict"],"UNKNOWN")
        unrelated = {**fabricated,"citations":[{"source_id":"B004","quote":"张先生站起身来"}]}
        self.assertEqual(portrait_observation(person,unrelated,original,"张先生")["verdict"],"UNKNOWN")
        unknown = {**person, "verdict":"UNKNOWN", "reason":"头部完全遮挡。"}
        self.assertEqual(portrait_observation(unknown,observed,original,"张先生")["verdict"],"UNKNOWN")
        real_source = {"canonical.hair":"白色短发", "B004":"张先生满头白发。"}
        self.assertEqual(portrait_observation(person,observed,real_source,"张先生")["verdict"],"UNKNOWN")
        mismatch = {**observed, "verdict":"FAIL","reason":"候选是黑色长发，与白色短发冲突。",
                    "citations":[{"source_id":"canonical.hair","quote":"白色短发"}]}
        self.assertEqual(portrait_observation(person,mismatch,real_source,"张先生")["verdict"],"FAIL")
        bad_quote = {**mismatch,"citations":[{"source_id":"B004","quote":"脸上有一颗痣"}]}
        self.assertEqual(portrait_observation(person,bad_quote,real_source,"张先生")["verdict"],"UNKNOWN")
