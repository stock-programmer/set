"""Read-only projections over offline asset-agent records."""
import unittest

from pianchang.asset_diagnostics import asset_diagnostic, current_image_access_guidance


class AssetDiagnosticTests(unittest.TestCase):
    def item(self, *checks, purpose="portrait"):
        pin = {"asset_id": "offline/portrait", "revision_id": "r2", "purpose": purpose,
               "subject_id": "char:lead", "sha256": "a" * 64, "media_sha256": "b" * 64}
        return {"subject_id": pin["subject_id"], "purpose": purpose, "status": "needs_attention",
                "trace_id": "offline-job", "candidate": pin, "selected_pin": dict(pin, revision_id="r1"),
                "attempts": [{"trace_id": "different-job"}, {"trace_id": "offline-job"}, {"trace_id": "offline-job"}],
                "review": {"status": "UNKNOWN", "candidate": pin, "trace_id": "offline-job",
                           "brief": {"name": "阿青", "canonical": {"identity": "女店员"}},
                           "checks": list(checks)}}

    def test_required_gaps_keep_actual_evidence_and_exclude_optional_checks(self):
        face = {"path": "asset_use", "status": "UNKNOWN", "actual": "剧本未提供阿青的具体外貌特征，无法对照。"}
        palette = {"path": "palette", "status": "UNKNOWN", "actual": "剧本未指定衣服颜色。"}
        optional = {"path": "anchor_1", "status": "UNKNOWN", "required": False, "actual": "背景不同。"}
        result = asset_diagnostic(self.item(face, optional, palette), span_id="offline-review")
        self.assertEqual((result["verdict"], result["attempt_count"], result["revision_id"],
                          result["selected_revision"], result["span_id"]), ("UNKNOWN", 2, "r2", "r1", "offline-review"))
        self.assertIn("女店员", result["reason"])
        self.assertIn("配色", result["reason"])
        self.assertEqual([c["actual"] for c in result["evidence"]], [face["actual"], palette["actual"]])

    def test_concrete_defect_precedes_inconclusive_or_optional_checks(self):
        wrong = {"path": "subject", "status": "FAIL", "actual": "要求蓝色钥匙牌，图中却是红色。"}
        other = {"path": "style", "status": "UNKNOWN", "actual": "无法观察背景。"}
        item = self.item(other, wrong, purpose="prop")
        item["review"]["status"] = "FAIL"
        result = asset_diagnostic(item)
        self.assertEqual((result["stage"], result["verdict"]), ("review", "FAIL"))
        self.assertIn(wrong["actual"], result["reason"])
        self.assertEqual(result["evidence"][0]["path"], "subject")

    def test_error_stage_depends_on_evidence(self):
        item = self.item({"path": "palette", "status": "UNKNOWN", "actual": "观察返回空。"})
        item["review"]["observation_error"] = "观察服务连接中断"
        result = asset_diagnostic(item)
        self.assertEqual((result["stage"], result["verdict"]), ("review", "UNKNOWN"))
        self.assertIn("观察服务连接中断", result["reason"])
        item["review"] = {"status": "PASS", "candidate": item["candidate"]}
        item["error"] = "绑定时制作规格已变化"
        self.assertEqual(asset_diagnostic(item)["stage"], "select")
        item.update(candidate=None, review=None, error="百炼生图请求中断，受理状态未知。")
        self.assertEqual(asset_diagnostic(item)["stage"], "interrupted")
        self.assertNotIn("重新生成", asset_diagnostic(item)["action"])
        item["error"] = "百炼生图返回 HTTP 403。"
        self.assertEqual(asset_diagnostic(item)["stage"], "generate")

    def test_old_image_model_404_is_distinct_from_other_failed_calls(self):
        model = "doubao-seedream-5-0-260128"
        error = f"Error code: 404 - The model or endpoint {model} does not exist or you do not have access to it."
        item = {"status": "needs_attention", "purpose": "portrait", "error": error}
        diagnosis = asset_diagnostic(item)
        self.assertEqual((diagnosis["code"], diagnosis["model_id"], diagnosis["stage"], diagnosis["verdict"]),
                         ("image_model_access", model, "generate", "FAIL"))
        self.assertIn("回应请求", diagnosis["action"])
        recorded = {"summary": "历史原因。逐项重新生成。", "items": [{**diagnosis, "action": "逐项重新生成。"}]}
        view = current_image_access_guidance(recorded)
        self.assertIn("回应请求", view["summary"])
        self.assertEqual(view["items"][0]["action"], diagnosis["action"])
        self.assertEqual(recorded["summary"], "历史原因。逐项重新生成。")
        for update in ({"purpose": "voice"}, {"error": error.replace(model, "text-model")},
                       {"error": error.replace("404", "503")}, {"error": "HTTP 404 - Image file missing"},
                       {"error": error[:64]}, {"rejected_image_model": None}):
            with self.subTest(update=update):
                self.assertNotIn("code", asset_diagnostic({**item, **update}))

    def test_stale_review_or_legacy_missing_cause_does_not_get_attributed(self):
        faulty = {"path": "palette", "status": "FAIL", "actual": "属于旧候选的红色。"}
        item = self.item(faulty)
        item["review"]["candidate"] = dict(item["candidate"], revision_id="r1")
        self.assertEqual(asset_diagnostic(item)["evidence"], [])
        item["review"]["candidate"] = item["candidate"]
        self.assertEqual(asset_diagnostic(item, trace_id="different-job")["evidence"], [])
        self.assertEqual(asset_diagnostic({"status": "needs_attention"})["stage"], "unknown")
        self.assertIsNone(asset_diagnostic({"status": "approved"}))
