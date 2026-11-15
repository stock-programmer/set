"""Evidence-backed checks for explicit quantity clauses in the original request."""
import re
from trace_system import traced


def _number(text):
    if text.isdigit():return int(text)
    digits={c:i for i,c in enumerate("零一二三四五六七八九")};digits["两"]=2
    if "十" in text:
        left,right=text.split("十",1);return (digits.get(left,1)*10)+digits.get(right,0)
    return digits.get(text)


@traced("pi.requirements.extract",kind="validation")
def explicit_quantities(idea):
    clauses=[c for c in re.split(r"[，。；\n]",idea or "") if re.search(r"要求|只写|限定|必须|总共|总计|合计|全剧",c)]
    result=[]
    for clause in clauses:
        for match in re.finditer(r"([0-9]+|[零一二两三四五六七八九十]+)\s*(集|场|个?镜头)",clause):
            count=_number(match[1]);unit=match[2]
            prefix=clause[max(0,match.start()-3):match.start()]
            if count is None or "每" in prefix:continue
            result.append({"field":{"集":"episodes","场":"scenes"}.get(unit,"shots"),"expected":count,"quote":match[0]})
    return result


@traced("pi.requirements.check",kind="validation")
def check_quantities(idea, script, project):
    actual={"episodes":len((script or {}).get("episodes",[])),
            "scenes":sum(len(e.get("scenes",[])) for e in (script or {}).get("episodes",[])),
            "shots":len((project or {}).get("shots",[]))}
    return [{"path":"requested."+r["field"],"expected":r["expected"],"actual":actual[r["field"]],
             "status":"PASS" if r["expected"]==actual[r["field"]] else "FAIL","source_quote":r["quote"]}
            for r in explicit_quantities(idea)]
