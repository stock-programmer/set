"""Evidence-backed checks for explicit quantity clauses in the original request."""
import re

from trace_system import traced


_DIGITS = {c:i for i,c in enumerate("零一二三四五六七八九")}
_DIGITS["两"] = 2
_NUMBER = r"(?:[0-9]+|[零一二两三四五六七八九十]+)"
_QUANTITY = re.compile(
    rf"(?P<count>{_NUMBER})\s*(?:"
    rf"(?P<shots>(?:(?:个|条|段)\s*(?:(?:时长(?:为)?\s*)?(?P<seconds>{_NUMBER})\s*秒(?:钟)?\s*的?\s*)?)?镜头)"
    r"|(?P<characters>(?:个|名|位)?角色)"
    r"|(?P<beats>(?:个)?(?:连续可拍|可拍摄|可拍|连续)?的?节拍)"
    r"|(?P<scenes>个?场景|场戏|场)"
    r"|(?P<episodes>集))"
)
_FIELDS = ("shots","characters","beats","scenes","episodes")
_DIRECTIVE = re.compile(r"要求|只写|只用|只拍|限定|必须|总共|总计|合计|全剧|采用|拍成|写成")
# Ranges, examples, ordinals and local counts do not specify an exact production-wide total.
_SCOPED = re.compile(r"每(?:个|一|集|场|段|镜|秒)|第\s*" + _NUMBER + r"|至少|最多|至多|不超过|不少于|大约|大概|例如|比如|约\s*" + _NUMBER
                     + rf"|{_NUMBER}\s*(?:个?(?:镜头|场景|角色|节拍)|集|场)?\s*(?:至|到|[-~～—或])\s*{_NUMBER}")
_SINGLE_DURATION = re.compile(rf"(?:镜头时长|时长)\s*(?:为|是)?\s*({_NUMBER})\s*秒(?:钟)?")


def _number(text):
    if text.isascii() and text.isdigit():
        return int(text)
    if re.fullmatch(r"[一二两三四五六七八九]?十[一二三四五六七八九]?",text):
        tens, units = text.split("十")
        return _DIGITS.get(tens,1)*10 + _DIGITS.get(units,0)
    return _DIGITS.get(text)


def seedance_request_limit(idea):
    """Only a count attached to Seedance submissions, never a scene or beat count."""
    match = re.search(rf"seedance\s*(?:视频)?(?:生成)?(?:模型|接口)?\s*(?:只|最多|至多)?\s*(?:发送|提交|调用)\s*({_NUMBER})\s*次", idea or "", re.I)
    return _number(match[1]) if match else None


@traced("pi.requirements.extract",kind="validation")
def explicit_quantities(idea):
    result = []
    previous_single_shot = False
    for clause in re.split(r"[，,。；;、：:\n]",idea or ""):
        clause = clause.strip()
        duration = _SINGLE_DURATION.fullmatch(clause)
        if duration and previous_single_shot:
            seconds = _number(duration[1])
            if seconds is not None:
                result.append({"field":"shot_duration_s","expected":seconds,"quote":clause})
        previous_single_shot = False
        matches = list(_QUANTITY.finditer(clause))
        if not matches:
            continue
        # Standalone specifications also occur as comma-separated lists without “要求”.
        remainder = _QUANTITY.sub("",clause)
        if not _DIRECTIVE.search(clause) and re.sub(r"[\s和与及共]", "", remainder):
            continue
        for match in matches:
            # A later local instruction, e.g. 每集三场, does not cancel an earlier 全剧一集.
            total_start = max(clause.rfind(word,0,match.start()) for word in ("总共","总计","合计","全剧"))
            if _SCOPED.search(clause[max(0,total_start):match.end()]):
                continue
            if re.match(r"\s*(?:左右|上下|以上|以下|以内|以外|[至到~～—-]\s*" + _NUMBER + ")", clause[match.end():]):
                continue
            count = _number(match["count"])
            if count is None:
                continue
            field = next(k for k in _FIELDS if match[k] is not None)
            if match["scenes"] == "场" and re.match(r"[\u4e00-\u9fff]",clause[match.end():]):
                # In compact specifications the next count can immediately follow 三场.
                if not _QUANTITY.match(clause,match.end()) and clause[match.end()] not in "和及与":
                    continue
            result.append({"field":field,"expected":count,"quote":match[0]})
            if field == "shots":
                previous_single_shot = count == 1
                if match["seconds"]:
                    seconds = _number(match["seconds"])
                    if seconds is not None:
                        result.append({"field":"shot_duration_s","expected":seconds,"quote":match[0]})
    return result


@traced("pi.requirements.check",kind="validation")
def check_quantities(idea, script, project):
    episodes = (script or {}).get("episodes",[])
    scenes = [scene for episode in episodes for scene in episode.get("scenes",[])]
    shots = (project or {}).get("shots")
    shots = shots if isinstance(shots,list) else []
    if (project or {}).get("generation_request_limit") is not None:
        shots = [member for task in shots for member in task.get("creative_shots", [task])]
    durations = [shot.get("duration_s") if isinstance(shot,dict) else None for shot in shots]
    entities = (project or {}).get("entities")
    characters = sum(isinstance(entity,dict) and entity.get("kind")=="character"
                     for entity in entities.values()) if isinstance(entities,dict) else 0
    actual = {"episodes":len(episodes),"scenes":len(scenes),"shots":len(shots),"characters":characters,
              "beats":sum(len(scene.get("beats",[])) for scene in scenes),
              "shot_duration_s":durations[0] if durations and all(d==durations[0] for d in durations) else durations}
    return [{"path":"requested."+r["field"],"expected":r["expected"],"actual":actual[r["field"]],
             "status":"PASS" if r["expected"]==actual[r["field"]] else "FAIL","source_quote":r["quote"]}
            for r in explicit_quantities(idea)]
