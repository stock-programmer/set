"""Plain-language read model for a single recorded asset work outcome."""
import re


_LABELS = {"subject": "对象外观", "asset_use": "辨识特征", "scene_use": "场景用途",
           "style": "风格", "palette": "配色", "concept": "创作意图", "voice": "音色",
           "performance": "语气", "audibility": "声音清晰度", "dialogue": "台词", "revision": "修改要求"}
_FALLBACK_ACTION = "展开证据或查看技术日志，定位中断的位置。"


def _text(value):
    return " ".join(value.split()) if isinstance(value, str) else ""


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _short(value, limit=105):
    return value if len(value) <= limit else value[:limit - 1] + "…"


def _label(path):
    if path.startswith(("anchor_", "declared_reference_")):
        return "参考素材比对"
    return _LABELS.get(path, path or "自检项目")


def _check_explanation(check, purpose, brief):
    """Condense only distinctions actually mentioned by the observer."""
    path, observed = check["path"], _text(check["actual"])
    if check["status"] == "UNKNOWN":
        missing = r"(?:剧本|原文|设定|资产定义)[^。；]{0,18}(?:没有|未(?:明确|提供|指定|给出))[^。；]{0,30}"
        if path == "palette" and re.search(missing + "(?:颜色|色彩|配色)", observed):
            return "自检报告指出，剧本没有给出可对照的配色。", "可在修改要求中写明这项资产的主色，再重新生成。"
        if purpose == "portrait" and path in {"subject", "asset_use"} and re.search(missing + "(?:外观|外貌|脸型|长相)", observed):
            identity = _text(_mapping(brief.get("canonical")).get("identity"))
            desc = f"人物设定写的是“{_short(identity, 35)}”。" if identity else ""
            return desc + "自检缺少可对照的外貌特征，无法确认定妆照。", "可在修改要求中补充一个能认出角色的特征，或导入角色参考图。"
        if path == "subject" and purpose == "prop" and re.search("静态.{0,6}(?:无法|不能)", observed):
            action = _text(_mapping(brief.get("canonical")).get("appearance"))
            if action and action in observed:
                desc = f"剧本描述了“{_short(action, 35)}”。"
            else:
                desc = ""
            return desc + "道具静帧无法验证自检报告提到的剧情动作。", "道具图可按可见特征核验，动作应在对应视频镜头中检查。"
        if purpose == "prop" and path == "asset_use" and re.search(missing + "(?:造型|形状|封面|标识|外观|材质)", observed):
            return "剧本没有给出能辨认这一道具的具体外观。", "可在修改要求中写明材质、颜色或可辨认的标记，再重新生成。"
    if observed:
        # Keep an excerpt of the actual observation for criteria without a specific explanation.
        return f'{check["label"]}：{_short(observed)}', None
    return f'{check["label"]}缺少可读的观察记录。', None


def _error_explanation(error, review_status, has_pin):
    if re.search("受理状态.{0,6}(?:未知|不明|需核实)|可能已处理|timed? ?out|超时|任务中断|without response", error, re.I):
        return "interrupted", "UNKNOWN", "调用中断，记录无法确认这次操作是否完成。", "先在 Trace 核对服务商的任务或响应，再决定是否重试。"
    if review_status == "PASS" and has_pin:
        return "select", "FAIL", "候选通过自检，但选用或绑定镜头时未能完成。" + _short(error), "核对当前剧本、自检报告和候选版本后，再处理绑定。"
    if "下载" in error:
        return "generate", "FAIL", "素材下载没有完成。" + _short(error), "先查看下载响应，核对能否取回已有素材。"
    if re.search("百炼生图|百炼声音调用", error):
        return "generate", "FAIL", "制作调用返回错误。" + _short(error), "打开该次调用，按服务商返回的错误检查配置或输入。"
    return "unknown", "UNKNOWN", "资产任务停在这条错误记录：" + _short(error), _FALLBACK_ACTION


def asset_diagnostic(item, name=None, *, trace_id=None, span_id=None):
    """Project existing evidence only. Callers may supply an exact trace/span association."""
    if item.get("status") != "needs_attention":
        return None
    pin = _mapping(item.get("candidate"))
    review = _mapping(item.get("review"))
    # Do not present observations belonging to another candidate or a different run.
    trace_id = trace_id or item.get("trace_id") or review.get("trace_id")
    reviewed_pin = review.get("candidate") or review.get("pin")
    if ((trace_id and review.get("trace_id") and review["trace_id"] != trace_id)
            or (reviewed_pin and reviewed_pin != pin)):
        review = {}
    brief = _mapping(review.get("brief"))
    status = review.get("verdict") or review.get("status")
    attempts = item.get("attempts")
    attempt_count = None
    if isinstance(attempts, list):
        attempt_count = sum(1 for attempt in attempts if isinstance(attempt, dict)
                            and (not trace_id or attempt.get("trace_id") == trace_id))
    checks = []
    for check in review["checks"] if isinstance(review.get("checks"), list) else []:
        if not isinstance(check, dict) or check.get("required", True) is False or check.get("status") not in {"UNKNOWN", "FAIL"}:
            continue
        path = _text(check.get("path"))
        checks.append({"path": path, "label": _label(path), "status": check["status"],
                       "expected": check.get("expected"), "actual": check.get("actual")})
    error = _text(item.get("error"))
    observer_error = _text(review.get("observation_error"))
    purpose = item.get("purpose")
    if error:
        stage, verdict, reason, action = _error_explanation(error, status, bool(pin))
    elif observer_error:
        stage, verdict = "review", "UNKNOWN"
        reason = "自检调用中断，未得到可用的观察结论。" + _short(observer_error)
        action = "先核对观察调用的结果；已有图片或声音可再次核验。"
    elif checks:
        stage = "review"
        checks.sort(key=lambda c: c["status"] != "FAIL")
        verdict = "FAIL" if any(c["status"] == "FAIL" for c in checks) else "UNKNOWN"
        explanations = [_check_explanation(c, purpose, brief) for c in checks]
        reason = " ".join(dict.fromkeys(text for text, _ in explanations[:2]))
        actions = [text for _, text in explanations[:2] if text]
        action = " ".join(dict.fromkeys(actions)) or (
            "按观察到的偏差填写修改要求，重新生成候选。" if verdict == "FAIL" else "查看必检项缺少的证据；可补充修改要求或导入相应参考。")
    else:
        # Old jobs may have only a summary or a verdict, with no recorded cause.
        stage = "review" if status in {"FAIL", "UNKNOWN"} else "unknown"
        verdict = "FAIL" if status == "FAIL" else "UNKNOWN"
        reason = "现有记录没有指出具体未通过哪一项。"
        action = _FALLBACK_ACTION
    return {"subject_id": item.get("subject_id"), "name": name or brief.get("name") or item.get("subject_id"),
            "purpose": purpose, "stage": stage, "verdict": verdict, "reason": reason, "action": action,
            "revision_id": pin.get("revision_id"), "attempt_count": attempt_count,
            "trace_id": trace_id, "span_id": span_id or review.get("span_id"), "evidence": checks,
            "candidate": dict(pin) if pin else None, "review_ref": review.get("review_ref"),
            "selected_revision": _mapping(item.get("selected_pin")).get("revision_id")}
