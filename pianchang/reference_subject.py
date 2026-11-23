"""Source-bound observations for the person shown in a portrait reference."""
import json
import re


SUBJECT_INPUT_VERSION = "reference-subject/1"
VISIBILITY = ("直接观察候选图。画面中是否确有清楚可辨的人物，足以确定这次定妆的可见脸部和造型？"
              "reason 描述实际看到的五官轮廓、发型、服饰或遮挡。没有人物且清晰可证可判 FAIL；看不清判 UNKNOWN。")
SOURCE_CHECK = ("这是本故事角色的原创定妆。角色名称仅用来归属素材，演员外形可以在这次定妆中首次确定。"
                "从 portrait_sources 逐项提取明确属于此角色且适用于定妆的可见条件，再用候选图逐项核对。"
                "区分其他角色、人物关系、职业和剧情动作；称谓不规定独一张脸，职业本身也不指定制服。"
                "当来源只给姓名、人物关系或动作，观察确有清晰人物且和现有设定相容时，"
                "可判 PASS 并写下所见人物及其造型。仅以可确定的原文条件和实际相反画面判 FAIL，遮挡或证据不够判 UNKNOWN。"
                "本项额外输出 visible（候选图直接可见的具体细节）和 citations 数组，元素为 {source_id,quote}，"
                "quote 必须原样摘自有该 ID 的来源。列出核对的明确外观条件；没有明确外观条件时给空数组。"
                "同一角色先前定妆的形象一致性由另一个含两张实际图片的比对项检查。")

# A conservative guard solely for migration of old reports. New inspections always read all
# applicable source lines, including descriptions outside this vocabulary.
APPEARANCE = re.compile(r"脸|面庞|相貌|五官|发型|发色|头发|短发|长发|白发|卷发|光头|胡须|胡子|"
                        r"肤色|眼镜|眼睛|眉|伤疤|胎记|雀斑|身形|体型|体格|身高|矮小|高大|瘦削|肥胖|"
                        r"年轻|年迈|中年|少年|老人|小孩|[零一二三四五六七八九十百\d]+岁|"
                        r"衣着|服饰|穿着|身穿|身着|戴着|外套|制服|衬衫|裙|衣|袖|盔|帽|披风|口罩|"
                        r"男子|女子|男孩|女孩|儿童|成年|银发|鬓|义肢|手指|胎记|刺青|纹身|"
                        r"hair|scar|glasses|beard|wearing|years? old|young|elderly", re.I)
VISUAL_FIELD = re.compile(r"appearance|hair|eyes?|skin|age|height|face|mark|scar|wardrobe|costume|outfit|"
                          r"clothing|silhouette|build|外观|外貌|年龄|体貌|造型|衣装|身材|着装", re.I)


def published_portrait_lines(script, subject, related):
    if not script:
        return []
    from pianchang.story_spec import index_sources
    entries = index_sources(script)
    direct = {ref.get("source_id") for ref in subject.get("source_refs",[]) if isinstance(ref,dict)}
    used = direct | {ref.get("source_id") for shot in related for ref in shot["sources"] if isinstance(ref,dict)}
    name = subject["canonical"].get("name")
    return [{"id":sid, "text":entry["text"], "kind":entry["kind"]} for sid,entry in entries.items()
            if sid in used or (name and name in entry["text"])]


def has_portrait_appearance_hint(brief):
    if (not isinstance(brief,dict) or not isinstance(brief.get("canonical"),dict)
            or any(v for k,v in brief["canonical"].items() if k != "name") or brief.get("human_feedback")):
        return True
    name = brief.get("name") or brief["canonical"].get("name")
    if not isinstance(name,str) or not name:
        return True
    if any(APPEARANCE.search(str(r.get("quote",""))) for r in brief.get("source_refs",[]) if isinstance(r,dict)):
        return True
    lines = []
    for item in [*brief.get("setup",[]), *brief.get("portrait_lines",[]), *brief.get("shots",[])]:
        if isinstance(item,dict):
            lines.extend([item.get("text",""),item.get("action",""),*(r.get("quote","") for r in item.get("sources",[]) if isinstance(r,dict))])
    lines.extend((brief.get("script") or {}).values())
    return any(APPEARANCE.search(clause) for text in lines if isinstance(text,str)
               for clause in re.split(r"[。！？\n]",text) if name in clause)


def portrait_sources(brief):
    entries = {}
    for k,v in brief.get("canonical",{}).items():
        if k != "name" and v:
            entries["canonical."+k] = v if isinstance(v,str) else json.dumps(v,ensure_ascii=False)
    for line in brief.get("portrait_lines",[]):
        entries[line["id"]] = line["text"]
    if brief.get("human_feedback"):
        entries["author_feedback"] = brief["human_feedback"]
    return entries


def portrait_observation(visibility, requirements, sources, name, *, missing_sources=()):
    raw = requirements.get("citations")
    visible = requirements.get("visible")
    citations = []
    valid = isinstance(visible,str) and bool(visible.strip()) and isinstance(raw,list) and not missing_sources
    for cite in raw if isinstance(raw,list) else []:
        sid = cite.get("source_id") if isinstance(cite,dict) else None
        quote = cite.get("quote") if isinstance(cite,dict) else None
        if (not isinstance(sid,str) or sid not in sources or not isinstance(quote,str)
                or not quote.strip() or quote.strip()==name or quote not in sources[sid]):
            valid = False
            continue
        citations.append({"source_id":sid,"quote":quote})
    if requirements["verdict"] == "FAIL":
        # A literal quotation that only names a character or recounts an action cannot
        # substantiate a visual contradiction. Unclassified evidence stays unresolved.
        valid = valid and any(APPEARANCE.search(c["quote"]) or
            (c["source_id"].startswith("canonical.") and
             (VISUAL_FIELD.search(c["source_id"][10:]) or APPEARANCE.search(c["source_id"][10:])))
            for c in citations)
    if requirements["verdict"] == "PASS":
        fixed = {k for k,v in sources.items() if k.startswith("canonical.") and
                 (VISUAL_FIELD.search(k[10:]) or APPEARANCE.search(k[10:]) or APPEARANCE.search(v))}
        valid = valid and fixed <= {c["source_id"] for c in citations}
        if name and any(name in clause and APPEARANCE.search(clause) for value in sources.values()
                        for clause in re.split(r"[。！？\n]",value)):
            valid = valid and bool(citations)
    checked = requirements["verdict"] if valid else "UNKNOWN"
    statuses = (visibility["verdict"],checked)
    verdict = "FAIL" if "FAIL" in statuses else "UNKNOWN" if "UNKNOWN" in statuses else "PASS"
    reason = visibility["reason"]+" "+requirements["reason"]
    if not valid:
        reason += " 原文依据或实际观察记录不完整。"
    return {"criterion_id":"subject","verdict":verdict,"reason":reason,
            "frame_ids":list(dict.fromkeys(visibility["frame_ids"]+requirements["frame_ids"])),
            "source_evidence":{"protocol":SUBJECT_INPUT_VERSION, "visible":visible if isinstance(visible,str) else "",
                               "visibility_verdict":visibility["verdict"], "source_verdict":checked, "citations":citations,
                               "missing_sources":list(missing_sources)}}
