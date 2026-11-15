"""Prepare the local dotenv file and collect an optional key in a terminal."""
import getpass
import os
import re
import sys
import tempfile
import warnings
from pathlib import Path

from pianchang.bailian_video import _read_env
from pianchang.model_routes import load_routes
from trace_system import event, register_secret, traced


def _write_private(path, contents):
    # mkstemp creates a mode-0600 file. Replace only after writing it in full.
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".env.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(contents)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


@traced("launcher.configure_workbench", kind="tool", capture_output=False)
def configure_workbench(root, *, interactive=True):
    path = root / ".env"
    if not path.exists():
        event("launcher.config.create", {"source": ".env.example", "target": ".env"})
        _write_private(path, (root / ".env.example").read_bytes())
    if not interactive:
        return
    if (root / "config/models.json").is_file():
        routes = load_routes(path)
        selected = [routes.resolve(purpose) for purpose in routes.routes]
        if any(route.connection not in {"bailian.chat", "bailian.media"} for route in selected):
            if any(not route.api_key for route in selected):
                print("部分用途尚缺密钥。请按 config/models.json 的 api_key_env 在 .env 或系统环境变量中填写，然后重启工作台。")
            return
    name = "DASHSCOPE_API_KEY"
    if name in os.environ:
        if not os.environ[name].strip():
            print("环境变量 DASHSCOPE_API_KEY 为空。要在线生成，请填写或移除该变量，再重新启动。")
        return
    if _read_env(path).get(name, "").strip() or not sys.stdin.isatty():
        return
    print("在线生成需要百炼北京地域的 API Key。现在按回车可以先体验示例，稍后再次双击即可补填。", flush=True)
    while True:
        # getpass may fall back to echoing input when a terminal can't hide it.
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            try:
                key = getpass.getpass("粘贴 API Key 后按回车（输入不显示）：").strip()
            except (getpass.GetPassWarning, EOFError):
                print("此窗口无法隐藏输入。可先浏览示例，之后重新双击或在 .env 中填写 Key。")
                return
        if not key:
            return
        register_secret(key)
        if all(ch.isprintable() and not ch.isspace() and ch not in "#\"'" for ch in key):
            break
        print("请粘贴单行密钥本身，去掉引号、空格和换行。")

    # Re-read after prompting, preserving edits to the user's other settings.
    if _read_env(path).get(name, "").strip():
        return
    contents = path.read_bytes().decode("utf-8")
    assignments = list(re.finditer(r"(?m)^(?:\ufeff)?[ \t]*(?:export[ \t]+)?DASHSCOPE_API_KEY[ \t]*=[ \t]*", contents))
    if assignments:
        index = assignments[-1].end()
        end = index
        if contents[index:index + 1] in ('"', "'"):
            index += 1
            end = contents.index(contents[index - 1], index)
        contents = contents[:index] + key + contents[end:]
    else:
        newline = "\r\n" if "\r\n" in contents else "\n"
        if contents and not contents.endswith("\n"):
            contents += newline
        contents += f"{name}={key}{newline}"
    event("launcher.config.write", {"target": ".env", "setting": name})
    _write_private(path, contents.encode("utf-8"))
    print("API Key 已保存到本机的 .env。", flush=True)
