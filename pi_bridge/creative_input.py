"""User-reserved story facts for an unpublished script, tied to the author’s message."""
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from trace_system import traced


TOPICS = {
    "character_name": ("主人公姓名", r"(?:主角|主人公|角色|男主|女主)(?:的)?(?:名(?:字)?|姓名)"),
    "setting": ("故事地点", r"(?:故事(?:的)?(?:发生地(?:点)?|地点|背景地点)|取景地)"),
    "title": ("片名", r"(?:片名|剧名|故事标题)"),
    "ending": ("故事结局", r"(?:故事)?结局"),
}
QUESTION_SCHEMA = {"type": "object", "additionalProperties": False,
    "properties": {"topic": {"type": "string", "enum": list(TOPICS)},
                   "source_quote": {"type": "string", "minLength": 4, "maxLength": 280},
                   "choices": {"type": "array", "minItems": 2, "maxItems": 5,
                               "items": {"type": "string", "minLength": 1, "maxLength": 40}}},
    "required": ["topic", "source_quote"]}

# This is a stop condition specified by the author ("wait for my X before writing"),
# stronger than a model believing that the story could use an additional detail.
AUTHOR_GATE = re.compile(r"(?:等我|等到我|待我|先(?:向我|问我|让我)|(?:由我|我会|我来|我稍后|我先))"
                         r"[^。！？\n]{0,100}(?:再(?:开始|动笔|写|编|创作|继续)|(?:之前|以前)[^。！？\n]{0,8}(?:别|不要|不能)(?:写|动笔|开始))")
CONTROL = re.compile(r"(?:授权|权限|批准|密钥|密码|账号|账户|API[ _-]?KEY|TOKEN|代付|付费|预算|接入点|工具调用|开通|执行命令)", re.I)
NOT_WAITING = re.compile(r"(?:无须|无需|不必|不用|不要|别|不需要)(?:再|先|一直|刻意)?(?:等(?:待)?|问|让|向)我")


def _reserved(sentence, topic):
    if NOT_WAITING.search(sentence):
        return False
    for gate in AUTHOR_GATE.finditer(sentence):
        if not re.search(r"给|提供|指定|决定|定下|补充|选择|选好|挑|告诉|确认|起名|命名", gate.group()):
            continue
        if re.search(TOPICS[topic][1], gate.group()) or re.search(
                TOPICS[topic][1] + r"[，、：: ]{0,4}$", sentence[:gate.start()]):
            return True
    return False


def _author_choices(quote):
    clauses = re.findall(r"(?:从|在|备选(?:是|为)?[：:]?|选项(?:是|为)?[：:]?|[（(])"
                         r"([^。！？；;\n]{2,180}?)(?:中|里|[）)；;。！？]|$)", quote)
    for clause in clauses:
        options = [s.strip(" \t\"“”‘’《》") for s in re.split(r"[、，,]|或(?:者)?", clause)]
        if 2 <= len(options) <= 5:
            yield options


def _read_rows(path, query, args):
    if not path.is_file():
        return []
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(query, args)]


def validate_reservation(root, session, job, proposed, *, journaled_answer_id=""):
    if (session.get("role") != "script" or job.get("kind") not in {"pi_script", "studio_script"}
            or job.get("_pi_mode", "work") != "work" or job.get("_pi_bus_message")
            or job.get("cancel_requested") or job.get("trace_incomplete") or job.get("script_ref")
            or job.get("_pi_creative_answer") or not isinstance(proposed, dict)
            or set(proposed) - {"topic", "source_quote", "choices"}):
        raise ValueError("本轮不能申请创作资料。")
    topic, quote = proposed.get("topic"), proposed.get("source_quote")
    if (not isinstance(topic, str) or topic not in TOPICS or not isinstance(quote, str)
            or not 4 <= len(quote) <= 280 or CONTROL.search(quote)
            or not any(_reserved(sentence, topic) for sentence in re.split(r"[。！？\n]", quote))):
        raise ValueError("请核对作者原话是否明确要求等本人提供这项资料后再动笔。可自行决定时继续写作。")
    choices = proposed.get("choices", [])
    if ("choices" in proposed and (not isinstance(choices, list) or not 2 <= len(choices) <= 5
            or any(not isinstance(v, str) or not 1 <= len(v) <= 40 or v.strip() != v or v not in quote
                   or CONTROL.search(v) or not v.isprintable() for v in choices)
            or len(set(choices)) != len(choices) or choices not in _author_choices(quote))):
        raise ValueError("选项必须逐项来自同一份作者原话。")
    story = session.get("source_job")
    expected = job["id"] if job["kind"] == "studio_script" else job.get("story_job_id")
    if (not isinstance(story, str) or not re.fullmatch("[a-f0-9]{32}", story)
            or story != expected or session["story_id"] != "story-" + story
            or job.get("session_id", session["id"]) != session["id"]):
        raise ValueError("提问与原剧本会话不匹配。")
    root = Path(root)
    workspace = root / "outputs/pi/workspaces" / session["story_id"]
    if ((workspace / "imported-script.json").exists() or (workspace / "script/candidate.json").exists()
            or _read_rows(root / "outputs/collaborations/assets.sqlite3",
                          "SELECT 1 FROM assets WHERE asset_id=? LIMIT 1", (session["story_id"] + "/script",))):
        raise ValueError("故事已经有剧本或候选，先核对已有资料和编译结果。")
    registry = root / "outputs/pi/registry.sqlite3"
    messages = _read_rows(registry, "SELECT id,text,role,mode,job_id FROM messages WHERE session_id=? AND id!=? "
                          "AND role IN ('user','agent') ORDER BY created_at DESC,rowid DESC LIMIT 1",
                          (session["id"], journaled_answer_id))
    message = messages[0] if messages else {}
    if (message.get("role") != "user" or message.get("mode") != "work" or message.get("job_id") != job["id"]
            or quote not in message["text"]):
        raise ValueError("原会话已有新消息或未找到作者的原文约束。")
    before, _, after = message["text"].partition(quote)
    leading = re.split(r"[。！？\n]", before)[-1]
    if (NOT_WAITING.search(leading + quote) or re.search(r"[“‘\x22]$", leading)
            or message["text"].count(quote) != 1):
        raise ValueError("这段话没有明确要求暂停本次写作。")
    remaining = after.strip(" 。！？.!?\n\r\t")
    if remaining and (re.search(TOPICS[topic][1], remaining) or re.search("不用等|现在就写|直接写|补充|更正", remaining)):
        raise ValueError("先核对作者随后补充的说明。")
    return {"topic": topic, "source_quote": quote, **({"choices": choices} if choices else {}),
            "source_message_id": message["id"], "session_id": session["id"], "story_id": session["story_id"]}


@traced("pi.story.verify_creative_input", kind="validation", capture_inputs=False)
def verify_creative_input(workspace, proposed):
    if workspace.script_ref or workspace.workspace.candidate or workspace.existing_publication or workspace.unknown:
        raise ValueError("请先核对本故事已有的稿件或原调用回执。")
    return validate_reservation(workspace.service.root, workspace.session, workspace.job, proposed)
